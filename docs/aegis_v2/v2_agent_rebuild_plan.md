# V2 Agent Rebuild Plan (Phases 1-4)

This plan rebuilds the V2 agent so it regains the unified, conversational,
"feels like one agent" behavior of V1 while keeping every V2 contract that is
worth keeping (typed websocket events, artifacts, catalog availability widgets,
model tiers, structured turn normalization, and runtime persistence).

It is sequenced so each phase is independently shippable and testable. Phase 0
is the prerequisite reliability fix (deep research currently errors) and is
specified in full below. Phase 5 (charts) is intentionally deferred and excluded
from this plan.

## Problem Statement

The current V2 turn is a one-shot router, not an agent:

- `run_agent_step` makes a single non-streaming `complete_with_tools` call and
  returns one `AgentDecision`
  (`aegis-agent/src/aegis_agent/v2/agent/tool_agent.py`).
- `_run_agent_loop` advertises `MAX_AGENT_STEPS = 3`, but every branch in
  `_run_agent_tool_call` sets a terminal status (`complete` / `awaiting_user` /
  `error`), so it never iterates (`aegis-agent/src/aegis_agent/v2/orchestrator.py`).
- The decision model never sees tool results. The answer is produced by a
  separate prompt in `stream_synthesis`, and metric tiles are extracted by a
  regex (`METRIC_VALUE_RE`) in `build_final_shell`
  (`aegis-agent/src/aegis_agent/v2/agent/final_response.py`).
- The system prompt is a hardcoded string literal in `_agent_messages` rather
  than a DB-backed prompt, so V1's conversational judgment was dropped, not
  ported.

The result: templated, keyword-driven responses that do not feel like one agent,
plus tile values that are guessed by regex rather than reasoned from evidence.

## Target Architecture

One streaming, tool-calling agent owns the whole turn, mirroring V1's
`run_aegis_agent` loop but emitting V2 typed events and driving V2
artifacts/widgets.

```
run_turn
  -> normalize_turn + load_conversation_context        (unchanged)
  -> run_agent_loop (streaming, tool-results-fed-back)  (Phase 1)
       step: stream_with_tools
         - assistant text deltas  -> chat.delta         (Phase 2)
         - tool_calls accumulated -> dispatch tool      (Phase 1)
       tool results appended to messages, loop continues (Phase 1)
       present_final_response tool -> final_response.started + body stream (Phase 3)
  -> system prompt loaded from DB                        (Phase 4)
```

Invariants preserved across all phases (consumed by `_persist_stream_event` in
`api.py` and by `conversation.py` on reload):

| Event | Must still be emitted | Notes |
| --- | --- | --- |
| `tool.started` / `tool.progress` / `tool.completed` / `tool.failed` | yes | Persisted as process monitor stages. |
| `widget.created` / `widget.completed` / `widget.failed` | yes | Availability + clarification widgets. |
| `artifact.created` / `artifact.updated` | yes | Quick/deep artifacts. |
| `final_response.started` | yes | Carries the shell JSON before the body. |
| `chat.delta` | yes | Streams assistant body text. |
| `chat.message` (`final=true`) | yes | Persisted as `final_shell_marker(shell) + body`. |

The connector already supports both call styles needed here:
`stream_with_tools` and `complete_with_tools`
(`aegis-agent/src/aegis_agent/connections/llm_connector.py`). No new connector
work is required.

---

## Phase 0 - Make deep research complete (availability fix)

**Goal:** Stop deep research from erroring by routing its availability preflight
to the V2 catalog tables and reporting missing coverage as a controlled gap
instead of a thrown exception. This is the single change that makes deep research
reliable, and it is a prerequisite for proving the loop in Phases 1-3.

