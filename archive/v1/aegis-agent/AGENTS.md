# Aegis Agent

Four-source document-research sibling project for Aegis. The package is `aegis_agent`.

## Project Layout

```text
src/aegis_agent/
  model/
    main.py                  # Async generator entrypoint
    agents/                  # Single agent loop, tools, research, progress, cards
    subagents/transcripts/   # Transcript retrieval and structured research
  connections/               # OpenAI, OAuth, PostgreSQL
  utils/                     # Settings, logging, SSL, Postgres prompts, monitor
templates/chat.html          # Static websocket chat UI
../scripts/seed_agent_demo_data.py  # Idempotent demo data and availability seeding
```

## Commands

```bash
cd /Users/alexwday/Projects/aegis-upgrade/aegis-agent

../.venv/bin/python -m pytest tests/ -q
../.venv/bin/python ../scripts/seed_agent_demo_data.py --fixture
../.venv/bin/python run_fastapi.py --port 8012
```

## Conventions

- Use `from aegis_agent.utils.settings import config` for settings.
- Use `from aegis_agent.utils.logging import get_logger` for logging.
- Runtime prompts load from PostgreSQL `public.prompts`. The repo copy lives in
  `/Users/alexwday/Projects/aegis-upgrade/aegis-prompts`.
- Keep public workflow output as async-generator websocket events.
- The agent path supports investor slides, supplementary financials, RTS, and
  Pillar 3 as the default document sources. Transcripts remain available only
  where explicitly requested by tests or future work.
- `run_research` must not retrieve data until bank, fiscal year, quarter, and
  research question are explicit.
