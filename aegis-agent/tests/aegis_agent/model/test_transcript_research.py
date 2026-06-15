"""Tests for structured transcript research output."""

from __future__ import annotations

import pytest

from aegis_agent.model.agents.schemas import BankPeriodCombination
from aegis_agent.model.subagents.transcripts import research as transcript_research


@pytest.mark.asyncio
async def test_transcript_tool_returns_structured_findings(monkeypatch) -> None:
    """Transcript research should return findings, citations, and coverage."""

    async def fake_run_retrieval_pipeline(**_kwargs):
        return {
            "combo_results": [
                {
                    "combo": {"bank_symbol": "RY-CA", "fiscal_year": 2026, "quarter": "Q1"},
                    "findings": [
                        {
                            "finding": "Management said credit quality was resilient.",
                            "page": 1,
                            "location_detail": "Q&A exchange 1",
                            "source_ref_ids": ["S1"],
                            "references": [
                                {
                                    "filename": "RY-CA_Q1_2026_E1_123_1.xml",
                                    "file_type": "xml",
                                    "page": 1,
                                    "location": "Q&A exchange 1",
                                    "link_marker": "{{S3_LINK:download:xml:RY-CA_Q1_2026_E1_123_1.xml:RY-CA source}}",
                                }
                            ],
                        }
                    ],
                    "expanded_chunks": [{"name": "Q&A exchange 1", "chunk_id": "page_1.1"}],
                }
            ]
        }

    monkeypatch.setattr(transcript_research, "run_retrieval_pipeline", fake_run_retrieval_pipeline)
    result = await transcript_research.research_transcripts(
        question="credit quality",
        combinations=[BankPeriodCombination(bank_symbol="RY-CA", fiscal_year=2026, quarter="Q1")],
        context={"execution_id": "test"},
    )

    assert result.status == "success"
    assert result.findings[0].combo_label == "RY-CA Q1 2026"
    assert result.citations[0].text_excerpt.startswith("Management said")
    assert result.coverage[0].chunk_count == 1