**Two confirmed root causes (both in the deep path):**
1. The deep path runs V1 `run_research_tool`, which calls
   `check_source_availability` and `SELECT ... FROM aegis_data_availability`
   (`aegis-agent/src/aegis_agent/model/agents/research.py`, ~line 161). V2's
   canonical availability is the catalog: `data_source_availability` JOIN
   `monitored_institutions` (`aegis-agent/src/aegis_agent/v2/tools/catalog.py`,
   `optional_context`). If `aegis_data_availability` is absent/empty in the V2
   database, every bank-period preflights as unavailable, yielding empty/failed
   research.
2. Even where a V2 `aegis_data_availability` table exists, the V1 preflight
   `SELECT` includes a `bank_aliases` column that the V2 table does not have
   (compare the columns read by `v2/tools/availability.py::check_data_availability`),
   so the query can fail outright with a SQL error. The thrown error is then
   surfaced as the "research doesn't complete" failure.

**Primary files:**
- `v2/agent/deep.py` - resolve availability against the V2 catalog before/around
  the V1 research call, and pass resolved scope through.
- `model/agents/research.py` - introduce a seam so V2 deep runs skip the V1
  `aegis_data_availability` preflight and use the injected V2 result.
- `v2/tools/catalog.py` - reuse `optional_context` (or factor a small shared
  resolver) that maps catalog `data_source_list` to V2 source ids.

**Design (recommended - Option C, a thin seam):**
1. In the V2 deep adapter, call `optional_context` with the turn's filters to get
   live catalog coverage (`DataAvailabilityResponse.rows` + `.missing`).
2. Compute available `(bank_symbol, fiscal_year, quarter, source_id)`
   combinations from those rows.
3. Pass the resolved availability into the research context (e.g.
   `context["v2_available_combinations"]`). In `check_source_availability`, when
   that key is present, split requested combinations using it and **do not query
   `aegis_data_availability`** - the V1 legacy path stays intact for V1 callers.
4. If no requested combination is available, emit a controlled "no coverage" gap
   and a normal completed (empty) research result, not an exception. The
   orchestrator already renders gaps via `deep_research_html` and the gap section.

**Alternatives considered:**
- Option A: rewrite `check_source_availability` itself to read the catalog
  tables. Fewer moving parts but edits shared V1 code and risks V1 regressions.
- Option B: precompute available combinations in the deep adapter and only send
  available ones to `run_research_tool`, bypassing its preflight entirely. Clean
  isolation; loses the V1 partial-availability status messaging. Acceptable if
  the controlled-gap output is preserved.

Option C is preferred because it keeps V1 callers unchanged while making V2 deep
research authoritative on the catalog.

**Note on quick research:** quick retrieval (`v2/agent/retrieval.py`) does not run
an availability preflight - it builds bank-period combinations and searches
directly - so this fix is scoped to the deep path only.

**Risks / mitigations:**
- Editing shared V1 research code -> gate the new behavior on the injected key so
  legacy V1 behavior is byte-for-byte unchanged when the key is absent.
- Catalog vs research bank-symbol mismatch -> normalize symbols through the same
  canonical mapping used elsewhere (`monitored_institutions.bank_ticker`).

**Tests:**
- Deep research with available scope returns findings (no `aegis_data_availability`
  query issued on the V2 path).
- Deep research with unavailable scope returns a controlled gap and a completed
  (non-error) result, not an exception.
- V1 callers (legacy path) still hit `aegis_data_availability` unchanged.

**Done when:** deep research completes for available scope and degrades to a
controlled gap for unavailable scope, with no `aegis_data_availability` dependency
on the V2 path.

---

## Phase 1 - Re-unify the agent loop

**Goal:** Replace the one-shot router with a real streaming tool-calling loop
that feeds tool results back to the same model and lets it decide the next step.

**Primary files:**
- `v2/agent/tool_agent.py` - convert `run_agent_step` (one shot) into a streaming
  step generator; reuse the delta-accumulation pattern from V1
  `_stream_model_step` / `_tool_call_from_delta`.
- `v2/orchestrator.py` - rewrite `_run_agent_loop` / `_run_agent_decision` /
  `_run_agent_tool_call` so tool execution is a loop continuation, not a turn end.

