# Aegis Agent

Self-contained four-source agent demo for Aegis. This sibling project keeps the
useful FastAPI, WebSocket, PostgreSQL, OpenAI Chat Completions, settings,
logging, OAuth, SSL, prompt loading, and monitoring patterns from Aegis, but
replaces the legacy staged router/clarifier/planner/response/summarizer flow with
one conversational agent.

## What V1 Supports

- One user-facing Aegis agent that clarifies scope, calls tools, loops, and
  synthesizes final answers.
- Local runtime retrievers for investor slides, supplementary financials, RTS,
  Pillar 3, and transcripts.
- Automatic `aegis_data_availability` preflight before source retrieval.
- Partial availability: missing bank-period combinations are reported while
  available combinations continue researching.
- Deterministic status updates over tool progress logs.
- Structured evidence IDs and linked source pills in final answers and evidence
  tabs.
- Static FastAPI chat UI with agent messages, status timeline, source evidence
  tabs, and clickable clarification cards.

## Project Boundaries

- `/Users/alexwday/Projects/aegis-upgrade/aegis-documents` owns organized source inputs.
- `/Users/alexwday/Projects/aegis-upgrade/aegis-pipeline` owns ETL pipelines for each source.
- `aegis-agent` owns the user-facing app, agent loop, tools, prompts, retrievers,
  and UI. It should not import retriever code from legacy
  `/Users/alexwday/Projects/aegis`.

## Run

```bash
cd /path/to/aegis-upgrade/aegis-agent
../.venv/bin/python run_fastapi.py --port 8012
```

Then open `http://127.0.0.1:8012`.

## Demo Queries

- `Using all four sources, summarize RBC Q1 2026 CET1 capital, RWA, and credit quality.`
- `Compare RBC and TD Q1 2026 capital and credit quality across all sources.`
- `What source evidence do we have for BMO Q1 2026 Pillar 3 RWA?`
