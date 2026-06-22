"""Tests for the V2 agent first-slice helpers."""

from __future__ import annotations

import pytest

from aegis_agent.v2.agent.deep import has_deep_scope, research_arguments
from aegis_agent.v2.agent.models import (
    EvidenceChunk,
    normalize_search_mode,
    normalize_turn,
    resolve_model_plan,
)
import aegis_agent.v2.agent.retrieval as retrieval
from aegis_agent.v2.agent.retrieval import retrieve_quick_evidence


def test_resolve_model_plan_maps_ui_modes_to_internal_tiers() -> None:
    """Small/Large UI choices should map onto the planned orchestrator/research tiers."""
    small = resolve_model_plan("small")
    large = resolve_model_plan("large")

    assert small.orchestrator_tier == "medium"
    assert small.research_tier == "small"
    assert large.orchestrator_tier == "large"
    assert large.research_tier == "medium"


def test_normalize_search_mode_accepts_old_and_new_values() -> None:
    """The websocket transition should accept old short/long and new quick/deep names."""
    assert normalize_search_mode("quick") == "quick"
    assert normalize_search_mode("short") == "quick"
    assert normalize_search_mode("deep") == "deep"
    assert normalize_search_mode("long") == "deep"


def test_normalize_turn_prefers_v2_contract_fields() -> None:
    """Normalize selected filters, optional context, model, and search mode."""
    turn = normalize_turn(
        {
            "query": "Compare CET1 trends",
            "filters": {"source_ids": ["rts", "pillar3"], "bank_symbols": ["td"]},
            "optional_context": {
                "bank_tickers": ["ry"],
                "fiscal_years": [2026],
                "quarters": ["q1"],
            },
            "model_selection": "large",
            "search_selection": "deep",
        }
    )

    assert turn.content == "Compare CET1 trends"
    assert turn.source_ids == ["rts", "pillar3"]
    assert turn.bank_symbols == ["RY"]
    assert turn.fiscal_years == [2026]
    assert turn.quarters == ["Q1"]
    assert turn.search_mode == "deep"
    assert turn.model_plan is not None
    assert turn.model_plan.orchestrator_tier == "large"


@pytest.mark.asyncio
async def test_quick_retrieval_caps_chunks_across_sources(monkeypatch) -> None:
    """Quick search should enforce the total evidence budget after all source retrievals."""

    async def fake_retrieve_mature_source(
        source_id, _turn, *, combinations, context, search_top_k
    ):
        assert combinations == [
            {"bank_symbol": "RY", "fiscal_year": 2026, "quarter": "Q1"}
        ]
        assert context == {"execution_id": "test"}
        assert search_top_k >= 8
        return [
            EvidenceChunk(
                source_name=source_id,
                source_display_name=source_id,
                chunk_id=f"{source_id}-{index}",
                chunk_content=f"{source_id} chunk {index}",
                score=float(index),
            )
            for index in range(60)
        ]

    monkeypatch.setattr(
        retrieval, "_research_context", lambda _turn: {"execution_id": "test"}
    )
    monkeypatch.setattr(
        retrieval, "_retrieve_mature_source", fake_retrieve_mature_source
    )
    turn = normalize_turn(
        {
            "content": "capital credit revenue",
            "filters": {"source_ids": ["rts", "pillar3", "transcripts"]},
            "optional_context": {
                "bank_tickers": ["RY"],
                "fiscal_years": [2026],
                "quarters": ["Q1"],
            },
        }
    )

    result = await retrieve_quick_evidence(turn)

    assert len(result.chunks) == 80
    assert result.chunks[0].score == 59


@pytest.mark.asyncio
async def test_quick_retrieval_requires_bank_period_scope() -> None:
    """Quick search should fail early without scoped bank, year, and quarter context."""
    turn = normalize_turn(
        {
            "content": "capital credit revenue",
            "filters": {"source_ids": ["rts"]},
        }
    )

    with pytest.raises(
        RuntimeError,
        match="Quick search requires selected bank, fiscal year, and quarter context.",
    ):
        await retrieve_quick_evidence(turn)


def test_deep_research_scope_and_arguments() -> None:
    """Deep research needs scoped bank/period selections and builds V1 arguments."""
    scoped = normalize_turn(
        {
            "content": "Analyze provisions",
            "filters": {"source_ids": ["rts"]},
            "optional_context": {
                "bank_tickers": ["RY"],
                "fiscal_years": [2026],
                "quarters": ["Q1"],
            },
        }
    )
    unscoped = normalize_turn(
        {"content": "Analyze provisions", "filters": {"source_ids": ["rts"]}}
    )

    assert has_deep_scope(scoped)
    assert not has_deep_scope(unscoped)
    assert research_arguments(scoped)["combinations"] == [
        {"bank_symbol": "RY", "fiscal_year": 2026, "quarter": "Q1"}
    ]
