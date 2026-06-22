# Runtime Schemas

V2 runtime tables should be generated from the Runtime sheet in `docs/aegis_v2/aegis_runtime_schema_field_map.xlsx`.

`prompts.sql` and `process_monitor_logs.sql` are copied from V1 because those schemas are intentionally preserved for the first V2 agent build.

`001_chat_artifact_tables.sql` creates the V2-owned runtime tables:

- `chat_conversations`
- `chat_messages`
- `artifacts`
