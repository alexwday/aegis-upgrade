# Aegis RTS Pipeline

Pipeline for converting Canadian bank report to shareholders PDFs into retrieval-ready
master CSVs and PostgreSQL tables.

The project keeps generated artifacts, logs, virtual environments, and secrets
out of git. The checked-in `data-input/` folder provides reproducible sample
inputs; place any additional source files under `data-input/report-to-shareholders/`
locally before running the pipeline.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

For local validation tooling:

```bash
python -m pip install -r requirements-dev.txt
```

Create local config:

```bash
cp database/.env.example database/.env
```

Edit `database/.env` with local values:

- `LLM_ENDPOINT_MODE=default` with `LLM_DEFAULT_URL=https://api.openai.com/v1`,
  or `LLM_ENDPOINT_MODE=internal` with your custom `LLM_BASE_URL`.
- `OPENAI_API_KEY` for default auth, or the `OAUTH_*` settings for OAuth.
- `LLM_MODEL_LARGE=gpt-5-mini` for extraction and enrichment.
- `LLM_REASONING_EFFORT_LARGE=low`.
- `PDF_RAW_OCR_MODEL=gpt-5-mini`.
- `PDF_RAW_OCR_REASONING_EFFORT=low`.
- `EMBEDDING_MODEL=text-embedding-3-large`.
- `EMBEDDING_DIMENSIONS=3072`.
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` for Postgres.

## Input Layout

Use this local folder structure:

```text
data-input/report-to-shareholders/
  2026_Q1/
    RY-CA/
      rbc_q1_2026_rts.pdf
    BNS-CA/
      bns_q1_2026_rts.pdf
```

The manifest stage derives fiscal year, quarter, and bank from the folder path.

## Run End To End

```bash
source venv/bin/activate
venv/bin/python database/main.py
```

The finalized outputs are written to:

```text
data-output/master/master-data.csv
data-output/master/master-embeddings.csv
data-output/master/master-manifest.json
data-output/master/upload/
```

## PostgreSQL Load

Create the public tables once:

```bash
source venv/bin/activate
venv/bin/python scripts/create_master_data_table.py --apply
```

Refresh the tables from the current finalized CSV snapshot:

```bash
venv/bin/python scripts/load_master_data_csv.py --apply
```

The default table names are:

```text
public."aegis-rts-data"
public."aegis-rts-embeddings"
```

The load script validates both CSVs, stages them in temporary tables, truncates
the targets, and inserts the new snapshot in one transaction.

## Validation

```bash
source venv/bin/activate
venv/bin/python -m compileall database scripts
venv/bin/python -m pytest
```
