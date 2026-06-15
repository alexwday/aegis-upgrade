"""Tests for deterministic status summaries."""

from __future__ import annotations

from aegis_agent.model.agents.schemas import ProgressEvent
from aegis_agent.model.agents.status_reporter import (
    build_research_status_snapshot,
    summarize_progress,
)


def test_status_reporter_summarizes_only_observed_stages() -> None:
    """Progress summaries should not invent unavailable or completed stages."""
    summary = summarize_progress(
        [
            ProgressEvent(
                source="transcripts",
                stage="research_started",
                status="started",
                message="Research started.",
                combo_label="RY-CA Q1 2026",
            )
        ]
    )

    assert summary == "Research in progress for RY-CA Q1 2026."
    assert "Unavailable" not in summary
    assert "Completed" not in summary


def test_status_reporter_includes_partial_and_complete_events() -> None:
    """Progress summaries should reflect missing and completed combinations."""
    summary = summarize_progress(
        [
            ProgressEvent(
                source="transcripts",
                stage="partial_availability",
                status="complete",
                message="No transcript data.",
                combo_label="TD-CA Q1 2026",
            ),
            ProgressEvent(
                source="transcripts",
                stage="combo_complete",
                status="complete",
                message="Done.",
                combo_label="RY-CA Q1 2026",
            ),
        ]
    )

    assert "Unavailable data: TD-CA Q1 2026." in summary
    assert "Completed research for RY-CA Q1 2026." in summary


def test_status_reporter_includes_source_findings_metadata() -> None:
    """Completed source metadata should surface concise findings in body summaries."""
    summary = summarize_progress(
        [
            ProgressEvent(
                source="investor_slides",
                stage="research_started",
                status="started",
                message="Investor slides research started.",
                combo_label="Investor slides: RY-CA Q1 2026",
            ),
            ProgressEvent(
                source="investor_slides",
                stage="combo_complete",
                status="complete",
                message="Investor slides research completed.",
                combo_label="Investor slides: RY-CA Q1 2026",
            ),
            ProgressEvent(
                source="investor_slides",
                stage="research_strategy",
                status="running",
                message="Investor slides search focus: metrics: CET1; keywords: capital.",
            ),
            ProgressEvent(
                source="investor_slides",
                stage="source_complete",
                status="complete",
                message="Investor slides completed.",
                metadata={
                    "source_label": "Investor slides",
                    "findings": [
                        {
                            "combo_label": "Investor slides: RY-CA Q1 2026",
                            "summary": "CET1 ratio was 13.7% with stable capital generation.",
                        }
                    ],
                },
            ),
        ]
    )

    assert "Completed research for Investor slides: RY-CA Q1 2026." in summary
    assert "Approach: Investor slides search focus: metrics: CET1; keywords: capital." in summary
    assert "Findings so far:" in summary
    assert "Investor slides found RY-CA Q1 2026: CET1 ratio was 13.7%" in summary


def test_status_snapshot_builds_source_rows_and_completed_summary() -> None:
    """Live research status snapshots should expose source rows and completed synthesis."""
    events = [
        ProgressEvent(
            source="investor_slides",
            stage="source_queued",
            status="pending",
            message="Investor slides queued.",
            metadata={"source_label": "Investor slides", "combination_count": 2},
        ),
        ProgressEvent(
            source="rts",
            stage="source_queued",
            status="pending",
            message="Reports to shareholders queued.",
            metadata={"source_label": "Reports to shareholders", "combination_count": 2},
        ),
        ProgressEvent(
            source="investor_slides",
            stage="source_complete",
            status="complete",
            message="Investor slides research completed with 1 finding(s).",
            metadata={
                "status": "success",
                "source_label": "Investor slides",
                "finding_count": 1,
                "gap_count": 0,
                "findings": [
                    {
                        "combo_label": "Investor slides: RY-CA Q1 2026",
                        "summary": "Capital remained strong and credit metrics were stable.",
                        "finding_type": "summary",
                    },
                    {
                        "combo_label": "Investor slides: RY-CA Q1 2026",
                        "summary": "CET1 ratio was 13.7%.",
                        "finding_type": "quantitative",
                    }
                ],
            },
        ),
    ]

    snapshot = build_research_status_snapshot(events)

    assert snapshot["completed_summaries"] == [
        {
            "source_id": "investor_slides",
            "source_label": "Investor slides",
            "status": "success",
            "status_label": "Complete",
            "quick_summary": "Investor slides research completed with 1 finding(s).",
            "summary_text": "RY-CA Q1 2026: Capital remained strong and credit metrics were stable.",
            "finding_count": 1,
            "gap_count": 0,
            "completed_at": events[-1].timestamp.isoformat(),
        }
    ]
    assert snapshot["completed_source_count"] == 1
    assert snapshot["total_source_count"] == 2
    assert snapshot["rows"][0]["source_label"] == "Investor slides"
    assert snapshot["rows"][0]["status"] == "complete"
    assert snapshot["rows"][0]["finding_count"] == 1
    assert snapshot["rows"][1]["source_label"] == "Reports to shareholders"
    assert snapshot["rows"][1]["status"] == "pending"
