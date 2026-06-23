# V2 Agent Rebuild — Review Findings & Fix Tracker

Independent critical review of the unified streaming tool-loop rebuild
(`6a9b3b5` + uncommitted working-tree changes), captured before the first
live-model/DB test. Source analysis: see `docs/aegis_v2/v2_agent_rebuild_plan.md`
for the rebuild's own phase notes.

This file is the working tracker. Work the items top-to-bottom by priority.

## Status legend

- `TODO` — not started
- `WIP` — in progress
- `DONE` — implemented + covered by a test (or verified)
- `DEFER` — intentionally out of scope for this pass (with reason)

## Summary table

| ID | Sev | Status | Finding |
| --- | --- | --- | --- |
| F1 | High | DONE | Content-before-tool-call in the same step leaks narration and corrupts shell state (discards `present_final_response` tiles / drops shell marker) |
| F2 | High | DONE | Inline `FALLBACK_SYSTEM_PROMPT` drifted from `orchestrator.yaml`; comment claims sync; inline copy is what runs until the DB row is pushed |
| F3 | High | DONE | Prompts load lazily on first turn; preload + cache all prompts at app launch so prompt loading is local/fast |
| F4 | Med-High | DONE | Phase 0 bank-symbol mismatch degrades to a silent "no coverage" gap; catalog read errors indistinguishable from genuine no-coverage |
| F5 | Med | DONE | Multiple `run_research` calls in one turn overwrite `turn_answer_*` state; earlier evidence citations dropped as hallucinations |
| F6 | Med | DONE | Body-step failure *after* `present_final_response` emitted the shell → no synthesis fallback → orphaned tiles, no body, no `final=true` |
| F7 | Med | DONE | "Unified loop" doesn't hold for availability: turn-terminal + hardcoded templated (non-agent) message |
| F8 | Low-Med | PARTIAL | Regex `build_final_shell` is on the primary path whenever the agent skips `present_final_response` (allowed by `tool_choice: auto`) |
| F9 | Low-Med | DONE | Tiles kept even when all `evidence_ids` filtered out; tile values never validated against evidence → unbacked metric tiles possible |
| F10 | Low | DONE | Loop-limit backstop emits a non-final `chat.message`; can leave an open `final_response` stream unclosed |
| F11 | Low | PARTIAL | Cleanups: `evidence_ids` local shadows imported function; fragile queue-drain loop; `max_tokens: 1100` truncation risk; no prompt caching |

---

## F1 — Content-before-tool-call corrupts the streamed answer (High) — DONE

**Resolution:** `_run_streaming_step` now takes `stream_live` (set from
`state.turn_shell_emitted`). On ambiguous steps (`stream_live=False`) content
deltas are buffered and only flushed if the step resolved to a direct answer;
a step that resolves to a tool call discards the buffered preamble. Live
real-time streaming is preserved for the body step after `present_final_response`
(when `turn_shell_emitted` is True). A failed step re-raises before any buffered
content is emitted, so failures never leak partial content. Covered by
`test_preamble_before_present_tool_call_does_not_leak_or_clobber_tiles`.

(original analysis below)



**Where:** `v2/orchestrator.py::_run_streaming_step` (~1175-1194),
`v2/agent/tool_agent.py::run_agent_step` (~466-481), shell guard at
`orchestrator.py:1287`.

**Problem:** `run_agent_step` forwards every content delta to `on_delta`
immediately, before it knows the step will end in a tool call. With
`tool_choice: "auto"` a model can emit a short preamble ("Let me check…") and
*then* a tool call in the same step. The orchestrator emits those tokens as
`chat.delta` and opens the answer header via `_open_answer_stream`, setting
`turn_shell_emitted = True`. Consequences:
- On a `present_final_response` step, `_open_answer_stream` builds the
  deterministic regex shell and the agent's hand-authored tiles are then
  discarded by `if not state.turn_shell_emitted:` — the Phase 3 regression
  the rebuild set out to kill.
- On the first step, a spurious `tool.completed agent_decision` marker is
  emitted, the preamble leaks as `chat.delta`, and the final message is
  persisted with no shell marker.

**Untested:** every streaming test feeds *either* content or a tool call, never
both in one step.

