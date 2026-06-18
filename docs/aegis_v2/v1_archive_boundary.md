# V1 Archive Boundary

This is a non-destructive archive plan. The goal is to freeze the current V1
agent/UI as a reference point while keeping reusable infrastructure available
for V2.

## Current Checkpoint

- Base branch before V2 planning: `main`.
- V1 archive branch: `archive/v1-agent-ui`.
- Planning branch: `codex/v2-ui-agent-planning`.
- Worktree was clean before planning docs were added.

## Archive Strategy

Do not move or delete V1 code yet. The first archive step should be a Git
checkpoint, followed by documentation of which paths are frozen. Physical file
moves can happen later, after retrieval code has been extracted or the V2
replacement exists.

Optional future tag:

```bash
git tag v1-agent-ui-freeze
```

## Frozen V1 Surface

These files define the current V1 user experience and orchestration behavior:

- `aegis-agent/templates/chat.html`
- `aegis-agent/run_fastapi.py`
- `aegis-agent/src/aegis_agent/model/main.py`
- `aegis-agent/src/aegis_agent/model/agents/aegis_agent.py`
- `aegis-agent/src/aegis_agent/model/agents/tools.py`
- `aegis-agent/src/aegis_agent/model/agents/research.py`
- `aegis-agent/src/aegis_agent/model/agents/charts.py`
- `aegis-agent/src/aegis_agent/model/agents/chart_slots.py`
- `aegis-agent/src/aegis_agent/model/agents/status_reporter.py`
- `aegis-agent/src/aegis_agent/model/agents/ui_cards.py`
- `aegis-agent/src/aegis_agent/model/agents/progress.py`
- `aegis-agent/src/aegis_agent/model/agents/schemas.py`
- `aegis-prompts/aegis_agent/system.yaml`
- `aegis-prompts/aegis_agent/chart_planner.yaml`

## Reusable Surface

These paths are useful for V2 and should not be treated as disposable:

- `aegis-agent/src/aegis_agent/model/subagents/`
- `aegis-agent/src/aegis_agent/connections/`
- `aegis-agent/src/aegis_agent/utils/`
- `scripts/retrieval_source_config.py`
- `aegis-table-schemas/`
- `aegis-pipeline/`
- `aegis-prompts/{investor_slides,supplementary_financials,rts,pillar3,transcripts,event_transcripts}/`

## Extraction Target

Before replacing the agent, define a retrieval layer with a stable interface:

```text
UI action -> agent decision -> retrieval tool call -> evidence/result contract
```

The V1 subagents can sit behind that interface at first. The interface can then
be improved without forcing the UI or agent to know whether retrieval is
per-source, cross-source, exact lookup, or hybrid search.
