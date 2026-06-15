# Aegis Pipeline

Consolidated ETL pipelines for Aegis document sources.

## Layout

```text
aegis-pipeline/
  sources/
    investor_slides/
      database/
        pipeline/
        utils/
        connections/
      scripts/
    supplementary_financials/
      database/
        pipeline/
        utils/
        connections/
      scripts/
    rts/
      database/
        pipeline/
        utils/
        connections/
      scripts/
    pillar3/
      database/
        pipeline/
        utils/
        connections/
      scripts/
    transcripts/
      database/
        pipeline/
        utils/
        connections/
      scripts/
```

Each source folder is copied from the working source-specific pipeline project
and keeps its own database package, prompt files, scripts, requirements, and
project metadata.

## Input Contract

Pipelines should use `/Users/alexwday/Projects/aegis-upgrade/aegis-documents` as the canonical
document input root. Source-specific project folders such as
`aegis-investor-slides` and `aegis-financial-supp` are now migration references,
not the target runtime structure.

## Runtime Contract

`aegis-agent` owns all runtime retrievers and does not import retrieval code from
legacy `/Users/alexwday/Projects/aegis`.
