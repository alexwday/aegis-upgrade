# Aegis Runtime Schema Field Map

Generated from the CSV files in this folder. Edit the CSVs, then rerun `python3 scripts/render_runtime_schema_md.py` from the repo root.

## Section Notes

| Section | Notes |
| --- | --- |
| Identity & Relationship Keys | Stable row anchors and foreign-key links shared across schemas. |
| UI Labels & Descriptors | Human-facing names descriptions and labels used by filters context selectors chat rows and viewer tiles. |
| Period & Availability | Fiscal year quarter and coverage fields that determine which sources are available for each bank. |
| Storage & Content | Source file identifiers chunk text enrichment JSON and embedding vectors used by retrieval. |
| Query & Conversation Runtime | User session query and chat-message state used during active conversations. |
| Generated Outputs & Viewer Artifacts | Agent-generated HTML reports widgets and viewer targets surfaced by the UI. |
| Process Monitor & Agent State | Query run stage events status updates and operational visibility. |
| Prompt & Tool Configuration | Prompt inventory versioning and tool routing configuration. |
| State & Audit | Lifecycle status timestamps and operational audit fields used for ordering debugging and maintenance. |

## Catalog


### Identity & Relationship Keys

Stable row anchors and foreign-key links shared across schemas.

#### `data_source_availability`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bank_ticker | text | Yes | Composite PK; FK | Optional context bank selector | monitored_institutions.bank_ticker | Required | Part of composite primary key with fiscal_year and quarter. |

#### `data_source_registry`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| data_source_name | text | Yes | PK | Filters drawer labels and source contracts | availability.data_source_list; source_retrieval_data.data_source_name; source_retrieval_embeddings.data_source_name | Required | Natural primary key for the source registry such as investor_slides. |

#### `monitored_institutions`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bank_ticker | text | Yes | PK | Optional context bank selector | availability.bank_ticker; source_retrieval_data.bank_ticker; source_retrieval_embeddings.bank_ticker | Required | Natural primary key for monitored banks. Tickers are expected to be unique. |


### UI Labels & Descriptors

Human-facing names descriptions and labels used by filters context selectors chat rows and viewer tiles.

#### `data_source_registry`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| data_source_display_name | text | Yes |  | Filters drawer display |  | Required | Human-readable source label. |
| data_source_description | text | Yes |  | Filters drawer descriptions |  | Required | Explains source coverage and intended use. |

#### `monitored_institutions`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| bank_name | text | Yes | Unique | Optional context and API contracts |  | Required | Short stable bank name such as rbc td or cibc. |
| bank_display_name | text | Yes |  | Optional context bank selector |  | Required | Full bank name shown in the UI. |
| bank_category | text | Yes |  | Optional context category selector |  | Required | Bank grouping such as Canadian bank or US bank. |


### Period & Availability

Fiscal year quarter and coverage fields that determine which sources are available for each bank.

#### `data_source_availability`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fiscal_year | integer | Yes | Composite PK | Optional context fiscal year selector |  | Required | Part of composite primary key with bank_ticker and quarter. |
| quarter | text | Yes | Composite PK | Optional context quarter selector |  | Required | Part of composite primary key with bank_ticker and fiscal_year. |
| data_source_list | text[] | Yes | FK list | Optional context source availability | data_source_registry.data_source_name | Required | Array of data_source_name values available for the bank fiscal year and quarter. |


### State & Audit

Lifecycle status timestamps and operational audit fields used for ordering debugging and maintenance.

#### `data_source_availability`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| updated_at | timestamptz | Yes |  | Admin and audit views |  | Required | Only audit field needed for catalog tables. |

#### `data_source_registry`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| updated_at | timestamptz | Yes |  | Admin and audit views |  | Required | Only audit field needed for catalog tables. |

#### `monitored_institutions`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| updated_at | timestamptz | Yes |  | Admin and audit views |  | Required | Only audit field needed for catalog tables. |


## Content


### Identity & Relationship Keys

Stable row anchors and foreign-key links shared across schemas.

#### `source_retrieval_data`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| data_source_name | text | Yes | FK | Source routing and filters | data_source_registry.data_source_name | Required | Standardized from v1 source_type. Each data source has a physical data table using this shared schema. |
| file_id | text | Yes | Composite PK; FK | Viewer source links and retrieval joins | source_retrieval_embeddings.file_id | Required | Part of v1 primary key with chunk_id. |
| chunk_id | text | Yes | Composite PK | Retrieval candidate id and citation anchor | source_retrieval_embeddings.content_unit_ids | Required | Part of v1 primary key with file_id. |