**Fix direction:** Buffer assistant content inside `run_agent_step` and only
forward it to `on_delta` once the step is known to be a pure direct answer (no
tool calls accumulated). If a tool call is present, the buffered content is
preamble and must not reach the answer stream or mutate shell state.

---

## F2 — Inline fallback prompt drifted from the YAML (High) — DONE

**Resolution:** `FALLBACK_SYSTEM_PROMPT` now mirrors the YAML's `system_prompt`
(the three scope rules that had been added only to the YAML are now in the inline
copy). Added `test_inline_fallback_prompt_matches_yaml`, which loads the YAML and
asserts whitespace-normalized equality, so future drift fails CI. Comment updated
to point at that test. (The DB row still needs pushing via
`scripts/push_aegis_prompts.py` for the live environment to use the DB copy; until
then the now-correct inline copy runs.)

(original analysis below)



**Where:** `v2/agent/tool_agent.py:17-60` (`FALLBACK_SYSTEM_PROMPT` + the
"kept in sync" comment), `aegis-prompts/agent/orchestrator.yaml:27-39`.

**Problem:** The YAML gained three scope rules (explicit_source_filter,
optional_context, greetings carve-out) that the inline body lacks; the comment
claims the inline is "kept in sync." Behavior is softened because
`RUNTIME_SYSTEM_RULES` is appended to both, but that double-states the rules on
the DB path and leaves the inline copy carrying the older "Honor UI-selected
sources" phrasing. Production uses the inline copy until the DB row is pushed
(`load_prompt_from_db` raises on a missing row), so the first live test won't
exercise the YAML.

**Fix direction:** Make the two a single source of truth or a verified mirror;
add a test asserting the inline fallback matches the YAML's `system_prompt`
(modulo the runtime-rules suffix). Push the prompt row to the target DB.

---

## F3 — Preload + cache all prompts at app launch (High) — DONE

**Resolution:** Added `tool_agent.warm_system_prompt()` and call it from the
FastAPI `lifespan` startup. A single call constructs the shared prompt manager
(bulk-loading every `aegis` prompt into the in-memory frame in one query) and
warms the v2 orchestrator system-prompt cache, so the first user turn no longer
pays the cold prompt load. It never raises (inline fallback stays in effect on DB
failure) and logs `db_backed` so you can confirm whether the DB row was used.
Covered by `test_warm_system_prompt_is_nonfatal_and_caches_on_success`.

(original analysis below)



**Where:** `run_fastapi.py::lifespan` (~55-62),
`utils/prompt_loader.py::_ensure_prompt_manager` (63-65),
`utils/sql_prompt.py` (lazy `SQLPromptManager` construction),
`v2/agent/tool_agent.py::_load_system_prompt` cache.

**Problem (precise):** `SQLPromptManager.__init__` already bulk-loads every
`aegis` prompt into an in-memory DataFrame in one query, and `get_latest_prompt`
filters that frame with no further DB hit. But construction is lazy — it happens
on the *first* `load_prompt_from_db` call, which lands inside the first user
turn, adding cold-start latency there. The v2 system prompt is then cached in a
module global.

**Fix direction:** Warm the prompt manager (and the v2 system-prompt cache) in
the FastAPI `lifespan` startup so the one-time load happens at launch, not on the
first turn. Keep it non-fatal if the DB is unreachable (fallback still works).

---

## F4 — Phase 0 symbol mismatch → silent no-coverage gap (Med-High) — DONE

**Resolution:** Added `_bank_match_keys` in `research.py` and applied it to both
the requested combo and the catalog index in `_split_by_v2_availability`, so a
short ticker (`RY`) matches a catalog `RY-CA` (and vice-versa) instead of
silently preflighting unavailable. Added
`test_check_source_availability_matches_across_ticker_suffix`. For observability,
`resolve_v2_availability` now logs `v2.deep.availability_empty` when a *successful*
catalog read resolves zero coverage, distinct from the existing
`v2.deep.availability_resolve_failed` read-error log — so a scope/format problem is
diagnosable rather than masquerading as genuine no-coverage. (Still untested
against the live catalog; remaining risk is documented.)

(original analysis below)



