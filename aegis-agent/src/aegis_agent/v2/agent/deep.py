"""Deep research adapter for the first V2 agent slice."""

from __future__ import annotations

from typing import Any

from ...model.agents.research import run_research_tool
from ...utils.logging import get_logger
from ..schemas import AvailabilityFilters
from ..tools.catalog import optional_context
from .llm_context import build_llm_context
from .models import NormalizedTurn


def has_deep_scope(turn: NormalizedTurn) -> bool:
    """Return whether the selected context is narrow enough for V1-style deep research."""
    return bool(turn.bank_symbols and turn.fiscal_years and turn.quarters)


async def resolve_v2_availability(turn: NormalizedTurn) -> dict[tuple[str, int, str], set[str]]:
    """Resolve catalog availability for the deep research scope.

    Returns a map from ``(source_id, fiscal_year, quarter)`` to the set of
    lowercased bank identifiers (symbol and display name) that have data for that
    source and period, read from the V2 catalog tables (``data_source_availability``
    joined to ``monitored_institutions``) rather than the legacy
    ``aegis_data_availability`` table. On a catalog read failure the map is empty,
    which degrades deep research to a controlled "no coverage" gap instead of an
    exception.
    """
    filters = AvailabilityFilters(
        source_ids=turn.source_ids,
        bank_symbols=turn.bank_symbols,
        fiscal_years=turn.fiscal_years,
        quarters=turn.quarters,
        limit=2000,
    )
    index: dict[tuple[str, int, str], set[str]] = {}
    try:
        response = await optional_context(filters)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        get_logger().warning(
            "v2.deep.availability_resolve_failed",
            run_uuid=turn.run_uuid,
            error=str(exc),
        )
        return index
    for row in response.rows:
        identifiers = {
            identifier
            for identifier in (str(row.bank_symbol).lower(), str(row.bank_name).lower())
            if identifier
        }
        for source_id in row.source_ids:
            key = (source_id, int(row.fiscal_year), str(row.quarter).upper())
            index.setdefault(key, set()).update(identifiers)
    return index


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
    context["v2_available_combinations"] = await resolve_v2_availability(turn)
    return await run_research_tool(research_arguments(turn), context)