#### `source_retrieval_embeddings`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| embedding_id | text | Yes | PK | Embedding row id |  | Required | Primary key for long-form embedding rows. |
| data_source_name | text | Yes | FK | Source routing and filters | data_source_registry.data_source_name | Required | Standardized from v1 source_type. |
| file_id | text | Yes | FK | Retrieval data join | source_retrieval_data.file_id | Required | Joins embedding rows back to data rows. |
| content_unit_id | text | No |  | Single content-unit pointer | source_retrieval_data.chunk_id | Optional | Nullable because document and section summary embeddings can cover multiple units. |
| content_unit_ids | jsonb | Yes | FK list | Multi-content-unit join | source_retrieval_data.chunk_id | Required | JSON array used in v1 lateral join to source_retrieval_data.chunk_id. |
| chunk_id | text | No |  | Optional direct chunk pointer | source_retrieval_data.chunk_id | Optional | V1 carries this when the embedding maps to a single chunk. |
| section_id | text | No |  | Section-level embedding pointer |  | Optional | Used for section_summary embeddings. |


### Period & Availability

Fiscal year quarter and coverage fields that determine which sources are available for each bank.

#### `source_retrieval_data`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fiscal_year | text | Yes |  | Optional context and retrieval filter | source_retrieval_embeddings.fiscal_year | Required | V1 stores fiscal_year as text. |
| quarter | text | Yes |  | Optional context and retrieval filter | source_retrieval_embeddings.quarter | Required | V1 stores quarter as text. |
| bank_ticker | text | Yes | FK | Optional context and retrieval filter | monitored_institutions.bank_ticker; source_retrieval_embeddings.bank_ticker | Required | Standardized from v1 bank. |

#### `source_retrieval_embeddings`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fiscal_year | text | Yes |  | Optional context and retrieval filter | source_retrieval_data.fiscal_year | Required | V1 stores fiscal_year as text. |
| quarter | text | Yes |  | Optional context and retrieval filter | source_retrieval_data.quarter | Required | V1 stores quarter as text. |
| bank_ticker | text | Yes | FK | Optional context and retrieval filter | monitored_institutions.bank_ticker; source_retrieval_data.bank_ticker | Required | Standardized from v1 bank. |


### Storage & Content

Source file identifiers chunk text enrichment JSON and embedding vectors used by retrieval.

#### `source_retrieval_data`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| file_name | text | Yes |  | Viewer citation title |  | Required | Standardized from v1 filename. |
| file_type | text | Yes |  | Viewer citation and parser routing |  | Required | Source file type such as pdf xlsx or xml. |
| file_path | text | Yes |  | Source file location |  | Required | Path to source file in the configured document store. |
| file_hash | text | Yes |  | Ingest integrity and dedupe | source_retrieval_embeddings.file_hash | Required | Hash of the source file. |
| page_number | integer | No |  | Viewer page jump |  | Optional | Nullable because not every source is page-based. |
| name | text | No |  | Retrieval title or row label |  | Optional | V1 field for content-unit name. |
| summary | text | No |  | Retrieval preview and full-text ranking |  | Optional | V1 field for content-unit summary. |
| chunk_content | text | No |  | Retrieval body and citation preview |  | Optional | V1 field for extracted chunk text. |
| keywords | jsonb | Yes |  | Keyword search and enrichment |  | Required | V1 default is empty JSON array. |
| metrics | jsonb | Yes |  | Metric search and enrichment |  | Required | V1 default is empty JSON array. |
| keyword_embedding | vector(3072) | No |  | Keyword vector retrieval |  | Optional | V1 optional vector column on the data table. |
| metric_embedding | vector(3072) | No |  | Metric vector retrieval |  | Optional | V1 optional vector column on the data table. |
| summary_embedding | vector(3072) | No |  | Summary vector retrieval |  | Optional | V1 optional vector column on the data table. |
| chunk_embedding | vector(3072) | No |  | Chunk vector retrieval |  | Optional | V1 optional vector column on the data table. |

