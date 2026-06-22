# V1 Agent Review Checklist

This checklist classifies V1 assets reused or referenced by the V2 first slice.
The goal is to make every temporary V1 dependency explicit before the V2 agent
is hardened.

## Decision Legend

| Decision | Meaning |
| --- | --- |
| keep | Keep the current contract or implementation with normal validation. |
| adapt | Reuse as a starting point, but wrap or rewrite parts for V2 contracts. |
| replace | Do not carry forward; build a V2-native version. |
| defer | Exclude from the first V2 agent path and revisit later. |

## Current Decisions

| Area | V1 asset or behavior | Decision | V2 first-slice status | Required follow-up |
| --- | --- | --- | --- | --- |
| Orchestrator prompt | `aegis_agent/system.yaml` | replace | V2 currently uses lightweight code routing for general, availability, quick, and deep paths. | Create a V2 orchestrator/classifier prompt that knows the V2 event contract, runtime tables, artifacts, model routing, and Quick/Deep semantics. |
| General conversation | V1 agent conversational behavior | adapt | General turns stream through the V2 final response shell, with deterministic fallback only for non-research general answers. | Add conversation history, prior artifacts, and prior research output as structured context. |
| Model routing | V1 static per-source model tiers | adapt | V2 `small` maps orchestrator to medium/research to small; V2 `large` maps orchestrator to large/research to medium. | Validate every V1 source prompt call receives `v2_model_plan` and does not silently use stale source constants. |
| Final response shell | `FinalResponseShell`, summary, metric tiles, shell marker | keep | V2 emits `final_response.started`, then persists the shell marker in the assistant message. | Improve tile extraction from source-backed findings and evidence ids. |
| Research tool schema | V1 `run_research` | adapt | V2 deep search calls `run_research_tool` directly through `v2/agent/deep.py`. | Replace V1 availability assumptions and tighten returned research shape for V2 artifacts/viewer links. |
| Choice cards | V1 `present_choice_card` | defer | V2 deep search asks a plain clarifying question when scope is missing. | Revisit after core research path is stable; likely replace with V2 widget/action contract. |
| Chart tools | V1 `audit_chart_slots`, chart planner, chart markers | defer | Excluded from first V2 slice. | Rebuild after quick/deep research and artifact references are stable. |
| Status reporting | V1 process monitor rows | keep | V2 writes process-monitor-compatible rows from WebSocket events. | Define stable V2 stage names/statuses/payloads. |
| Availability widget | V1 availability concepts | replace | V2 uses catalog-backed `data_source_registry`, `monitored_institutions`, and `data_source_availability` endpoints/widgets. | Deep research still needs to use the V2 catalog availability table instead of V1 `aegis_data_availability`. |
| Runtime persistence | V1-ish chat/process tables | keep | V2 persists user/assistant/tool messages, artifacts, and process stages. | Add stronger widget reload/versioning and source-reference persistence. |
| Report generation | V1 HTML report/chart artifacts | defer | Excluded from first V2 slice. | Rebuild as a separate V2 report-generation workflow. |

## Source Adapter Review

V2 quick search now calls strict V1 primitives directly: query prep, embeddings,
multi-strategy search, rerank, fused scoring, and page/sheet gap-fill. V2 deep
search still goes through the broader V1 `run_research_tool`.

| Source | V1 tables | Prompt assets used by quick | Prompt assets used by deep | Decision | Notes |
| --- | --- | --- | --- | --- | --- |
| `rts` | `aegis-rts-data`, `aegis-rts-embeddings` | `rts/query_prep`, `rts/rerank` | `rts/query_prep`, `rts/rerank`, `rts/research` | adapt | Page-based source. Quick uses V1 page gap-fill. Validate citation page and file id mapping into V2 references. |
| `pillar3` | `aegis-pillar3-data`, `aegis-pillar3-embeddings` | `pillar3/query_prep`, `pillar3/rerank` | `pillar3/query_prep`, `pillar3/rerank`, `pillar3/research` | adapt | Sheet-oriented source in the V1 pipeline. Validate sheet/page naming and regulatory table labels in artifacts. |
| `supplementary_financials` | `aegis-financial-supp-data`, `aegis-financial-supp-embeddings` | `supplementary_financials/query_prep`, `supplementary_financials/rerank` | `supplementary_financials/query_prep`, `supplementary_financials/rerank`, `supplementary_financials/research` | adapt | Sheet-oriented source. Needs special validation for exact metric/table references. |
| `investor_slides` | `aegis-investor-slides-data`, `aegis-investor-slides-embeddings` | `investor_slides/query_prep`, `investor_slides/rerank` | `investor_slides/query_prep`, `investor_slides/rerank`, `investor_slides/research` | adapt | Page-based source. Validate slide/page references and image-like chart/table extraction behavior. |
| `transcripts` | `aegis-earnings-transcripts-data`, `aegis-earnings-transcripts-embeddings` | `transcripts/query_prep`, `transcripts/rerank` | `transcripts/query_prep`, `transcripts/rerank`, `transcripts/research` | adapt | V2 quick now uses the V1 processed transcript tables, not the older direct `aegis_transcripts` shortcut. Validate transcript unit labels in references. |
| `event_transcripts` | `aegis-event-transcripts-data`, `aegis-event-transcripts-embeddings` | `event_transcripts/query_prep`, `event_transcripts/rerank` | `event_transcripts/query_prep`, `event_transcripts/rerank`, `event_transcripts/research` | adapt | Must report unavailable/missing source data as a controlled gap when the table/data is absent. No fallback retrieval path. |

