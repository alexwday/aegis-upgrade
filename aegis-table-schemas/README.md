# Aegis V2 Table Schemas

This folder is the clean SQL workspace for the Aegis V2 runtime.

The field-level planning source is:

- `docs/aegis_v2/aegis_runtime_schema_field_map.xlsx`

The old V1 schema exports are archived at:

- `archive/v1/aegis-table-schemas/`

## Layout

- `catalog/`: data source registry, monitored institutions, and source availability.
- `content/`: shared retrieval data and embedding table schemas.
- `runtime/`: conversations, messages, artifacts, process monitor logs, and prompts.
- `migrations/`: ordered DDL migrations once the V2 schema is converted from the workbook.

`runtime/prompts.sql` and `runtime/process_monitor_logs.sql` intentionally mirror V1 until the runtime is ready for a migration.