#### `source_retrieval_embeddings`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| file_name | text | Yes |  | Viewer citation title |  | Required | Standardized from v1 filename. |
| file_type | text | Yes |  | Viewer citation and parser routing |  | Required | Source file type such as pdf xlsx or xml. |
| file_path | text | Yes |  | Source file location |  | Required | Path to source file in the configured document store. |
| file_hash | text | Yes |  | Ingest integrity and dedupe | source_retrieval_data.file_hash | Required | Hash of the source file. |
| embedding_type | text | Yes |  | Retrieval strategy and ranking |  | Required | V1 values include content keyword metric section_summary and document_summary. |
| embedding_scope | text | Yes |  | Retrieval scope |  | Required | V1 values include content_unit section and document. |
| embedding_text | text | Yes |  | Vector input text and debug preview |  | Required | Text used to produce the embedding vector. |
| text_hash | text | No |  | Embedding dedupe and cache key |  | Optional | Hash of embedding_text. |
| embedding | vector(3072) | No |  | Semantic retrieval |  | Optional | V1 DDL allows null but retrieval requires non-null vectors for searchable rows. |
| embedding_model | text | Yes |  | Embedding provenance |  | Required | Model used to produce embedding. |
| embedding_dimensions | integer | Yes |  | Embedding validation |  | Required | V1 default dimension is 3072. |


### State & Audit

Lifecycle status timestamps and operational audit fields used for ordering debugging and maintenance.

#### `source_retrieval_data`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| created_at | timestamptz | No |  | Ingest lineage |  | Optional | V1 data table column is nullable. |

#### `source_retrieval_embeddings`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| created_at | timestamptz | Yes |  | Ingest lineage |  | Required | V1 embeddings table requires created_at. |


## Runtime


### Identity & Relationship Keys

Stable row anchors and foreign-key links shared across schemas.

#### `artifacts`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| artifact_id | uuid | Yes | PK | Artifacts strip and viewer |  | Required | Stable generated artifact id. |
| conversation_id | uuid | Yes | FK | Artifacts strip query by conversation | chat_conversations.conversation_id | Required | Conversation that generated the artifact. |
| run_uuid | uuid | No | Indexed link | Artifact lineage | process_monitor_logs.run_uuid | Optional | Links artifact generation to the v1 process monitor run_uuid. |

#### `chat_conversations`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| conversation_id | uuid | Yes | PK | Chat conversation lookup | chat_messages.conversation_id; artifacts.conversation_id | Required | Stable chat conversation id. |
| user_id | uuid | Yes | FK | Conversation ownership | auth.users.id | Required | Current authenticated user id. v1 process_monitor_logs stores user_id as varchar so runtime code should cast or store the string copy there. |

#### `chat_messages`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| message_id | uuid | Yes | PK | Chat message rendering |  | Required | Stable chat message id. |
| conversation_id | uuid | Yes | FK | Chat history load | chat_conversations.conversation_id | Required | Parent conversation. |
| run_uuid | uuid | No | Indexed link | Process monitor linkage | process_monitor_logs.run_uuid | Optional | Links assistant output to the v1 process monitor run_uuid that produced it. |

#### `process_monitor_logs`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| log_id | bigint | Yes | PK | Process log row id |  | Required | V1 primary key backed by process_monitor_logs_log_id_seq. |
| run_uuid | uuid | Yes | Indexed | Process run grouping | chat_messages.run_uuid; artifacts.run_uuid | Required | V1 indexed run identifier. One run_uuid can have many stage log rows. |
| user_id | varchar(255) | No |  | Process log ownership | chat_conversations.user_id | Optional | V1 nullable user id. Kept as v1 varchar; chat_conversations.user_id is uuid in the v2 runtime schema. |

#### `prompts`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| id | integer | Yes | PK | Prompt inventory |  | Required | V1 primary key backed by prompts_id_seq. |


### UI Labels & Descriptors

Human-facing names descriptions and labels used by filters context selectors chat rows and viewer tiles.

#### `artifacts`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| artifact_title | text | Yes |  | Artifact tile title |  | Required | Renamed from title. |

#### `chat_conversations`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| conversation_title | text | No |  | Conversation list and chat header |  | Optional | Renamed from title. |


### Query & Conversation Runtime

User session query and chat-message state used during active conversations.

#### `chat_messages`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| role | text | Yes |  | Chat transcript rendering |  | Required | User assistant tool or system role. |
| content | text | Yes |  | Chat transcript rendering |  | Required | Message body or streamed final text. |


### Generated Outputs & Viewer Artifacts

Agent-generated HTML reports widgets and viewer targets surfaced by the UI.

#### `artifacts`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| artifact_type | text | Yes |  | Artifact tile type and icon |  | Required | Values like quick_search deep_search or report. |
| artifact_content | text | Yes |  | Viewer HTML preview |  | Required | Renamed from html_content. Stores rendered HTML research output or report body. |
| artifact_references | jsonb | No | FK | Viewer citations | source_retrieval_data.file_id/chunk_id; source_retrieval_embeddings.embedding_id | Optional | Renamed from source_refs. Retrieval rows referenced by the artifact. |


