# Retrieval Tooling Plan

The current V1 retrievers are strict and source-specific. V2 can move toward
shared retrieval tools, but the safest path is a common retrieval interface over
source adapters, not an immediate monolithic rewrite.

## Current Shape

The durable contract is strong:

- Each source has a data table.
- Each source has an embeddings table.
- Data tables share core fields such as source type, fiscal year, quarter, bank,
  file metadata, page/sheet-like location, summary, chunk ID, chunk content,
  keywords, metrics, and embeddings.
- Embedding tables share core fields for embedding type, scope, source metadata,
  content unit IDs, embedding text, model, dimensions, and vector.

The source-specific parts are still real:

- Table names differ.
- Some sources are page-oriented while supplements and Pillar 3 can be
  sheet-oriented.
- Gap fill differs by source.
- Prompts differ by source.
- Metadata quality and location semantics differ by source.

## Cross-Source Retriever Answer

Yes, a cross-source retriever is feasible, but it should probably be a retrieval
broker rather than one giant retriever.

Recommended shape:

```text
agent tool
  -> retrieval broker
    -> source registry
    -> generic hybrid search
    -> source adapter for table names, gap fill, location labels, and prompts
    -> unified evidence/result contract
```

This lets V2 search across all source data tables when useful, while preserving
source-specific behavior where it matters.

## Candidate Retrieval Tools

### `resolve_scope`

Normalize user language into bank-period-source scope.

Returns:

- banks with IDs/symbols/names
- fiscal years and quarters
- requested sources
- unresolved scope questions

### `check_data_availability`

Read `aegis_data_availability` and return source coverage before research.

Returns:

- available bank-period-source combinations
- missing combinations
- partial coverage summary
- suggested source filters

### `search_evidence`

Generic hybrid search across one or more source tables.

Inputs:

- query text
- bank-period combinations
- sources
- top K
- retrieval modes: vector, BM25, keyword, metric, section summary

Returns:

- normalized evidence chunks
- scores by strategy
- source metadata
- location metadata

### `get_evidence_neighborhood`

Fetch neighboring chunks, pages, sheets, or transcript segments around selected
evidence.

Returns:

- before/after chunks
- same page/sheet/section chunks
- location-aware ordering

### `extract_metrics`

Extract or normalize metric observations from selected evidence.

Returns:

- metric name
- value
- unit
- period
- segment
- source evidence IDs
- confidence and conflicts

### `compare_metrics`

Build cross-bank or cross-period comparison output from structured metric
observations.

Returns:

- rows and columns
- deltas
- missing values
- source conflicts
- chart-ready series

### `audit_citations`

Validate that answer claims are supported by evidence IDs.

Returns:

- supported claims
- weak claims
- unsupported claims
- suggested citations

## Implementation Path

1. Define a `SourceConfig` object for runtime retrieval, reusing the root source
   registry where possible.
2. Define a normalized `EvidenceChunk` schema independent of source.
3. Build a generic `search_evidence` function parameterized by `SourceConfig`.
4. Keep source-specific gap fill as adapters.
5. Build a broker that can call generic search across multiple sources.
6. Expose broker operations as agent tools.
7. Gradually retire duplicated per-source retrieval code after parity tests.

## Source Adapter Responsibilities

Adapters should be small and explicit:

- data table name
- embeddings table name
- source display label
- location type: page, sheet, transcript segment, section
- gap-fill strategy
- source-specific prompt keys
- result metadata normalization

## UI-Driven Priorities

Do not build retrieval tools in isolation. Build them when a UI state requires
them:

- Coverage matrix first implies `check_data_availability`.
- Evidence panel first implies normalized evidence IDs and `get_evidence`.
- Compare view first implies metric extraction and normalization.
- Chart blocks imply chart-ready structured result schemas.
- Research timeline implies plan/progress events.

## Open Questions

1. Should cross-source search rank all chunks in one shared pool, or rank within
   each source first and then fuse?
2. Should source priority be user-controlled, agent-controlled, or workflow
   controlled?
3. How strict should exact metric retrieval be before the agent is allowed to
   synthesize a numeric comparison?
4. Should the UI show raw chunk scores, simplified relevance labels, or neither?
5. Should retrieval output include source conflicts as first-class objects?