**Design:**
1. Build the message list once per turn: system prompt (Phase 4), a structured
   `turn` payload (existing `_turn_payload`), and prior context
   (`ConversationContext.to_prompt_text`).
2. Each loop iteration calls `stream_with_tools` and accumulates both content
   deltas and `tool_calls` deltas (port `_tool_call_from_delta` and the
   accumulation block from V1 `_stream_model_step`).
3. After a step:
   - If the model emitted tool calls: emit `tool.started`, run the tool
     (availability / quick / deep, reusing existing `_run_availability_turn`,
     `_run_quick_research_turn`, `_run_deep_research_turn`), emit the existing
     tool/artifact/widget events, then append an OpenAI `role: "tool"` message
     with a compact JSON result and **continue the loop**.
   - If the model emitted only content: that content is the user-facing answer
     (handled in Phase 2/3); end the turn.
4. Keep `ask_clarification` terminal (`awaiting_user`) - that is correct loop
   behavior, since the turn genuinely waits for the user.
5. Keep a bounded loop (raise `MAX_AGENT_STEPS` from 3 to ~6 to match V1's
   `MAX_AGENT_LOOPS`) with the existing loop-limit error event as the backstop.

**Key contract change:** research tools stop being turn-terminal. They append a
tool result and the agent re-enters the loop with that result in context. This
single change is what restores agentic behavior; everything else in Phase 1 is
plumbing.

**Tool result shape fed back to the model (compact, not raw HTML):**
- availability: row count, missing count, and a short coverage summary.
- quick: retained chunk count, per-source counts, gaps, and the evidence list
  with `source:chunk_id` ids and trimmed text.
- deep: status, findings (with metric + evidence refs), and gaps.

**Risks / mitigations:**
- Intermediate "narration" leaking as chat text -> mitigate in the system prompt
  ("either call exactly one tool or write the final answer; do not narrate
  between tool calls"). Unlike V1, we can stream content whenever it appears
  because clarifications and direct answers should stream too.
- Token growth across loops -> append compact tool results, not raw artifacts.

**Tests:**
- A turn that runs research then continues to a model step that authors the
  answer (assert research is not turn-terminal).
- A multi-tool turn (availability -> research) within the step budget.
- Loop-limit backstop still emits the existing failure event.

**Done when:** a scoped research turn runs the tool, the same agent reads the
result, and the agent (not a separate synthesizer) produces the final assistant
message, with all existing events still emitted in order.

**Implementation status: landed.**
- `run_research` is now non-terminal. After retrieval, the orchestrator appends
  an assistant tool-call message plus a compact, citation-ready tool result
  (`source:chunk_id` evidence ids + trimmed text) to a per-turn
  `state.turn_scratchpad`, then continues the loop. The same agent reads it back
  through `run_agent_step(..., scratchpad=...)` and authors the answer.
- The separate `stream_synthesis` call was removed from the research path; the
  agent-authored content is emitted with the deterministic shell via
  `_stream_research_answer` (`final_response.started` -> `chat.delta` ->
  `chat.message` with `final_shell_marker(shell) + body`).
- Step budget raised from 3 to 6.
- Covered by `test_research_with_explicit_text_scope_runs_quick_search`,
  `test_research_feeds_evidence_back_with_citation_ids`, and the existing
  loop replan/limit tests.

**Scoping decisions (deferred into Phase 2):**
- Still uses `complete_with_tools` (buffered), not `stream_with_tools`. The agent
  authors the full answer, emitted as one `chat.delta`. Live per-token streaming
  is moved to Phase 2, where it actually reaches the UI; the loop topology and
  feedback (the architectural win) land here without the streaming-accumulation
  risk.
- `stream_synthesis` / `_stream_final_response` are retained as the Phase 2
  fallback foundation, not yet wired as a guarded fallback.
- `check_data_availability` and `ask_clarification` remain turn-terminal for now.
  Availability is the documented loop-tool target, but folding it into the loop
  (so the agent comments on coverage) is a small follow-up; research was the
  high-value non-terminal change because that is where templated synthesis lived.

---

## Phase 2 - Let the agent author the answer

**Goal:** The model that read the evidence writes the brief in its own voice,
streamed as `chat.delta`. Remove `stream_synthesis` as the primary answer path.

**Primary files:**
- `v2/orchestrator.py` - stream the agent's own content deltas as `chat.delta`
  during the answer step; drop the separate `_stream_final_response` ->
  `stream_synthesis` call for the normal path.
- `v2/agent/final_response.py` - demote `stream_synthesis` to a fallback used
  only if the agent step fails to produce a body; keep `_chunk_prompt`,
  `final_shell_marker`, and the shell models.

**Design:**
1. The evidence/research result is already in the loop's message context (Phase
   1), so the answer step needs no second retrieval and no second prompt.
