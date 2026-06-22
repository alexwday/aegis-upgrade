# Aegis V2 Prompts

This folder is the clean prompt workspace for the Aegis V2 agent.

The V1 prompt archive is preserved at:

- `archive/v1/aegis-prompts/`

Runtime prompt rows still use the V1-compatible `public.prompts` table shape. New V2 prompt files should be added here before being pushed into Postgres.

## Layout

- `global/`: shared project, database, citation, and policy instructions.
- `agent/`: orchestration, routing, planning, and response prompts.
- `tools/`: prompt files owned by individual tools or widget contracts.
