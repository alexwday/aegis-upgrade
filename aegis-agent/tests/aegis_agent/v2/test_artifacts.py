"""Tests for V2 research artifact HTML builders."""

from __future__ import annotations

from aegis_agent.v2.agent.artifacts import deep_research_html, quick_research_html
from aegis_agent.v2.agent.models import EvidenceChunk, normalize_turn


def test_quick_artifact_groups_by_source_file_and_links_source_preview() -> None:
    """Quick artifacts should expose retained chunks by source and file."""
    turn = normalize_turn(
        {
            "content": "What changed in capital?",
            "filters": {"source_ids": ["rts"]},
            "optional_context": {
                "bank_tickers": ["RY"],
                "fiscal_years": [2026],
                "quarters": ["Q1"],
            },
        }
    )
    html = quick_research_html(
        turn,
        [
            EvidenceChunk(
                source_name="rts",
                source_display_name="Reports to Shareholders",
                bank_ticker="RY",
                fiscal_year=2026,
                quarter="Q1",
                file_id="file-1",
                file_name="rbc-q1-2026.pdf",
                page_number=12,
                chunk_id="chunk-1",
                chunk_content="The bank reported a CET1 ratio of 13.2%.",
            )
        ],
        ["Pillar 3 data unavailable for the selected period."],
    )

    assert "Reports to Shareholders (1)" in html
    assert "rbc-q1-2026.pdf" in html
    assert "RY / Q1 / 2026 / p. 12" in html
    assert "/source-documents/rts/file-1/preview#page=12" in html
    assert "/source-documents/rts/file-1/download" in html
    assert "Pillar 3 data unavailable for the selected period." in html


def test_deep_artifact_renders_metric_table_and_citation_links() -> None:
    """Deep artifacts should render structured research payloads."""
    turn = normalize_turn(
        {
            "content": "Compare capital trends",
            "model_selection": "large",
            "search_selection": "deep",
        }
    )
    html = deep_research_html(
        turn,
        {
            "quick_summary": "RBC capital remained above management targets.",
            "findings": [
                {
                    "combo_label": "Reports to Shareholders: RY Q1 2026",
                    "summary": "RBC reported CET1 of 13.2%.",
                    "metric": {
                        "metric_name": "CET1 ratio",
                        "metric_value": "13.2",
                        "unit": "%",
                        "period": "Q1 2026",
                        "segment": "Enterprise",
                    },
                    "evidence_refs": [
                        {"evidence_id": "E1", "display_label": "RTS p. 12"}
                    ],
                    "table": {
                        "title": "Capital",
                        "columns": ["Metric", "Value"],
                        "rows": [{"Metric": "CET1", "Value": "13.2%"}],
                    },
                }
            ],
            "citations": [
                {
                    "evidence_id": "E1",
                    "source_id": "rts",
                    "file_id": "file-1",
                    "filename": "rbc-q1-2026.pdf",
                    "page_number": 12,
                    "display_label": "RTS p. 12",
                    "text_excerpt": "RBC reported CET1 of 13.2%.",
                }
            ],
        },
        [],
        ["No transcript data was available."],
    )

    assert "Deep search artifact" in html
    assert "CET1 ratio | 13.2% | Q1 2026 | Enterprise" in html
    assert "E1 - RTS p. 12" in html
    assert "<table>" in html
    assert "Capital" in html
    assert "/source-documents/rts/file-1/preview#page=12" in html
    assert "/source-documents/rts/file-1/download" in html
    assert "No transcript data was available." in html
