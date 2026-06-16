# Aegis Pipeline

Consolidated ETL pipelines for Aegis document sources. Each source turns
documents under `aegis-documents/` into retrieval-ready master CSVs and optional
PostgreSQL tables.

## Layout

```text
aegis-pipeline/
  connections/
  utils/
  sources/
    investor_slides/
      main.py
      pipeline/
    supplementary_financials/
      main.py
      pipeline/
    rts/
      main.py
      pipeline/
    pillar3/
      main.py
      pipeline/
    transcripts/
      main.py
      pipeline/
    event_transcripts/
      main.py
      pipeline/
  tests/
```

Shared connection and utility helpers live once at the Aegis pipeline root.
Each source folder keeps only its source-specific pipeline implementation,
generated artifacts, and local `.env`.

Token counting uses one shared tiktoken cache at
`aegis-pipeline/tokenizer-cache/`, so all sources reuse the same tokenizer
assets.

## Sources

| Source key | Documents folder | Input type | Public tables |
| --- | --- | --- | --- |
| `investor_slides` | `aegis-documents/investor_slides/` | Investor slide PDFs | `aegis-investor-slides-data`, `aegis-investor-slides-embeddings` |
| `supplementary_financials` | `aegis-documents/supplementary_financials/` | Financial supplement workbooks | `aegis-financial-supp-data`, `aegis-financial-supp-embeddings` |
| `rts` | `aegis-documents/rts/` | Report to shareholders PDFs | `aegis-rts-data`, `aegis-rts-embeddings` |
| `pillar3` | `aegis-documents/pillar3/` | Pillar 3 workbooks | `aegis-pillar3-data`, `aegis-pillar3-embeddings` |
| `transcripts` | `aegis-documents/transcripts/` | Earnings transcript XMLs | `aegis-earnings-transcripts-data`, `aegis-earnings-transcripts-embeddings` |
| `event_transcripts` | `aegis-documents/event_transcripts/` | Event transcript XMLs | `aegis-event-transcripts-data`, `aegis-event-transcripts-embeddings` |

Use this local input shape for every source:

```text
aegis-documents/<source>/
  2026_Q1/
    RY-CA/
      source_file.ext
    BNS-CA/
      source_file.ext
```

The manifest stage derives fiscal year, quarter, and bank from the folder path.
For `transcripts`, multiple visible FactSet XMLs for the same bank-period are
collapsed to the canonical earnings-call file by title/filename and version
priority. For `event_transcripts`, all matching XMLs are processed separately,
while filenames that declare a different year or quarter are skipped.

## Setup

From the repository root:

```bash
python3 scripts/bootstrap_workstation.py
.venv/bin/python scripts/sync_env.py
```

Edit the root `.env` with local values, then run `scripts/sync_env.py` to write
`aegis-agent/.env` and each source's `aegis-pipeline/sources/<source>/.env`.
Important values include:

- `LLM_ENDPOINT_MODE=default` with `LLM_DEFAULT_URL=https://api.openai.com/v1`,
  or `LLM_ENDPOINT_MODE=internal` with your custom `LLM_BASE_URL`.
- `OPENAI_API_KEY` for default auth, or the `OAUTH_*` settings for OAuth.
- `EMBEDDING_MODEL=text-embedding-3-large`.
- `EMBEDDING_DIMENSIONS=3072`.
- `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` for Postgres.

PDF sources also use:

- `PDF_RAW_OCR_MODEL=gpt-5.4` for `investor_slides`.
- `PDF_RAW_OCR_MODEL=gpt-5-mini` and `PDF_RAW_OCR_REASONING_EFFORT=low` for
  `rts`.

## Run Pipelines

Run one source:

```bash
.venv/bin/python scripts/run_pipeline.py --source investor_slides
```

Run all sources:

```bash
.venv/bin/python scripts/run_pipeline.py --all
```

Valid source keys are `investor_slides`, `supplementary_financials`, `rts`,
`pillar3`, `transcripts`, and `event_transcripts`.

Finalized outputs are written under each source folder:

```text
aegis-pipeline/sources/<source>/data-output/master/master-data.csv
aegis-pipeline/sources/<source>/data-output/master/master-embeddings.csv
aegis-pipeline/sources/<source>/data-output/master/master-manifest.json
aegis-pipeline/sources/<source>/data-output/master/upload/
```

## PostgreSQL Load

Create the public tables once from the project root:

```bash
.venv/bin/python scripts/create_retrieval_tables.py --source investor_slides --apply
```

Refresh tables from the current finalized CSV snapshot:

```bash
.venv/bin/python scripts/load_retrieval_master_csvs.py --source investor_slides --apply
```

The load script validates both CSVs, stages them in temporary tables, truncates
the targets, and inserts the new snapshot in one transaction.

## Validation

Compile the pipeline code:

```bash
.venv/bin/python -m compileall aegis-pipeline/utils aegis-pipeline/connections aegis-pipeline/sources scripts
```

Run pipeline tests:

```bash
.venv/bin/python -m pytest aegis-pipeline/tests -q
```

Check env sync targets without writing files:

```bash
.venv/bin/python scripts/sync_env.py --check
```

## Runtime Contract

`aegis-agent` owns all runtime retrievers and does not import retrieval code from
legacy `/Users/alexwday/Projects/aegis`.
