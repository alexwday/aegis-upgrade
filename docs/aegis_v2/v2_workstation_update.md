# V2 Workstation Update Script

Use `scripts/update_v2_workstation.py` from the repo root on the work computer
after pulling the latest V2 branch.

The work computer is expected to already have:

- processed Q1 and Q2 2026 source data loaded into the source tables
- a valid root `.env` or `aegis-agent/.env`
- database connectivity to the local PostgreSQL instance
- the monitored institutions YAML at one of the known FactSet paths, or passed
  explicitly with `--monitored-institutions-yaml`

## What It Does

The script is idempotent and performs the V2 install/update steps in order:

1. Adds missing medium-tier model env keys for V2 model routing.
2. Syncs the root env into `aegis-agent/.env` and pipeline env files.
3. Creates V2 catalog/runtime tables when missing.
4. Upserts V2 data source registry rows.
5. Upserts monitored institutions from `monitored_institutions.yaml`.
6. Refreshes `data_source_availability` from the live processed source tables.
7. Pushes prompt YAML files into `public.prompts` when prompt YAML exists.
8. Optionally builds the frontend.
9. Starts FastAPI and opens `http://127.0.0.1:8012/v2`.

## Medium Model Config

The V2 code already supports the internal `small`, `medium`, and `large` model
tiers. The UI still exposes only Small and Large:

- UI Small: orchestrator uses medium, research uses small
- UI Large: orchestrator uses large, research uses medium

On an older work-computer env file, the only required model-config change is to
add the medium env keys. The update script appends missing medium keys unless
`--no-env-patch` is passed. After the script adds them, review `LLM_MODEL_MEDIUM`
and the medium reasoning/token settings for the work environment.

## Recommended Commands

Run a full dry run first:

```bash
./.venv/bin/python scripts/update_v2_workstation.py --dry-run --allow-empty-prompts --skip-server
```

Run the strict install/update:

```bash
./.venv/bin/python scripts/update_v2_workstation.py --build-frontend
```

If the monitored institutions YAML is not in a default location:

```bash
./.venv/bin/python scripts/update_v2_workstation.py \
  --monitored-institutions-yaml "/path/to/monitored_institutions.yaml" \
  --build-frontend
```

If V2 prompt YAML files have not been created yet, either intentionally allow an
empty prompt load:

```bash
./.venv/bin/python scripts/update_v2_workstation.py --allow-empty-prompts --build-frontend
```

or seed the V1 prompt archive as a temporary bridge:

```bash
./.venv/bin/python scripts/update_v2_workstation.py \
  --prompts-dir archive/v1/aegis-prompts \
  --build-frontend
```

## Source Availability Refresh

By default, the availability refresh is strict:

- all configured source tables must exist
- each source table must have `bank`, `fiscal_year`, and `quarter`
- at least one availability row must be derived
- the new V2 `data_source_availability` table is replaced from live source data

Use `--allow-missing-sources` only for a partial local install. Use
`--merge-availability` only when you intentionally want to preserve existing
availability rows.

## Prompt State

The clean V2 prompt workspace currently exists at `aegis-prompts/`, but it only
contains placeholder README files. The updater can push prompt YAML as soon as
those files are added. Until then, use `--allow-empty-prompts` or point
`--prompts-dir` at `archive/v1/aegis-prompts` if you want to seed the old V1
prompts for the temporary V2 bridge.