## Prompt Asset Review

| Prompt family | Decision | Runtime status | Required follow-up |
| --- | --- | --- | --- |
| `aegis_agent/system` | replace | Not used directly by V2 orchestration. | Write V2-native orchestrator/classifier prompt. |
| Per-source `query_prep` | adapt | Used by quick and deep research through V1 pipeline functions. | Rewrite once V2 shared retrieval table/schema is finalized; keep strict no-fallback behavior. |
| Per-source `rerank` | adapt | Used by quick and deep research through V1 pipeline functions. | Validate candidate formatting and removal floors per source. |
| Per-source `research` | adapt | Used only by deep research. | Normalize output into V2 `EvidenceChunk`/finding/reference contracts and artifact references. |
| Per-source `content_extraction`, `doc_metadata`, `section_summary`, OCR prompts | defer | Not part of V2 runtime query path. | Revisit when rebuilding ingestion and shared retrieval tables. |
| Legacy `aegis/router`, `planner`, `response`, `summarizer`, clarifiers | replace | Not used by the current V2 path. | Fold any useful behavior into the V2 orchestrator prompt or typed tools. |
| ETL/report/key-theme prompts | defer | Not part of first V2 research slice. | Review when report generation returns. |
| `chart_planner` | defer | Not used by current V2 path. | Rebuild after chart/report scope is reintroduced. |

## Tool And Runtime Contract Review

| Contract | Decision | V2 status | Required follow-up |
| --- | --- | --- | --- |
| Incoming `/api/v2/ws` payload normalization | keep | Accepts current and transition names for filters, optional context, model, and search. | Document final names once UI stops sending compatibility aliases. |
| `research_depth: short/long` compatibility | keep temporarily | Normalizes to `quick/deep`. | Remove only after all frontend callers use `search_selection`. |
| `Quick` / `Deep` search modes | keep | UI and backend support the intended labels. | Add integration tests for actual WebSocket payloads from the UI. |
| `tool.started`, `tool.progress`, `tool.completed`, `tool.failed` | adapt | Emitted and persisted as process monitor rows. | Define stable stage taxonomy and payload schemas. |
| `chat.delta` | keep | Streams final assistant body text. | Add reconnect/replay behavior if streaming is interrupted. |
| `final_response.started` | keep | Sends summary/metric shell before body streaming. | Strengthen metric tiles with evidence-backed extraction. |
| `widget.created/completed/failed` | keep | Availability widget is streamed and completed widgets are persisted as hidden tool messages. | Backfill/migrate older conversations without widget markers if needed. |
| Hidden widget marker | keep | Uses `<aegis_widget>...</aegis_widget>` in persisted tool messages. | Add schema version inside marker payload. |
| `artifact.created/updated` | keep | Persists quick/deep artifacts to the runtime artifact table. | Add source reference payloads that can open exact source docs/pages/sheets. |
| `preview.open` | defer | Defined in schema but not central to current backend flow. | Use only after source-document links are live. |

## Artifact Path Review

| Artifact path | Decision | V2 status | Required follow-up |
| --- | --- | --- | --- |
| Quick research artifact | adapt | Groups retained chunks by source in self-contained HTML. | Improve grouping by file/page/sheet/section and include viewer-ready source links. |
| Deep research artifact | adapt | Converts V1 findings/citations into self-contained HTML. | Preserve structured findings, gaps, citations, and evidence refs more faithfully. |
| Availability widget as persisted message | keep | Reloads completed widgets from hidden chat messages. | Add migration/regeneration path for old conversations that predate widget persistence. |
| V1 report HTML artifacts | defer | Not generated by first V2 slice. | Rebuild as separate report workflow. |
| V1 chart artifacts/slots | defer | Not generated by first V2 slice. | Rebuild after metric extraction and chart contracts are stable. |

## Confirmed Gaps To Carry Forward

1. Deep research still checks V1 `aegis_data_availability`; it should use the V2 catalog availability schema.
2. V2 has no V2-native orchestrator prompt yet; routing is currently code heuristics.
3. Quick research is strict and scoped, but live per-source validation is still needed.
4. Source-document viewer links need a stable reference payload from both quick chunks and deep findings.
5. Process monitor stage names are functional but not yet a final taxonomy.
6. Old widget-less conversations either need migration or accepted non-reload behavior.
7. Chart/report generation remains intentionally deferred.

## Recommended Next Implementation Order

1. Replace the deep-research availability bridge with V2 catalog availability reads.
2. Add a V2-native orchestrator/classifier prompt and tests for general vs availability vs quick vs deep routing.
3. Build a source-adapter validation harness for all six sources using one known bank/period/query.
4. Upgrade quick/deep artifact reference payloads so source links open exact documents in the viewer.
5. Formalize process monitor stage names and payload schemas.
