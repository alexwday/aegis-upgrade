"""Deep research adapter for the first V2 agent slice."""

from __future__ import annotations

from typing import Any

from ...model.agents.research import run_research_tool
from .llm_context import build_llm_context
from .models import NormalizedTurn


def has_deep_scope(turn: NormalizedTurn) -> bool:
    """Return whether the selected context is narrow enough for V1-style deep research."""
    return bool(turn.bank_symbols and turn.fiscal_years and turn.quarters)


def research_arguments(turn: NormalizedTurn) -> dict[str, Any]:
    """Build V1 research tool arguments from V2 UI filters."""
    combinations = [
        {
            "bank_symbol": bank,
            "fiscal_year": year,
            "quarter": quarter,
        }
        for bank in turn.bank_symbols
        for year in turn.fiscal_years
        for quarter in turn.quarters
    ]
    return {
        "question": turn.content,
        "combinations": combinations,
        "sources": turn.source_ids,
    }


async def run_deep_research(
    turn: NormalizedTurn, llm_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run the configured deep research path."""
    context = llm_context or await build_llm_context(
        turn.run_uuid or "v2-deep-research", "deep research"
    )
    context.setdefault("source_filter", turn.source_ids)
    context.setdefault("v2_model_plan", turn.model_plan)
    return await run_research_tool(research_arguments(turn), context)
