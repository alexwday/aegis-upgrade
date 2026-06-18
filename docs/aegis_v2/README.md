# Aegis V2 Planning

This folder is the planning surface for the UI-first Aegis rebuild.

The current repo is at a good checkpoint for this work: the durable data and
pipeline layers are separate from the V1 agent and UI. V2 should preserve the
source documents, PostgreSQL schemas, ETL pipelines, prompt archive, connection
utilities, and useful retrieval code while replacing the user-facing interface
and agentic process.

## Working Principle

Design the UI first, then derive agent tools from the UI.

The interface should show what analysts need to control, inspect, compare, and
trust. Those visible controls and states should become the tool contracts the
agent can call. This keeps the agent focused on operating a well-defined
research workstation instead of improvising the product shape turn by turn.

## Preserve

- `aegis-documents/`: canonical source files and folder conventions.
- `aegis-pipeline/`: ETL, chunking, enrichment, embedding, and load flow.
- `aegis-table-schemas/`: PostgreSQL table contracts.
- `scripts/`: setup, table creation, loading, prompt sync, and source registry.
- `aegis-prompts/`: prompt archive, especially source ETL/retrieval prompts.
- `aegis-agent/src/aegis_agent/connections/`: LLM, PostgreSQL, OAuth patterns.
- `aegis-agent/src/aegis_agent/utils/`: settings, logging, SSL, prompt loading.
- `aegis-agent/src/aegis_agent/model/subagents/`: current retrieval logic, at
  least until it is extracted or replaced by shared retrieval tools.

## Freeze Or Replace

- `aegis-agent/templates/chat.html`: V1 static chat UI.
- `aegis-agent/run_fastapi.py`: V1 websocket/session assumptions.
- `aegis-agent/src/aegis_agent/model/agents/`: V1 single-agent loop, tool schema,
  progress stream, chart slot protocol, and final response protocol.
- `aegis-prompts/aegis_agent/`: V1 agent prompt set.

## Work Order

1. Freeze V1 boundaries and avoid deleting retrieval code prematurely.
2. Design the V2 analyst UI and interaction states.
3. Convert each UI feature into explicit backend contracts.
4. Build or extract retrieval tools behind those contracts.
5. Implement the new agent orchestration over the tool layer.
6. Replace the V1 UI with the V2 interface.
7. Cut over only after the new UI and agent can run against existing data.

## Planning Docs

- [V1 Archive Boundary](./v1_archive_boundary.md)
- [UI-First Design Plan](./ui_first_design_plan.md)
- [Retrieval Tooling Plan](./retrieval_tooling_plan.md)

