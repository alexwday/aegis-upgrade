"""Phase 0 tests: V2 deep research resolves availability from the catalog."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import aegis_agent.v2.agent.deep as deep
from aegis_agent.v2.agent.deep import resolve_v2_availability, run_deep_research
from aegis_agent.v2.agent.models import normalize_turn


def _row(bank_symbol: str, bank_name: str, fiscal_year: int, quarter: str, source_ids):
    return SimpleNamespace(
        bank_symbol=bank_symbol,
        bank_name=bank_name,
        fiscal_year=fiscal_year,
        quarter=quarter,
        source_ids=list(source_ids),
    )


def _turn():
    return normalize_turn(
        {
            "content": "Analyze provisions",
            "filters": {"source_ids": ["rts", "pillar3"]},
            "optional_context": {
                "bank_tickers": ["RY"],
                "fiscal_years": [2026],
                "quarters": ["Q1"],
            },
        }
    )


@pytest.mark.asyncio
async def test_resolve_v2_availability_builds_index(monkeypatch) -> None:
    """Catalog rows become a (source, year, quarter) -> identifier-set index."""

    async def fake_optional_context(_filters):
        return SimpleNamespace(
            rows=[
                _row("RY-CA", "Royal Bank of Canada", 2026, "Q1", ["rts", "pillar3"]),
            ]
        )

    monkeypatch.setattr(deep, "optional_context", fake_optional_context)

    index = await resolve_v2_availability(_turn())

    assert index[("rts", 2026, "Q1")] == {"ry-ca", "royal bank of canada"}
    assert index[("pillar3", 2026, "Q1")] == {"ry-ca", "royal bank of canada"}
    assert ("transcripts", 2026, "Q1") not in index


@pytest.mark.asyncio
async def test_resolve_v2_availability_degrades_on_catalog_failure(monkeypatch) -> None:
    """A catalog read failure yields an empty index instead of raising."""

    async def boom(_filters):
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(deep, "optional_context", boom)

    index = await resolve_v2_availability(_turn())

    assert index == {}


@pytest.mark.asyncio
async def test_run_deep_research_injects_v2_availability(monkeypatch) -> None:
    """run_deep_research passes the resolved catalog index into the research context."""
    captured: dict[str, object] = {}

    async def fake_optional_context(_filters):
        return SimpleNamespace(
            rows=[_row("RY-CA", "Royal Bank of Canada", 2026, "Q1", ["rts"])]
        )

    async def fake_run_research_tool(arguments, context):
        captured["context"] = context
        return {"status": "success", "findings": []}

    monkeypatch.setattr(deep, "optional_context", fake_optional_context)
    monkeypatch.setattr(deep, "run_research_tool", fake_run_research_tool)

    llm_context = {
        "execution_id": "test-run",
        "auth_config": {"success": True, "method": "api_key", "token": "test-token"},
        "ssl_config": {"success": True, "verify": False},
    }
    result = await run_deep_research(_turn(), llm_context=llm_context)

    assert result["status"] == "success"
    index = captured["context"]["v2_available_combinations"]
    assert index[("rts", 2026, "Q1")] == {"ry-ca", "royal bank of canada"}
