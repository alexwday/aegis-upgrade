# Aegis Prompts

Canonical YAML archive for Aegis prompt rows. Runtime code loads prompts from `public.prompts`; this folder is the repo copy used to review and seed that table.

Each prompt YAML maps to one `public.prompts` row by `model`, `layer`, `name`, and `version`. The `_archive` block records whether this copy came from the latest Postgres export or from the current local YAML during the fallback-removal migration.

Use `python scripts/push_aegis_prompts.py` to upsert these files into Postgres.