2. Preserve the citation contract: instruct the agent to cite source-backed
   claims with the exact `[[source:chunk_id]]` ids already defined by
   `evidence_id_for_chunk`, the quick artifact, and the deep evidence refs.
3. Persist the final message exactly as today: `final_shell_marker(shell) + body`
   so `conversation.py::parse_final_response` keeps working on reload.

**Risks / mitigations:**
- Fallback divergence -> keep `stream_synthesis` behavior identical to today so a
  failed agent body still yields a coherent answer.
- Hallucinated citations -> the agent only has the supplied evidence in context;
  add a test asserting every `[[...]]` id in the body exists in the evidence set.

**Tests:**
- Quick and deep answers stream from the agent step and contain only valid
  evidence ids.
- Fallback path triggers and still emits a final `chat.message`.

**Done when:** the streamed answer is authored by the agent loop in one voice,
citations resolve, and `stream_synthesis` is only a guarded fallback.

**Implementation status: landed.**
- `run_agent_step` now streams via `stream_with_tools`, accumulating tool-call
  deltas and forwarding content tokens to an `on_delta` callback. The
  orchestrator bridges that callback through a queue in `_run_streaming_step`
  and emits live `chat.delta` events (`_emit_answer_delta`), opening the answer
  with `final_response.started` (research-backed) or the `tool.completed`
  agent-decision marker (general) on the first token, then persists the final
  `chat.message` in `_finish_answer`.
- `stream_synthesis` / `_stream_final_response` is now a **wired fallback**:
  `_handle_step_failure` renders it when the agent step raises after research
  has run, and `_finish_answer` uses it when the agent yields an empty body.
- Citation contract preserved end-to-end (the fed-back evidence carries
  `source:chunk_id` ids; the agent cites them in the streamed body).
- Covered by `test_research_answer_streams_tokens_live` (tokens stream after the
  shell, in order) and `test_answer_step_failure_falls_back_to_synthesis`, plus
  the rewritten streaming `test_tool_agent.py` (direct-answer streaming, tool-call
  parsing, split-argument-delta accumulation).

**Design note — `on_delta` over a generator-typed `run_agent_step`.** The agent
step stays a coroutine returning `AgentDecision`, streaming tokens via the
optional `on_delta` callback rather than becoming an async generator. This keeps
the orchestrator's decision dispatch intact and lets non-streaming callers/tests
omit the callback; the orchestrator bridges deltas to the event stream with the
proven V1 queue-drain pattern.

**Deferred to later phases:** tiles are still the Phase 1 deterministic
`build_final_shell` (Phase 3 replaces them with the `present_final_response`
tool); availability/clarification remain terminal.

---

## Phase 3 - Structured tiles via a tool, not regex

**Goal:** Replace regex/templated tile extraction with model-authored tiles whose
structure is enforced by a tool schema - accurate (grounded in the evidence the
agent just read) and robust (no brittle inline-JSON-marker parsing, no regex).

