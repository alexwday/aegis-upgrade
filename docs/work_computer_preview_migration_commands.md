# Work Computer Preview Migration Commands

Run these from the `aegis-upgrade` repo root on the work computer.

## 1. Update Code And Dependencies

```bash
git fetch origin
git switch codex/v2-ui-agent-planning
git pull --ff-only
.venv/bin/python -m pip install -r requirements-workstation.txt
```

## 2. Reprocess Sources With Changed Page Models

This updates XLSX sheet numbering to visible sheet order and rebuilds transcript
rows as generated-PDF page records.

```bash
for source in \
  supplementary_financials \
  pillar3 \
  transcripts \
  event_transcripts; do
  .venv/bin/python scripts/run_pipeline.py --source "$source"
done
```

## 3. Refresh Agent Availability

```bash
.venv/bin/python scripts/db_setup.py --refresh-availability
```

## 4. Backfill Stored Preview Bytes

Dry-run first:

```bash
.venv/bin/python scripts/backfill_source_document_previews.py --all
```

Apply the new preview renderer:

```bash
.venv/bin/python scripts/backfill_source_document_previews.py --all --apply --force
```

## 5. Test Reference Links

```bash
.venv/bin/python scripts/test_source_document_preview.py --env-file .env
```

Expected behavior:

- PDF sources open PDF previews at `#page=N`.
- XLSX sources open HTML workbook previews at `#sheet-N`.
- XML transcript sources open generated transcript PDFs at `#page=N`.
- Download original still returns the original PDF/XLSX/XML bytes.