**Where:** `model/agents/research.py::_split_by_v2_availability`,
`v2/agent/deep.py::resolve_v2_availability` (39-57).

**Problem:** Availability matching intersects `{bank_id, bank_symbol, bank_name}`
(lowercased) against catalog identifiers. A symbol-format mismatch (e.g. `RY` vs
`RY-CA`) yields an empty intersection → every combo preflights unavailable →
controlled "no coverage" gap that *looks* like "found nothing" rather than a
scope/format problem. `resolve_v2_availability` also swallows catalog errors into
an empty index, so a real DB error is indistinguishable from genuine no-coverage.

**Fix direction:** Normalize symbols on both sides before intersection; emit a
distinct log/telemetry signal (and ideally a distinct gap reason) for
"no rows resolved at all" vs "rows resolved but scope unavailable," and don't let
a catalog exception masquerade as no-coverage.

---

## F5 — Multi-research overwrites answer state (Med) — DONE

**Resolution:** Added `_accumulate_chunks` (append + de-dupe by evidence id) and
stopped the cross-modality wipes: the quick path no longer downgrades a deep turn
or nulls `turn_answer_research`, and the deep path no longer empties accumulated
quick chunks. `_known_evidence_ids` now sees every research call's evidence, so
citations to an earlier call survive validation. Covered by
`test_two_research_calls_keep_both_evidence_sets_for_citations`. (Deep-then-deep
still overwrites the single research dict — rare; noted as residual.)

(original analysis below)



**Where:** `v2/orchestrator.py:951-954` (`turn_answer_chunks = …`,
`turn_answer_research = None`), `_known_evidence_ids` (544-563).

**Problem:** Research helpers assign rather than accumulate, and the quick path
wipes deep state. A second `run_research` in one turn clobbers the first; the
scratchpad keeps both results but the shell/known-ids/artifact reflect only the
last, so valid citations to earlier evidence are filtered as hallucinations.

**Fix direction:** Accumulate evidence/research across research calls in the turn
(or union the known-id set across all research results this turn).

---

## F6 — Body-step failure after shell emit → orphaned shell (Med) — DONE

**Resolution:** `_handle_step_failure` now always closes the turn. If no body has
streamed yet it renders synthesis, reusing the already-emitted shell via the new
`existing_shell` arg to `_stream_final_response` (no second
`final_response.started`). If a shell and partial body already streamed, it emits
a final `chat.message` carrying the accumulated `turn_streamed_text` to close the
open stream. Added `turn_streamed_text` accumulation in `_emit_answer_delta`.
Covered by `test_body_failure_after_present_reuses_shell_and_closes_stream`.

(original analysis below)



**Where:** `v2/orchestrator.py::_handle_step_failure` (1199-1215).

**Problem:** Synthesis fallback only fires when `not turn_shell_emitted`. A
failure on the body-authoring step after `present_final_response` already emitted
the shell skips synthesis and emits an error message — leaving tiles with no
body and no `final=true` close.

**Fix direction:** When a shell is already emitted, still close the turn with a
coherent body (synthesis fallback or a graceful final message that reuses the
emitted shell marker).

---

## F7 — Availability path is still the old router (Med) — DONE

**Resolution:** `check_data_availability` is now non-terminal, mirroring the
research feedback path. `_run_availability_turn` still emits the full
tool/widget lifecycle but no longer emits the templated message; instead it sets
`turn_disposition` and stores a compact `_availability_feedback` (coverage rows +
gaps) on the session. The `check_data_availability` branch appends the assistant
tool-call + tool-result to the scratchpad and continues the loop, so the same
agent reads the coverage and authors the reply in its own voice (and can chain
into `run_research` in the same turn — true multi-tool behavior). A hard
availability failure still terminates with an error message. The prompt (YAML +
inline mirror) gained a discipline line telling the agent to summarize coverage
and not call `present_final_response` for coverage answers. The two availability
tests were updated to the non-terminal flow (agent authors the reply; widget
lifecycle preserved). **Note for live test:** the coverage-answer voice is the
part most likely to want prompt tuning once you see real output.

(original analysis below)



**Where:** `v2/orchestrator.py:393-401` (templated message),
`1317-1323` (`outcome.status = "complete"`).

