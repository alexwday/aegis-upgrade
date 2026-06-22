# Aegis V2 Runtime API Contract

Status: `aegis.v2.runtime.v1` draft, aligned to the current `/api/v2/ws`
implementation.

This contract describes what the V2 UI sends to Aegis, what Aegis streams back,
and which REST endpoints the UI uses to hydrate catalog/runtime state. The
current transport is WebSocket-first. The older planned `POST /api/v2/query`
plus SSE stream is deferred and should not be built against for this slice.

## REST Hydration

| UI event | Endpoint | Purpose |
| --- | --- | --- |
| Open or refresh `/v2` | `GET /api/v2/bootstrap?user_id=...&conversation_id=...` | Load active conversation, `messages`, hydrated `chat_items`, and recent artifacts. |
| Open filters | `GET /api/v2/data-sources` | Load source registry rows for filter controls. |
| Open optional context | `GET /api/v2/optional-context` | Load bank, period, and source availability options. Supports `sources`, `banks`, `bank_categories`, `years`, `quarters`, `keyword`, and `limit`. |
| Refresh documents | `GET /api/v2/documents` | Load previewable source documents for the current filters. |
| Open existing conversation | `GET /api/v2/conversations/{conversation_id}` | Load one persisted conversation, raw `messages`, hydrated `chat_items`, and artifacts. |
| Refresh artifact strip | `GET /api/v2/conversations/{conversation_id}/artifacts` | Load artifacts newest first. |
| Open artifact | `GET /api/v2/artifacts/{artifact_id}` | Load artifact HTML for the viewer. |
| Open source document | `GET /source-documents/{source}/{file_id}/preview` | Render source document preview in the viewer. |
| Download source document | `GET /source-documents/{source}/{file_id}/download` | Download original source bytes. |

## WebSocket

Connect to `ws://{host}/api/v2/ws`. The server immediately emits
`session.ready`.

### Canonical Client Message

```json
{
  "user_id": "00000000-0000-0000-0000-000000000001",
  "conversation_id": "230029be-7349-469c-adba-8b432f4388d4",
  "query": "Compare CET1 for RBC and TD in Q1 2026",
  "filters": {
    "data_sources": ["rts", "pillar3"],
    "bank_tickers": ["RY", "TD"],
    "fiscal_years": [2026],
    "quarters": ["Q1"]
  },
  "optional_context": {
    "bank_tickers": ["RY", "TD"],
    "fiscal_years": [2026],
    "quarters": ["Q1"]
  },
  "model_selection": "small",
  "search_selection": "quick"
}
```

Canonical values:

| Field | Required | Values |
| --- | --- | --- |
| `user_id` | Yes | UUID string. |
| `conversation_id` | No | Existing UUID string. Omit/null to create or use current conversation. |
| `query` | Yes | User-visible request text. |
| `filters.data_sources` | No | Source registry `data_source_name` values. |
| `filters.bank_tickers` | No | Bank ticker symbols. |
| `filters.fiscal_years` | No | Integer fiscal years. |
| `filters.quarters` | No | `Q1`, `Q2`, `Q3`, `Q4`; numeric `1`-`4` normalize server-side. |
| `optional_context` | No | Same shape as `filters`; used as scoped prompt/retrieval context. |
| `model_selection` | Yes | `small` or `large`. Medium is internal only. |
| `search_selection` | Yes | `quick` or `deep`. |

Transition compatibility remains in the backend:

| Old/current frontend field | Canonical field |
| --- | --- |
| `content` | `query` |
| `filters.source_ids` | `filters.data_sources` |
| `filters.bank_symbols` | `filters.bank_tickers` |
| `context` | `optional_context` |
| `preferences.research_depth = short` | `search_selection = quick` |
| `preferences.research_depth = long` | `search_selection = deep` |

New UI work should prefer canonical fields. Compatibility fields should only be
sent while the frontend is transitioning.

## Event Envelope

Every server event uses this envelope:

```json
{
  "type": "chat.delta",
  "session_id": "session_...",
  "payload": {},
  "event_id": "event_...",
  "timestamp": "2026-06-22T18:00:00Z"
}
```

