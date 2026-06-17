"""Static UI checks for evidence-linked citations."""

from __future__ import annotations

from pathlib import Path


def test_chat_template_uses_structured_evidence_links() -> None:
    """The UI should not regex-parse parenthetical citations into duplicate chips."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert "function extractCitations" not in html
    assert "renderEvidenceMarkers" in html
    assert "evidence-link" in html
    assert "data-evidence-id" in html


def test_chat_template_uses_declared_final_response_shell() -> None:
    """The UI should render the agent-declared shell instead of inferring layout."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert 'event.type === "final_response_start"' in html
    assert "function startFinalResponse" in html
    assert "function extractMetrics" not in html
    assert "function usesDefaultBriefStructure" not in html
    assert "function renderResearchAnswerCard" not in html
    assert "function renderFinalAnswer" not in html


def test_chat_template_keeps_research_trace_below_evidence() -> None:
    """Research trace should get its own response-frame container, not evidence tabs."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert "function removeStatusTrace" in html
    assert "responseFrame.append(main, evidence)" in html
    assert "turn.evidence.appendChild(panel)" in html
    assert "className = \"evidence-stack empty\"" in html
    assert "reasoning-tab" not in html
    assert "ensureReasoningTab" not in html
    assert "Reasoning" not in html


def test_chat_template_themes_generated_interactive_parts_dark_blue() -> None:
    """Generated and interactive response pieces should share the dark blue theme."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert "--generated-surface" in html
    assert html.count("background: var(--generated-surface)") >= 6
    for selector in (
        ".metric-card",
        ".citation-chip",
        ".evidence-panel",
        ".status-panel",
        ".main-response.status-feed",
        ".choice",
    ):
        assert selector in html


def test_chat_template_renders_research_tables_and_lists() -> None:
    """Research markdown tables should render as styled tables, not pipe text."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert "function renderMarkdownTable" in html
    assert "function renderTableCellMarkdown" in html
    assert "function isMarkdownTableStart" in html
    assert "function consumeList" in html
    assert "research-table-wrap" in html
    assert "research-table" in html
    assert ".final-body ul" in html
    assert ".evidence-content .research-table" in html
    assert 'renderInlineMarkdown(row[cellIndex] || "", evidenceRegistry)' not in html


def test_chat_template_uses_single_research_status_snapshot_board() -> None:
    """Research progress should update one status board instead of appending summaries."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert 'event.type === "research_status_snapshot"' in html
    assert "function renderResearchStatusSnapshot" in html
    assert "function ensureResearchStatusBoard" in html
    assert "function indexCompletedResearchSummaries" in html
    assert "snapshot?.headline" in html
    assert "researchSnapshotStarted" in html
    assert "!options.research || !turn.researchSnapshotStarted" in html
    assert '`${completedSources}/${totalSources} sources`' in html
    assert "research-status-board" in html
    assert "research-status-table" in html
    assert "research-summary-body" in html
    assert "completed_summaries" in html
    assert "Current step" not in html


def test_chat_template_sends_source_filter_with_user_payloads() -> None:
    """The composer should expose source filters and attach them to websocket payloads."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert "Data source filter" in html
    assert 'value="transcripts"' in html
    assert 'value="event_transcripts"' in html
    assert "function getSelectedSourceFilter" in html
    assert "source_filter: sourceFilter" in html


def test_chat_template_keeps_choice_cards_compact_and_completes_status() -> None:
    """Choice cards should render as compact controls and close their status trace."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert "max-height: 74px" in html
    assert "grid-template-columns: repeat(auto-fit, minmax(240px, 1fr))" in html
    assert "function completeChoiceTurn" in html
    assert "completeChoiceTurn(turn)" in html
    assert 'turn.statusText.textContent = "Selection received"' in html


def test_chat_template_renders_json_chart_artifacts_without_images() -> None:
    """Inline charts should hydrate from structured JSON specs, not image asset URLs."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert 'event.type === "chart_artifact"' in html
    assert "function mergeChartArtifact" in html
    assert "function renderChartGraphic" in html
    assert "function renderPeerBarChart" in html
    assert "function renderPeerRankBarChart" in html
    assert "function renderTrendLineChart" in html
    assert "function renderMultiSeriesLineChart" in html
    assert "function renderSlopegraphChart" in html
    assert "function renderDeltaBarChart" in html
    assert "function renderHeatmapChart" in html
    assert "function renderCompositionChart" in html
    assert "function renderWaterfallChart" in html
    assert "function renderScatterPlotChart" in html
    assert "function renderSmallMultiplePanelChart" in html
    assert "function chartPoints" in html
    assert "function chartSeries" in html
    assert "renderEvidenceIdChips" not in html
    assert 'Interactive source-grounded chart</span><span>' not in html
    assert "artifact?.spec?.x_label" in html
    assert "artifact?.spec?.y_label" in html
    assert "Chart data unavailable" in html
    assert 'artifact.spec.chart_type === "peer_rank_bar"' in html
    assert 'artifact.spec.chart_type === "multi_series_line"' in html
    assert 'artifact.spec.chart_type === "slopegraph"' in html
    assert 'artifact.spec.chart_type === "delta_bar"' in html
    assert 'artifact.spec.chart_type === "composition_stacked_bar"' in html
    assert 'artifact.spec.chart_type === "waterfall"' in html
    assert 'artifact.spec.chart_type === "scatter_plot"' in html
    assert 'artifact.spec.chart_type === "small_multiple_panel"' in html
    assert 'artifact.spec.chart_type === "trend_bar"' in html
    assert "chartArtifacts: {}" in html
    assert "asset_url" not in html
    assert "<img src=" not in html


def test_chat_template_draws_planner_point_specs() -> None:
    """Planner-authored point specs should be normalized before SVG rendering."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert "function chartPointFacts" in html
    assert "function periodPartsFromLabel" in html
    assert "function chartRowPeriodRank" in html
    assert "point.label || point.period_label || point.bank_label" in html
    assert "return renderPeerBarChart(artifact, facts.length ? facts : pointFacts);" in html
    assert "return renderTrendLineChart(artifact, facts.length ? facts : pointFacts);" in html
    assert "return renderHeatmapChart(artifact, facts.length ? facts : pointFacts);" in html


def test_chart_planner_prompt_avoids_mixed_metric_single_axis_charts() -> None:
    """Broad key-metric comparisons should not become one shared-axis graph."""
    prompt = (
        Path(__file__).resolve().parents[4]
        / "aegis-prompts"
        / "aegis_agent"
        / "chart_planner.yaml"
    )
    text = prompt.read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "Do not combine different metrics or units on a single shared-axis chart" in normalized
    assert "small_multiple_panel" in text
    assert "one metric per panel" in normalized
    assert "every point must include a label" in normalized