**Problem:** `check_data_availability` is turn-terminal and emits a hardcoded
message; the agent never reads the result. Known deferral in the plan, but it
means "unified single-agent loop" holds only for `run_research`.

**Fix direction:** Make availability a non-terminal loop tool that feeds a
compact coverage summary back so the agent comments on it. Larger change —
may stay `DEFER` for this pass.

---

## F8 — Regex shell not actually fallback-only (Low-Med) — PARTIAL

**Resolution (partial):** Added a clarifying note in `_open_answer_stream`
documenting that the `build_final_shell` (regex) path is reached whenever the
agent skips `present_final_response`, so it is a reachable fallback, not strictly
"fallback-only" as Phase 3 claimed. The substantive decision — whether to force
`present_final_response` via `tool_choice` on research turns — is deliberately
left for live-model data on how reliably the agent calls `present`. Forcing it
blind before the first live test is exactly the risk this review is trying to
avoid. **Action after live test:** measure `present` call rate; if low, force it.

(original analysis below)



**Where:** `v2/orchestrator.py::_open_answer_stream` (622-628).

**Problem:** When the agent goes research → direct answer without calling
`present_final_response` (allowed by `tool_choice: auto`), `build_final_shell`
(regex) builds the tiles. Phase 3 claims the regex path is fallback-only; it
isn't, and the happy-path streaming test exercises it.

**Fix direction:** Either nudge/force `present_final_response` on research turns,
or accept the deterministic shell as a real (documented) path — tighten the
claim and tests accordingly.

---

## F9 — Tiles kept with no evidence backing (Low-Med) — DONE

**Resolution:** In `_shell_from_present_arguments`, a tile that *supplied*
`evidence_ids` but whose ids were all filtered out as hallucinated is now dropped
(strong fabrication signal); tiles that cited nothing are kept as agent-authored.
Covered by `test_tile_with_only_hallucinated_evidence_is_dropped`. (Tile values
are still intentionally agent-reasoned and not literal-matched against evidence —
that is a deliberate design choice, not a bug.)

(original analysis below)



**Where:** `v2/orchestrator.py::_shell_from_present_arguments` (585-599).

**Problem:** A tile is kept even when all its `evidence_ids` are filtered out,
and tile values are never validated against evidence. A hallucinated value with a
hallucinated citation becomes a confident tile with no citation.

**Fix direction:** Optionally drop metric tiles whose evidence_ids all filtered
out (or flag them), per the plan's "require evidence_ids on metric tiles."

---

## F10 — Loop-limit emits a non-final message (Low) — DONE

**Resolution:** The loop-limit backstop still emits `tool.failed` (monitor stage),
but when a shell was already emitted it now closes the open answer stream with a
`final=true` `chat.message` bound to `turn_stream_id` (carrying the shell marker
and any streamed body) instead of a dangling non-final message. The no-shell case
is unchanged. Covered by `test_loop_limit_with_open_shell_closes_the_stream`.

(original analysis below)



**Where:** `v2/orchestrator.py:1131-1148`.

**Problem:** The loop-limit backstop emits `tool.failed` + a non-final
`chat.message`; if `final_response.started` was already emitted, the UI is left
with an unclosed answer stream.

**Fix direction:** Close any open answer stream with a `final=true` message on
the loop-limit path.

---

## F11 — Cleanups (Low) — PARTIAL

**Done:**
- `evidence_ids` local shadow renamed to `raw_evidence_ids` / `valid_evidence_ids`
  in `_shell_from_present_arguments` (done as part of F9).

**Intentionally not changed (documented):**
- **`max_tokens: 1100`** (`tool_agent.py`): kept. The present-tiles step and the
  body step are separate model calls, so 1100 tokens applies to the body alone
  (~800 words) — adequate for an analyst brief. Raising it is a cost/latency
  tuning decision better made with live data.
- **Queue-drain in `_run_streaming_step`**: kept. It is correct (verified) and now
  also carries the F1 buffering. Left as-is to avoid churn before the live test.
- **Prompt caching across loop steps** (Anthropic prompt cache): out of scope; a
  connector-level perf item tracked separately. F3 already removed the cold
  first-turn prompt load.
</content>
</invoke>