The API layer adds `payload.conversation_id` and `payload.run_uuid` before
sending events to the UI. The same `run_uuid` links chat messages, artifacts,
and process monitor rows.

## Stream Events

| Event | Payload | UI behavior |
| --- | --- | --- |
| `session.ready` | `{ "session_id": "..." }` | Mark WebSocket connected. |
| `tool.started` | `{ "tool_id", "name", ... }` | Remove thinking state and show status. |
| `tool.progress` | `{ "tool_id", "name", "message", ... }` | Update status/progress text. |
| `tool.completed` | `{ "tool_id", "name", ... }` | Mark tool stage complete. |
| `tool.failed` | `{ "tool_id", "name", "error" }` | Show failure message/status. |
| `widget.created` | `{ "widget": HtmlWidget }` | Insert or update chat widget. |
| `widget.updated` | `{ "widget": HtmlWidget }` | Update existing widget. |
| `widget.completed` | `{ "widget": HtmlWidget }` | Render completed widget and persist it for reload. |
| `widget.failed` | `{ "widget": HtmlWidget }` | Render failed widget and persist it for reload. |
| `artifact.created` | `{ "artifact": Artifact }` | Add artifact tile and persist artifact. |
| `artifact.updated` | `{ "artifact": Artifact }` | Replace artifact tile/payload. |
| `final_response.started` | `{ "stream_id", "shell": FinalResponseShell }` | Render executive summary and metric tiles shell. |
| `chat.delta` | `{ "stream_id", "role": "assistant", "content" }` | Append streamed assistant body text. |
| `chat.message` | `{ "role", "content", "message_id?", "stream_id?", "final?" }` | Persisted final/non-stream message. Final messages include the hidden final shell marker for reload. |
| `preview.open` | `{ "kind", "title", ... }` | Open a viewer target. Currently reserved; source/artifact links are mostly handled by REST routes. |

## Route Behavior

General conversation:

1. `tool.completed` with `name = classify_turn` and
   `decision = general_conversation`.
2. `final_response.started`.
3. Zero or more `chat.delta`.
4. Final `chat.message` with `final = true`.

Availability:

1. `tool.started` with `name = check_data_availability`.
2. `widget.created`.
3. `tool.completed` or `tool.failed`.
4. `widget.completed` or `widget.failed`.
5. Assistant `chat.message`.

Quick research:

1. `tool.started` with `name = quick_research`.
2. `tool.progress` with retained chunk/gap status.
3. `artifact.created` with `kind = quick_search`.
4. `tool.completed`.
5. `final_response.started`.
6. Zero or more `chat.delta`.
7. Final `chat.message` with `final = true`.

Deep research:

1. If bank, fiscal year, or quarter scope is missing, Aegis sends a clarifying
   `chat.message` and does not run broad research.
2. Otherwise the stream mirrors quick research with `name = deep_research` and
   `kind = deep_search`.

Research failures do not fall back to another research path. Quick uses quick,
deep uses deep, and errors are surfaced as `tool.failed` plus an assistant
failure message.

## Persisted Reload Contract

`GET /api/v2/bootstrap` and `GET /api/v2/conversations/{conversation_id}`
return both:

- `messages`: raw persisted chat rows for backwards compatibility.
- `chat_items`: hydrated display rows. This is the preferred UI source.

`chat_items` shape:

```json
[
  { "type": "message", "message": { "id": "...", "role": "user", "content": "..." } },
  { "type": "widget", "widget": { "id": "widget_...", "kind": "data_availability", "title": "Data Availability", "status": "complete", "html": "...", "data": {}, "actions": [] } }
]
```

Widgets only reload if the original chat history contains the full persisted
widget payload. Older rows that only have process-monitor summaries cannot be
losslessly reconstructed.

## Versioning Rules

- Additive payload fields are allowed.
- Event `type`, canonical request enum values, and top-level REST response
  fields require a contract update.
- Frontend should ignore unknown additive payload keys.
- Backend should keep transition aliases until the UI only sends canonical
  fields and the backlog explicitly removes compatibility.
