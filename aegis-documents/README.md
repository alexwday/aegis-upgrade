# Aegis Documents

Canonical raw/organized document inputs for Aegis data sources.

## Layout

```text
aegis-documents/
  investor_slides/
    2026_Q1/<BANK-REGION>/*
  supplementary_financials/
    2026_Q1/<BANK-REGION>/*
  rts/
    2026_Q1/<BANK-REGION>/*
  pillar3/
    2026_Q1/<BANK-REGION>/*
  transcripts/
```

`BANK-REGION` folder names are the canonical bank identifiers used downstream,
for example `RY-CA`, `TD-CA`, and `BMO-CA`. Pipelines should read from this
folder tree instead of each source-specific project keeping its own input copy.

## Current Drop Folders

The repository includes tracked Q2 2026 drop folders for the Canadian bank set
currently used by the agent:

```text
BMO-CA, BNS-CA, CM-CA, NA-CA, RY-CA, TD-CA
```

Place new documents under:

```text
aegis-documents/<source>/2026_Q2/<BANK-REGION>/
```

## Ownership

- This project owns source documents only.
- `aegis-pipeline` owns extraction, chunking, enrichment, embedding, and loading.
- `aegis-agent` owns retrieval, agent behavior, UI, and user-facing evidence links.