**Primary files:**
- `v2/agent/tool_agent.py` - add a `present_final_response` tool whose parameter
  schema is the `FinalResponseShell` (summary headline/dek/eyebrow, up to 4
  tiles with label/value/context/evidence_ids, body_style).
- `v2/orchestrator.py` - on a `present_final_response` tool call: validate the
  shell, emit `final_response.started` with it, append a tool result instructing
  the agent to write the body, then stream the body (Phase 2) on the next step.
- `v2/agent/final_response.py` - keep `build_final_shell` only as a deterministic
  fallback when validation fails or the agent does not call the tool; delete
  `METRIC_VALUE_RE` from the primary path.

**Why a tool call instead of V1's inline marker:** V1 asked the model to
hand-author `<aegis_final_shell>{json}</aegis_final_shell>` inside the same token
stream as the prose, which was brittle (your "tiles filled in wrong" symptom).
A tool call gives schema-validated structured output and separates the structured
shell from the prose body, while still producing both from the same agent in the
same turn.

**Flow on a research turn:**
1. Research tool returns; result appended (Phase 1).
2. Agent calls `present_final_response` with tiles grounded in the evidence.
3. Orchestrator emits `final_response.started`, appends a tool result
   ("shell accepted; now write the analyst brief body with `[[id]]` citations").
4. Agent streams the body (Phase 2); persist `marker + body`.

**Risks / mitigations:**
- Extra model round-trip per research turn -> acceptable (one short, schema-bound
  step on the smaller research/orchestrator tier); only on research turns.
- Agent skips the tool -> deterministic `build_final_shell` fallback keeps tiles
  populated; assert the shell is always present before the body streams.
- Tiles must stay evidence-backed -> require `evidence_ids` on metric tiles and
  validate they exist in the evidence set.

**Tests:**
- Tiles come from `present_final_response`, carry valid `evidence_ids`, and match
  values present in the evidence (not regex guesses).
- Missing/invalid tool call falls back to deterministic tiles without breaking
  `final_response.started`.

**Done when:** tiles are authored by the agent under a validated schema with
evidence ids, and the regex path is fallback-only.

**Implementation status: landed.**
- Added a `present_final_response` tool (`tool_agent.py`) whose schema is the
  final shell (headline, dek, up to four tiles with label/value/context/
  evidence_ids). The prompt now instructs: after `run_research`, call
  `present_final_response`, then write the body.
- The orchestrator handles the tool in `_run_agent_tool_call`: it builds a
  validated `FinalResponseShell` via `_shell_from_present_arguments`, **filters
  each tile's `evidence_ids` against the ids actually retrieved this turn**
  (`_known_evidence_ids`) so hallucinated citations drop, emits
  `final_response.started` with the agent shell, appends a "write the body" tool
  result, and continues. The body then streams (Phase 2) and persists with the
  agent shell's marker.
- `build_final_shell` (and its `METRIC_VALUE_RE` regex) is now reached only from
  the two fallback paths - `_open_answer_stream` (agent skipped the tool) and
  `_stream_final_response` (agent step failed) - so the regex is off the primary
  path.
- The answer-stream state (`turn_stream_id`, `turn_shell`, `turn_shell_emitted`,
  `turn_streamed_any`) moved from per-step to per-turn reset so one stable
  `stream_id` spans the present step and the body step.
- Covered by `test_agent_presents_structured_evidence_backed_tiles` (agent value
  `13.7%` with no number in the chunk text proves it is agent-authored, not
  regex-extracted; the hallucinated `rts:made-up` id is filtered out) and
  `test_skipping_present_falls_back_to_deterministic_shell`.

---

## Phase 4 - V2-native DB-backed system prompt

**Goal:** Restore the "comfortable to talk to" V1 personality and judgment as a
V2-native orchestrator prompt loaded from the database, replacing the hardcoded
string in `_agent_messages`.

