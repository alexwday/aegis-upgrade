# UI-First Design Plan

Aegis V2 should behave like an analyst research workstation, not a generic chat
box. The UI should make scope, coverage, evidence, uncertainty, comparisons, and
next actions visible. The agent should operate that workstation.

## Design Thesis

The UI defines the product contract. Every major UI control or state should map
to a backend capability:

- If the UI lets a user select banks and periods, the backend needs scope
  resolution and validation.
- If the UI shows source coverage, the backend needs a coverage tool.
- If the UI shows evidence beside an answer, the backend needs stable evidence
  IDs and citation validation.
- If the UI offers charts or tables, the backend needs structured numeric
  retrieval and chart-ready output.
- If the UI shows agent work in progress, the backend needs explicit research
  plan and progress events.

## Core Screens

### Research Workspace

Primary screen for asking, scoping, running, and reviewing research.

Expected regions:

- Scope rail: banks, periods, sources, report type, saved presets.
- Composer: natural-language ask plus explicit scope chips.
- Research plan: pending/running/completed steps with tool names and source
  coverage.
- Answer surface: concise synthesis, table/chart blocks, and caveats.
- Evidence panel: source cards, excerpts, pages/sheets, and citation links.
- Follow-up tray: suggested next analyses and user-pinned follow-ups.

### Coverage Explorer

Shows what data exists before or during a request.

Expected capabilities:

- Matrix by bank, quarter, year, and source.
- Missing-source explanation.
- Quick filters for complete coverage, partial coverage, and source-specific
  coverage.
- Launch research from selected coverage cells.

### Evidence Drilldown

Lets the user inspect why Aegis said something.

Expected capabilities:

- Evidence list grouped by source and bank-period.
- Original source location: page, sheet, section, or transcript segment.
- Chunk text, summary, extracted metrics, and confidence notes.
- Open related neighboring chunks.
- Pin evidence into the current answer or export set.

### Compare View

Dedicated surface for peer comparison and trend work.

Expected capabilities:

- Bank rows and metric columns.
- Period selector for point-in-time or trend.
- Source selector and source-priority display.
- Chart/table toggle.
- Highlight differences, deltas, missing values, and source conflicts.

## UI Feature To Tool Contract Map

| UI feature | Backend capability implied |
| --- | --- |
| Bank/period chips | `resolve_scope` and `validate_scope` |
| Source coverage matrix | `check_data_availability` |
| Source filter controls | source-aware retrieval routing |
| Research plan timeline | `create_research_plan` and progress events |
| Evidence side panel | `get_evidence`, stable evidence IDs, source links |
| Neighboring source context | `get_evidence_neighborhood` |
| Chart/table blocks | structured metric retrieval and chart-ready result schema |
| Peer compare mode | `compare_metrics` and cross-bank normalization |
| Trend mode | `retrieve_metric_time_series` |
| Citation audit state | `audit_citations` |
| Follow-up suggestions | `suggest_followups` from result gaps and evidence |
| User-pinned evidence | session evidence registry |

## Business Example Template

Use this template for each new business requirement:

```text
User example:

Desired UI behavior:

Required controls:

Required answer blocks:

Evidence standard:

Clarification behavior:

Retrieval/tools implied:

Failure or partial-data behavior:
```

## First Design Pass

The first UI design pass should answer these questions before agent code is
rewritten:

1. What are the default visible scopes: bank, period, source, topic, output type?
2. Should Aegis start from a blank composer, a coverage explorer, or saved
   workflows?
3. Which output blocks are first-class: narrative brief, peer table, metric
   card, trend chart, evidence list, source conflict note?
4. What should the user be able to correct after a result appears?
5. Which actions should run immediately and which require confirmation?
6. What does "good enough evidence" mean for each answer type?
7. How should partial coverage be displayed without blocking useful work?

