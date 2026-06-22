"""Tests for V2 final response shell construction."""

from __future__ import annotations

from aegis_agent.v2.agent.final_response import build_final_shell
from aegis_agent.v2.agent.models import EvidenceChunk, normalize_turn


def test_deep_final_shell_prefers_source_backed_metric_tiles() -> None:
    """Structured deep findings should produce evidence-backed metric tiles."""
    turn = normalize_turn(
        {
            "content": "Compare capital metrics",
            "model_selection": "large",
            "search_selection": "deep",
        }
    )
    shell = build_final_shell(
        turn,
        mode="deep",
        research_result={
            "findings": [
                {
                    "combo_label": "Reports to shareholders: RY Q1 2026",
                    "summary": "RBC reported a CET1 ratio of 13.2%.",
                    "metric": {
                        "metric_name": "CET1 ratio",
                        "metric_value": "13.2",
                        "unit": "%",
                        "period": "Q1 2026",
                        "segment": "Enterprise",
                    },
                    "evidence_refs": [{"evidence_id": "E1"}],
                }
            ]
        },
    )

    assert shell.tiles[0].label == "CET1 ratio"
    assert shell.tiles[0].value == "13.2%"
    assert "Q1 2026" in str(shell.tiles[0].context)
    assert shell.tiles[0].evidence_ids == ["E1"]


def test_quick_final_shell_extracts_metric_tiles_from_evidence_chunks() -> None:
    """Quick evidence chunks should produce source-backed metric tiles when possible."""
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
    shell = build_final_shell(
        turn,
        mode="quick",
        chunks=[
            EvidenceChunk(
                source_name="rts",
                source_display_name="Reports to Shareholders",
                bank_ticker="RY",
                fiscal_year=2026,
                quarter="Q1",
                file_name="rbc-q1-2026.pdf",
                page_number=12,
                chunk_id="chunk-1",
                chunk_content="The bank reported that CET1 ratio was 13.2% in the quarter.",
            )
        ],
    )

    assert shell.tiles[0].label == "CET1 ratio"
    assert shell.tiles[0].value == "13.2%"
    assert shell.tiles[0].evidence_ids == ["rts:chunk-1"]
    assert "RY" in str(shell.tiles[0].context)