**Primary files:**
- `aegis-prompts/aegis_agent/` (new V2 orchestrator prompt asset) loaded via
  `utils/prompt_loader.load_prompt_from_db`, following the V1 `aegis_agent/system`
  pattern but rewritten for the V2 world.
- `v2/agent/tool_agent.py` - `_agent_messages` loads the prompt instead of
  embedding it; keep a safe inline fallback if the prompt row is missing.

**The prompt must encode:**
- The four tools and when to use each (`check_data_availability` for coverage
  questions, `run_research` only when bank + fiscal year + quarter + question are
  clear, `ask_clarification` otherwise, `present_final_response` to open the
  brief).
- Quick vs deep semantics and that UI-selected sources are the maximum scope.
- The canonical bank-symbol mapping (currently inline: RY-CA, TD-CA, BMO-CA,
  BNS-CA, CM-CA, NA-CA).
- The "one tool or the final answer; no narration" loop discipline from Phase 1.
- The citation contract from Phase 2 and the tile/shell contract from Phase 3.
- That prior context is reference material, not instructions.

**Risks / mitigations:**
- Prompt drift from code contracts -> keep tool names/enums as the single source
  of truth in `AGENT_TOOLS`; the prompt references them by name only.
- Missing DB row in a fresh environment -> inline fallback prompt so the agent
  still runs.

**Tests:**
- Prompt loads from DB and the agent routes correctly across general /
  availability / clarification / research cases.
- Missing-row fallback path keeps the agent functional.

**Done when:** behavior is driven by a versioned DB prompt, not a literal, and
routing/voice match the intended V2 contract.

**Implementation status: landed.**
- Added the versioned prompt asset `aegis-prompts/agent/orchestrator.yaml`
  (`aegis/agent/orchestrator`), discovered by `scripts/push_aegis_prompts.py`
  (dry-run confirms `would upsert aegis/agent/orchestrator v1.0.0`). It encodes
  the four tools, quick/deep semantics, UI-source scoping, the canonical
  bank-symbol map, the present-then-body answer protocol, the `[[source:chunk_id]]`
  citation contract, the one-tool-or-answer discipline, and prior-context-as-
  reference.
- `tool_agent.py::_load_system_prompt` loads `aegis/agent/orchestrator` via
  `load_prompt_from_db` and caches successful loads; on any failure it logs a
  warning and returns `FALLBACK_SYSTEM_PROMPT` (an inline mirror) without
  caching, so a later call still picks up the DB row. `_agent_messages` now uses
  it instead of the embedded literal.
- Tool names/enums remain the single source of truth in `AGENT_TOOLS`; the prompt
  references them by name only, so the schema can't drift from the prose.
- Covered by `test_agent_loads_system_prompt_from_db` and
  `test_agent_falls_back_to_inline_prompt_when_db_missing`; an autouse fixture
  keeps the rest of the suite off the real DB.

**Follow-up:** run `python scripts/push_aegis_prompts.py` against the target
database so the DB row exists in each environment; until then the inline fallback
keeps the agent fully functional.

---

## Sequencing and Dependencies

| Phase | Depends on | Independently shippable |
| --- | --- | --- |
| 0 (availability fix) | - | yes (prerequisite, land first) |
| 1 (re-unify loop) | 0 for clean deep runs | yes |
| 2 (agent authors answer) | 1 | yes |
| 3 (structured tiles) | 1, 2 | yes |
| 4 (DB prompt) | 1 | yes (can land anytime after 1) |

Recommended order: 0 -> 1 -> 2 -> 3, with 4 landing alongside or right after 1.
Phases 3 and 4 are the previously-burned, higher-risk surfaces (structured
output and prompt behavior); they come last, only after the loop they depend on
is proven.

## Out of Scope

- Phase 5 charts / report generation (still deferred per the checklist).
- Source-document deep-link reference payloads (tracked separately in the gap
  backlog).
- Process-monitor final taxonomy and token/cost accounting (separate item).
