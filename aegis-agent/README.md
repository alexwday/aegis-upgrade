# Aegis Agent V2

This is the active V2 agent working copy. It starts from the V1 agent code so we
can reuse the useful FastAPI, PostgreSQL, settings, logging, OAuth, SSL, prompt
loading, and monitoring patterns while rebuilding the UI/API contract and agent
runtime around the V2 schema.

The frozen V1 copy is archived at:

- `archive/v1/aegis-agent/`

## V2 Inputs

- UI and schema planning: `docs/aegis_v2/`
- Runtime schema field map: `docs/aegis_v2/aegis_runtime_schema_field_map.xlsx`
- Clean prompt workspace: `aegis-prompts/`
- Clean SQL schema workspace: `aegis-table-schemas/`

## Project Boundaries

- `/Users/alexwday/Projects/aegis-upgrade/aegis-documents` owns organized source inputs.
- `/Users/alexwday/Projects/aegis-upgrade/aegis-pipeline` owns ETL pipelines for each source.
- `aegis-agent` owns the V2 user-facing app, API contract, agent loop, tools,
  retrievers, and UI.

## Run

The old run path may change as the V2 API replaces the V1 websocket/session
flow.

```bash
cd /path/to/aegis-upgrade/aegis-agent
../.venv/bin/python run_fastapi.py --port 8012
```

Then open `http://127.0.0.1:8012`.