### Process Monitor & Agent State

Query run stage events status updates and operational visibility.

#### `process_monitor_logs`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| model_name | varchar(100) | Yes | Indexed | Process monitor filtering |  | Required | V1 indexed model name. |
| stage_name | varchar(100) | Yes | Indexed | Process monitor stage display |  | Required | V1 indexed stage name. |
| stage_start_time | timestamptz | Yes | Indexed | Process monitor timeline |  | Required | V1 indexed stage start timestamp. |
| stage_end_time | timestamptz | No |  | Process monitor timeline |  | Optional | V1 nullable stage end timestamp. |
| duration_ms | integer | No |  | Process monitor duration |  | Optional | V1 nullable stage duration in milliseconds. |
| llm_calls | jsonb | No |  | Process monitor LLM usage detail |  | Optional | V1 nullable structured LLM call detail. |
| total_tokens | integer | No |  | Process monitor token usage |  | Optional | V1 nullable total token count. |
| total_cost | numeric(12,6) | No |  | Process monitor cost usage |  | Optional | V1 nullable total cost. |
| status | varchar(255) | No | Indexed | Process monitor status display |  | Optional | V1 indexed nullable status. |
| decision_details | text | No |  | Process monitor decision detail |  | Optional | V1 nullable detail text. |
| error_message | text | No |  | Process monitor error display |  | Optional | V1 nullable error text. |
| environment | varchar(50) | No | Indexed | Process monitor environment filtering |  | Optional | V1 indexed nullable environment. |
| custom_metadata | jsonb | No |  | Process monitor structured metadata |  | Optional | V1 nullable custom metadata. |
| notes | text | No |  | Process monitor notes |  | Optional | V1 nullable notes. |


### Prompt & Tool Configuration

Prompt inventory versioning and tool routing configuration.

#### `prompts`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| model | text | Yes | Unique composite | Prompt lookup and routing |  | Required | Part of unique key with layer name and version. |
| layer | text | Yes | Unique composite | Prompt lookup and grouping |  | Required | Part of unique key with model name and version. |
| name | text | Yes | Unique composite | Prompt lookup |  | Required | Part of unique key with model layer and version. |
| description | text | No |  | Prompt inventory |  | Optional | Optional prompt description. |
| comments | text | No |  | Prompt inventory |  | Optional | Optional prompt comments. |
| system_prompt | text | No |  | Prompt execution |  | Optional | System prompt body. |
| user_prompt | text | No |  | Prompt execution |  | Optional | User prompt body. |
| tool_definition | jsonb | No |  | Tool calling configuration |  | Optional | Optional JSON tool definition. |
| uses_global | text[] | No |  | Prompt composition |  | Optional | Global prompt names composed into this prompt. |
| version | text | Yes | Unique composite | Prompt versioning |  | Required | Defaults to 1.0.0 in v1. Part of unique key with model layer and name. |


### State & Audit

Lifecycle status timestamps and operational audit fields used for ordering debugging and maintenance.

#### `artifacts`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| created_at | timestamptz | Yes |  | Artifact strip newest-first sort |  | Required | Creation time displayed as date and time on tile. |
| updated_at | timestamptz | Yes |  | Artifact refresh and audit |  | Required | Last artifact update time. |

#### `chat_conversations`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| created_at | timestamptz | Yes |  | Conversation ordering |  | Required | Conversation creation time. |
| updated_at | timestamptz | Yes |  | Conversation ordering and sync |  | Required | Conversation last activity time. |

#### `chat_messages`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| created_at | timestamptz | Yes |  | Message ordering |  | Required | Message creation time. |

#### `process_monitor_logs`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| log_timestamp | timestamptz | No |  | Process log ordering |  | Optional | V1 defaults to CURRENT_TIMESTAMP. |

#### `prompts`

| Field | Type | Required | Key | Usage | Links | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| created_at | timestamp | No |  | Prompt inventory ordering |  | Optional | V1 defaults to CURRENT_TIMESTAMP. |
| updated_at | timestamp | No |  | Prompt audit |  | Optional | V1 defaults to CURRENT_TIMESTAMP. |


## UI Contract

