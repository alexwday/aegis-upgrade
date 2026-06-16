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

    assert "function collapseStatusTrace" in html
    assert "function moveStatusTraceIntoResponse" in html
    assert "trace-stack empty" in html
    assert "responseFrame.append(main, evidence, trace)" in html
    assert "Research trace" in html
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
    assert "function isMarkdownTableStart" in html
    assert "function consumeList" in html
    assert "research-table-wrap" in html
    assert "research-table" in html
    assert ".final-body ul" in html
    assert ".evidence-content .research-table" in html


def test_chat_template_uses_single_research_status_snapshot_board() -> None:
    """Research progress should update one status board instead of appending summaries."""
    template = Path(__file__).resolve().parents[3] / "templates" / "chat.html"
    html = template.read_text(encoding="utf-8")

    assert 'event.type === "research_status_snapshot"' in html
    assert "function renderResearchStatusSnapshot" in html
    assert "function ensureResearchStatusBoard" in html
    assert "function renderCompletedResearchSummaries" in html
    assert "research-source-strip" in html
    assert "research-summary-list" in html
    assert "research-summary-body" in html
    assert "Completed source summaries" in html
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
