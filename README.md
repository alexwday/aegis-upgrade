# Aegis Upgrade

Portable workspace for the Aegis agent, source document inputs, ETL pipelines,
and PostgreSQL table schemas.

## Layout

```text
aegis-agent/          FastAPI chat app and retrieval agent
aegis-documents/      Canonical Q1/Q2 source document drop folders
aegis-pipeline/       Per-source ETL pipelines
aegis-prompts/        Canonical YAML copy of Aegis prompt rows
aegis-table-schemas/  PostgreSQL table DDL exports
docs/                 V2 planning notes and architecture docs
scripts/              Root scripts for setup, env sync, DB, prompts, and pipelines
```

## Aegis V2 Planning

The V2 rebuild is being planned UI-first in `docs/aegis_v2/`. The current agent
and UI should be treated as a frozen V1 reference while preserving the durable
pipeline, table, prompt, and retrieval infrastructure.

## Push To A New GitHub Repo

Create an empty GitHub repo, then from this project root:

```bash
git init
git add .
git commit -m "Initial Aegis upgrade workspace"
git branch -M main
git remote add origin git@github.com:<OWNER>/<REPO>.git
git push -u origin main
```

The repo is configured to ignore `.env`, generated per-source env files,
virtual environments, Python caches, and pipeline output folders.

## Work Computer Setup

Clone the repo, create the venv, and install the unified workstation
requirements:

```bash
git clone git@github.com:<OWNER>/<REPO>.git
cd <REPO>
python3 scripts/bootstrap_workstation.py
```

Create local config:

```bash
cp .env.example .env
```

Fill `.env` with work-computer values. For OAuth, SSL, and a custom
OpenAI-compatible base URL, use:

```dotenv
AUTH_METHOD=oauth
LLM_AUTH_MODE=oauth
LLM_ENDPOINT_MODE=internal
LLM_BASE_URL=<custom-base-url>
OAUTH_ENDPOINT=<token-endpoint>
OAUTH_CLIENT_ID=<client-id>
OAUTH_CLIENT_SECRET=<client-secret>
SSL_VERIFY=true
POSTGRES_HOST=<host>
POSTGRES_DATABASE=<database>
POSTGRES_USER=<user>
POSTGRES_PASSWORD=<password>
```

Then sync the root `.env` into `aegis-agent/.env` and each pipeline
`.env`:

```bash
.venv/bin/python scripts/sync_env.py
```

## PostgreSQL Setup

Check which expected tables already exist:

```bash
.venv/bin/python scripts/db_setup.py
```

Create missing tables from `aegis-table-schemas/`:

```bash
.venv/bin/python scripts/db_setup.py --apply
```

Create a source retrieval table pair directly when you do not want to run the
full schema directory setup:

```bash
.venv/bin/python scripts/create_retrieval_tables.py --source investor_slides --apply
```

Upsert the canonical prompt archive into `public.prompts`:

```bash
.venv/bin/python scripts/push_aegis_prompts.py
```

If your database user cannot create extensions, ask your DBA to install
`pgvector` once, then rerun with:

```bash
.venv/bin/python scripts/db_setup.py --apply --skip-vector-extension
```

## Add Documents

Drop source files under:

```text
aegis-documents/<source>/2026_Q2/<BANK-REGION>/
```

The Q2 folders are already present for:

```text
BMO-CA, BNS-CA, CM-CA, NA-CA, RY-CA, TD-CA
```

## One-Time CSV Migration

If this workstation has old `master-data.csv` / `master-embeddings.csv`
outputs, migrate each source once before relying on incremental pipeline runs:

```bash
for source in \
  investor_slides \
  supplementary_financials \
  rts \
  pillar3 \
  transcripts \
  event_transcripts; do
  .venv/bin/python scripts/migrate_retrieval_csvs_to_postgres.py \
    --source "$source" \
    --apply
done
```

## Run Pipelines

```bash
.venv/bin/python scripts/run_pipeline.py --source investor_slides
.venv/bin/python scripts/run_pipeline.py --all
```

After syncing source tables, refresh the agent availability preflight table:

```bash
.venv/bin/python scripts/db_setup.py --refresh-availability
```

## Run The Agent

```bash
cd aegis-agent
../.venv/bin/python run_fastapi.py --port 8012
```

Open `http://127.0.0.1:8012` and query Q1/Q2 coverage. Example:

```text
Compare RBC and TD Q1 2026 capital and credit quality across all sources.
```
