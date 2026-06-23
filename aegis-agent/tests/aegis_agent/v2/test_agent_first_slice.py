"""Tests for the V2 agent first-slice helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from aegis_agent.utils.settings import config
from aegis_agent.v2.agent.deep import has_deep_scope, research_arguments
import aegis_agent.v2.agent.deep as deep
from aegis_agent.v2.agent.models import (
    normalize_search_mode,
    normalize_turn,
    resolve_model_plan,
)
import aegis_agent.v2.agent.retrieval as retrieval
from aegis_agent.v2.agent.retrieval import retrieve_quick_evidence
from aegis_agent.v2.sources import SOURCE_IDS


def test_resolve_model_plan_maps_ui_modes_to_internal_tiers() -> None:
    """Small/Large UI choices should map onto the planned orchestrator/research tiers."""
    small = resolve_model_plan("small")
    large = resolve_model_plan("large")

    assert small.orchestrator_tier == "medium"
    assert small.research_tier == "small"
    assert small.orchestrator_model == config.llm.medium.model
    assert small.research_model == config.llm.small.model
    assert large.orchestrator_tier == "large"
    assert large.research_tier == "medium"
    assert large.orchestrator_model == config.llm.large.model
    assert large.research_model == config.llm.medium.model


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
    assert turn.source_filter_explicit is True
    assert turn.optional_context_selected is True
    assert turn.model_plan is not None
    assert turn.model_plan.orchestrator_tier == "large"


def test_normalize_turn_infers_explicit_text_scope_when_filters_are_missing() -> None:
    """Explicit bank, fiscal year, and quarter text should populate research scope."""
    turn = normalize_turn(
        {
            "content": "Compare RBC Q1 2026 CET1 trends across all sources",
            "search_selection": "quick",
        }
    )

    assert turn.bank_symbols == ["RY-CA"]
    assert turn.fiscal_years == [2026]
    assert turn.quarters == ["Q1"]
    assert turn.source_filter_explicit is False
    assert turn.optional_context_selected is False


def test_normalize_turn_treats_default_ui_state_as_unselected() -> None:
    """Default all-source and empty optional-context payloads are not selections."""
    turn = normalize_turn(
        {
            "content": "hi",
            "filters": {"source_ids": list(SOURCE_IDS)},
            "optional_context": {
                "bank_symbols": [],
                "bank_categories": [],
                "fiscal_years": [],
                "quarters": [],
            },
            "context": {
                "sources": list(SOURCE_IDS),
                "search_mode": "quick",
                "model_mode": "small",
            },
        }
    )

    assert turn.source_ids == list(SOURCE_IDS)
    assert turn.source_filter_explicit is False
    assert turn.optional_context_selected is False


def test_normalize_turn_keeps_relative_periods_unresolved() -> None:
    """Relative periods should still require clarification."""
    turn = normalize_turn({"content": "Compare RBC last quarter capital trends"})

    assert turn.bank_symbols == ["RY-CA"]
    assert turn.fiscal_years == []
    assert turn.quarters == []


@pytest.mark.asyncio
async def test_quick_query_prep_uses_one_llm_call_and_one_embedding_batch() -> None:
    """Global quick prep should expand once and embed the expanded query set once."""
    captured: dict[str, object] = {"llm_calls": 0, "embedding_calls": 0}

    async def fake_call_tool_prompt(**kwargs):
        captured["llm_calls"] = int(captured["llm_calls"]) + 1
        assert kwargs["prompt_name"] == "query_prep"
        assert kwargs["max_tokens"] == 1200
        return {"expanded": True}, {"total_tokens": 12}

    def fake_normalize_prepared_query(_parsed, _prompt_input):
        return {
            "rewritten_query": "rewritten",
            "sub_queries": ["sub one", "sub two"],
            "keywords": ["cet1", "capital"],
            "metrics": ["pcl"],
            "hyde_answer": "hyde answer",
        }

    async def fake_embed_batch(*, input_texts, context):
        captured["embedding_calls"] = int(captured["embedding_calls"]) + 1
        captured["input_texts"] = input_texts
        assert context["execution_id"] == "test"
        return {
            "data": [{"embedding": [float(index)]} for index, _ in enumerate(input_texts)],
            "metrics": {"total_tokens": 20},
        }

    module = SimpleNamespace(
        call_tool_prompt=fake_call_tool_prompt,
        normalize_prepared_query=fake_normalize_prepared_query,
        embed_batch=fake_embed_batch,
        format_scope=lambda _combinations: "scope",
    )
    prepared = await retrieval._strict_prepare_query(  # pylint: disable=protected-access
        module,
        normalize_turn({"content": "capital"}),
        [{"bank_symbol": "RY", "fiscal_year": 2026, "quarter": "Q1"}],
        {"execution_id": "test"},
    )

    assert captured["llm_calls"] == 1
    assert captured["embedding_calls"] == 1
    assert captured["input_texts"] == [
        "rewritten",
        "sub one",
        "sub two",
        "cet1 capital",
        "pcl",
        "hyde answer",
    ]
    assert set(prepared["embeddings"]) == {
        "rewritten",
        "sub_query_0",
        "sub_query_1",
        "keywords",
        "metrics",
        "hyde",
    }


@pytest.mark.asyncio
async def test_quick_retrieval_caps_chunks_across_sources(monkeypatch) -> None:
    """Quick search should use one query prep and cap a narrow scope at 20 chunks."""
    captured: dict[str, object] = {
        "prep_calls": 0,
        "source_sets": [],
        "combos": [],
        "top_k": [],
    }

    async def fake_prepare_query(_module, _turn, combinations, context):
        captured["prep_calls"] = int(captured["prep_calls"]) + 1
        assert combinations == [
            {"bank_symbol": "RY", "fiscal_year": 2026, "quarter": "Q1"}
        ]
        assert context["execution_id"] == "test"
        assert context["source_filter"] == [
            "transcripts",
            "event_transcripts",
            "investor_slides",
        ]
        assert context["v2_model_plan"].research_tier == "small"
        return {"rewritten_query": "capital credit revenue"}

    async def fake_retrieve_combo_candidates(
        source_ids, combo, *, prepared, context, search_top_k
    ):
        captured["source_sets"].append(source_ids)  # type: ignore[union-attr]
        captured["combos"].append(combo)  # type: ignore[union-attr]
        captured["top_k"].append(search_top_k)  # type: ignore[union-attr]
        assert combo == {"bank_symbol": "RY", "fiscal_year": 2026, "quarter": "Q1"}
        assert prepared == {"rewritten_query": "capital credit revenue"}
        assert context["execution_id"] == "test"
        return [
            _raw_candidate(
                source_ids[index % len(source_ids)], "RY", 2026, "Q1", index
            )
            for index in range(60)
        ]

    async def fake_rerank(_module, **kwargs):
        return kwargs["candidates"]

    monkeypatch.setattr(retrieval, "_pipeline_module", lambda _source_id: object())
    monkeypatch.setattr(retrieval, "_strict_prepare_query", fake_prepare_query)
    monkeypatch.setattr(
        retrieval, "_retrieve_combo_candidates", fake_retrieve_combo_candidates
    )
    monkeypatch.setattr(retrieval, "_strict_rerank_merged_candidates", fake_rerank)
    turn = normalize_turn(
        {
            "content": "capital credit revenue",
            "filters": {"source_ids": list(SOURCE_IDS)},
            "optional_context": {
                "bank_tickers": ["RY"],
                "fiscal_years": [2026],
                "quarters": ["Q1"],
            },
        }
    )

    result = await retrieve_quick_evidence(turn, llm_context={"execution_id": "test"})

    assert captured["prep_calls"] == 1
    assert captured["source_sets"] == [
        ["transcripts", "event_transcripts", "investor_slides"]
    ]
    assert captured["combos"] == [
        {"bank_symbol": "RY", "fiscal_year": 2026, "quarter": "Q1"}
    ]
    assert captured["top_k"] == [20]
    assert len(result.chunks) == 20
    assert result.chunks[0].score == 59
    assert {chunk.source_name for chunk in result.chunks}.issubset(
        {"transcripts", "event_transcripts", "investor_slides"}
    )


def _raw_candidate(
    source_id: str, bank: str, fiscal_year: int, quarter: str, index: int
) -> dict[str, object]:
    """Build one raw V1-style retrieval row for quick tests."""
    return {
        "_quick_source_id": source_id,
        "bank": bank,
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "file_id": f"{source_id}-{bank}-{fiscal_year}-{quarter}",
        "chunk_id": f"{source_id}-{bank}-{fiscal_year}-{quarter}-{index}",
        "chunk_content": f"{bank} {quarter} {fiscal_year} chunk {index}",
        "filename": f"{source_id}.pdf",
        "name": "Capital",
        "page_number": index + 1,
        "score": float(index),
        "keywords": [],
        "metrics": [],
        "match_sources": ["content_vector"],
    }


@pytest.mark.asyncio
async def test_quick_retrieval_uses_five_chunks_per_combo_for_max_scope(
    monkeypatch,
) -> None:
    """Quick search should support at most 12 combos with a 60-chunk total cap."""
    captured: dict[str, object] = {"top_k": []}

    async def fake_prepare_query(_module, _turn, combinations, _context):
        assert len(combinations) == 12
        return {"rewritten_query": "capital"}

    async def fake_retrieve_combo_candidates(
        source_ids, combo, *, prepared, context, search_top_k
    ):
        del prepared, context
        captured["top_k"].append(search_top_k)  # type: ignore[union-attr]
        rows: list[dict[str, object]] = []
        for index in range(search_top_k * 2):
            rows.append(
                _raw_candidate(
                    source_ids[index % len(source_ids)],
                    str(combo["bank_symbol"]),
                    int(combo["fiscal_year"]),
                    str(combo["quarter"]),
                    index,
                )
            )
        return rows

    async def fake_rerank(_module, **kwargs):
        return kwargs["candidates"]

    monkeypatch.setattr(retrieval, "_pipeline_module", lambda _source_id: object())
    monkeypatch.setattr(retrieval, "_strict_prepare_query", fake_prepare_query)
    monkeypatch.setattr(
        retrieval, "_retrieve_combo_candidates", fake_retrieve_combo_candidates
    )
    monkeypatch.setattr(retrieval, "_strict_rerank_merged_candidates", fake_rerank)
    turn = normalize_turn(
        {
            "content": "capital",
            "filters": {"source_ids": ["rts", "pillar3"]},
            "optional_context": {
                "bank_tickers": ["RY", "TD", "BMO", "BNS", "CM", "NA"],
                "fiscal_years": [2026],
                "quarters": ["Q1", "Q2"],
            },
        }
    )

    result = await retrieve_quick_evidence(turn, llm_context={"execution_id": "test"})

    assert captured["top_k"] == [5] * 12
    assert len(result.chunks) == 60
    per_combo: dict[tuple[str | None, int | None, str | None], int] = {}
    for chunk in result.chunks:
        key = (chunk.bank_ticker, chunk.fiscal_year, chunk.quarter)
        per_combo[key] = per_combo.get(key, 0) + 1
    assert set(per_combo.values()) == {5}


@pytest.mark.asyncio
async def test_quick_retrieval_rejects_more_than_twelve_combos() -> None:
    """Quick search should fail before retrieval when scope is too broad."""
    turn = normalize_turn(
        {
            "content": "capital",
            "filters": {"source_ids": ["rts"]},
            "optional_context": {
                "bank_tickers": [f"B{index}" for index in range(13)],
                "fiscal_years": [2026],
                "quarters": ["Q1"],
            },
        }
    )

    with pytest.raises(RuntimeError, match="up to 12 bank-period combinations"):
        await retrieve_quick_evidence(turn, llm_context={"execution_id": "test"})


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


@pytest.mark.asyncio
async def test_deep_research_uses_supplied_llm_context(monkeypatch) -> None:
    """Deep research should pass an authenticated V2 context into the V1 tool."""
    captured: dict[str, object] = {}

    async def fake_run_research_tool(arguments, context):
        captured["arguments"] = arguments
        captured["context"] = context
        return {"status": "success", "findings": []}

    async def fake_optional_context(_filters):
        return SimpleNamespace(rows=[])

    monkeypatch.setattr(deep, "run_research_tool", fake_run_research_tool)
    monkeypatch.setattr(deep, "optional_context", fake_optional_context)
    turn = normalize_turn(
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
    llm_context = {
        "execution_id": "test-run",
        "auth_config": {"success": True, "method": "api_key", "token": "test-token"},
        "ssl_config": {"success": True, "verify": False},
    }

    result = await deep.run_deep_research(turn, llm_context=llm_context)

    assert result["status"] == "success"
    assert captured["context"] is llm_context
    assert llm_context["source_filter"] == ["rts"]
    assert llm_context["v2_model_plan"].research_tier == "small"
    assert llm_context["auth_config"]["token"] == "test-token"