| UI Event | Trigger | Method | Endpoint | Request Payload | Response Payload | Stream Events | Tables Read | Tables Written | UI Behavior | Developer Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| load_v2_interface | Open or refresh /v2 | GET | /api/v2/bootstrap | user_id; active_conversation_id?; catalog_version? | user; active_conversation?; messages[]; recent_artifacts[]; data_sources[]; optional_context_options | none | chat_conversations; chat_messages; artifacts; data_source_registry; monitored_institutions; data_source_availability | none | Hydrate chat ask controls filters optional context and artifact strip | Bootstrap can be split later if payload becomes too heavy. |
| open_filters | Click Filters button | GET | /api/v2/data-sources | user_id | data_sources[] {data_source_name; data_source_display_name; data_source_description} | none | data_source_registry | none | Render filters drawer | data_source_name is the stable value sent back on query submit. |
| open_optional_context | Open Optional Context control | GET | /api/v2/optional-context | user_id; selected_data_sources? | banks[] {bank_ticker; bank_name; bank_display_name; bank_category}; availability[] {bank_ticker; fiscal_year; quarter; data_source_list} | none | monitored_institutions; data_source_availability | none | Render bank period and available-source selectors | Availability lookup key is bank_ticker + fiscal_year + quarter. |
| select_conversation | Open an existing chat | GET | /api/v2/conversations/{conversation_id} | user_id; conversation_id | conversation; messages[]; artifacts[] | none | chat_conversations; chat_messages; artifacts | none | Set active conversation and load history | Use when user selects a prior conversation. |
| create_conversation | Start a new chat | POST | /api/v2/conversations | user_id; conversation_title? | conversation_id; conversation_title; created_at; updated_at | none | none | chat_conversations | Create and activate an empty conversation | Conversation title can be backfilled after the first message. |
| send_query | Click Send | POST | /api/v2/query | user_id; conversation_id; filter_selection[]; optional_context_selection; user_query; model_selection; search_selection | accepted; run_uuid; user_message_id; assistant_message_id; stream_url | status; text_delta; widget_trigger; reference_link; artifact_created; process_log; done; error | data_source_registry; monitored_institutions; data_source_availability; prompts; source_retrieval_data; source_retrieval_embeddings | chat_messages; process_monitor_logs; artifacts as generated | Append user message and start assistant response stream | run_uuid links chat messages artifacts and v1 process_monitor_logs rows. |
| receive_aegis_stream_event | Subscribe to Aegis response stream | GET/SSE | /api/v2/query/{run_uuid}/stream | run_uuid; last_event_id? | event {event_type; event_id; payload; created_at} | status; text_delta; widget_trigger; reference_link; artifact_created; process_log; done; error | process_monitor_logs; artifacts | chat_messages final assistant; process_monitor_logs; artifacts | Render status text widgets links artifact tiles and completion state | Stream event payloads should be versioned before backend implementation. |
| render_widget_trigger | Receive widget_trigger stream event | CLIENT | ui://widgets/{widget_type} | widget_type; widget_payload; run_uuid | rendered_widget_state | none | none | none | Render prebuilt UI widget from JSON payload | Aegis sends inputs only; UI owns the widget implementations. |
| refresh_artifacts | Artifact strip refresh after stream or tab focus | GET | /api/v2/conversations/{conversation_id}/artifacts | user_id; conversation_id; limit? | artifacts[] {artifact_id; artifact_title; artifact_type; created_at; updated_at; artifact_references?} | none | artifacts | none | Update artifact row scroller newest first | Used by the viewer tab and after artifact_created events. |
| open_artifact | Click artifact tile | GET | /api/v2/artifacts/{artifact_id} | user_id; conversation_id; artifact_id | artifact_id; artifact_title; artifact_type; artifact_content; artifact_references; created_at; updated_at | none | artifacts | none | Open artifact content in viewer | artifact_content is expected to be renderable HTML. |
| open_reference_link | Click source or artifact reference link | POST | /api/v2/references/resolve | user_id; target_type; data_source_name?; file_id?; chunk_id?; embedding_id?; artifact_id?; page_number? | viewer_target {kind; title; metadata; preview_url?; artifact_content?; retrieval_excerpt?} | none | source_retrieval_data; source_retrieval_embeddings; artifacts | none | Open document retrieval row or artifact in viewer | Use one resolver for links from chat text widgets and artifacts. |
| inspect_process_monitor | Open process or debug details | GET | /api/v2/process-monitor/{run_uuid} | run_uuid | process_monitor_logs[] ordered by stage_start_time | none | process_monitor_logs | none | Show process stage timeline or developer debug drawer | Developer-facing endpoint; run_uuid is not unique in process_monitor_logs. |
