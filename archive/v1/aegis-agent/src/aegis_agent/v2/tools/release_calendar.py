"""Release calendar data for Aegis V2."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from ...connections.postgres_connector import fetch_all
from ..schemas import ReleaseCalendarResponse, ReleaseEvent
from ..sources import SOURCE_IDS
from .availability import bank_category, banks_for_categories
from .documents import _table_exists


def _month_bounds(month: str | None) -> tuple[datetime, datetime, str]:
    """Return inclusive/exclusive month bounds for YYYY-MM input."""
    now = datetime.utcnow()
    raw = month or f"{now.year:04d}-{now.month:02d}"
    year, month_number = [int(part) for part in raw.split("-", 1)]
    start = datetime(year, month_number, 1)
    if month_number == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month_number + 1, 1)
    return start, end, f"{year:04d}-{month_number:02d}"


def _category_key(value: str) -> str:
    """Normalize category values for comparison."""
    return str(value or "").strip().replace(" ", "_").lower()


async def list_release_events(
    *,
    month: str | None = None,
    bank_categories: list[str] | None = None,
    bank_symbols: list[str] | None = None,
    event_types: list[str] | None = None,
    fiscal_years: list[int] | None = None,
    quarters: list[str] | None = None,
    limit: int = 500,
) -> ReleaseCalendarResponse:
    """Return compact monthly release events from existing Aegis metadata."""
    start, end, normalized_month = _month_bounds(month)
    requested_types = {event_type for event_type in event_types or [] if event_type}
    selected_years = {int(year) for year in fiscal_years or []}
    selected_quarters = {quarter.upper() for quarter in quarters or [] if quarter}
    category_banks = banks_for_categories(bank_categories or [])
    selected_banks = [symbol.upper() for symbol in bank_symbols or [] if symbol]
    if category_banks:
        selected_banks = sorted(set(selected_banks).intersection(category_banks)) if selected_banks else sorted(category_banks)
    if bank_categories and not selected_banks:
        return ReleaseCalendarResponse(month=normalized_month)

    events: list[ReleaseEvent] = []

    if not requested_types or "data_refresh" in requested_types:
        clauses = ["last_updated >= :start", "last_updated < :end"]
        params: dict[str, Any] = {"start": start, "end": end, "limit": limit}
        if selected_banks:
            clauses.append("bank_symbol = ANY(:bank_symbols)")
            params["bank_symbols"] = selected_banks
        if selected_years:
            clauses.append("fiscal_year = ANY(:fiscal_years)")
            params["fiscal_years"] = sorted(selected_years)
        if selected_quarters:
            clauses.append("quarter = ANY(:quarters)")
            params["quarters"] = sorted(selected_quarters)
        try:
            rows = await fetch_all(
                f"""
                SELECT bank_symbol, bank_name, fiscal_year, quarter, database_names, last_updated
                FROM public.aegis_data_availability
                WHERE {" AND ".join(clauses)}
                ORDER BY last_updated ASC
                LIMIT :limit
                """,
                params,
            )
        except SQLAlchemyError:
            rows = []
        for index, row in enumerate(rows):
            symbol = str(row.get("bank_symbol") or "")
            category, _ = bank_category(symbol, [])
            database_names = row.get("database_names") or []
            source_id = next((source for source in SOURCE_IDS if source in database_names), None)
            events.append(
                ReleaseEvent(
                    id=f"refresh_{symbol}_{row.get('fiscal_year')}_{row.get('quarter')}_{index}",
                    event_type="data_refresh",
                    title=f"{symbol} {row.get('quarter')} {row.get('fiscal_year')} data refreshed",
                    event_date=row.get("last_updated") or start,
                    bank_symbol=symbol,
                    bank_name=str(row.get("bank_name") or ""),
                    bank_category=category,
                    fiscal_year=int(row.get("fiscal_year") or 0),
                    quarter=str(row.get("quarter") or ""),
                    source_id=source_id,
                )
            )

    if (not requested_types or "report_generated" in requested_types) and await _table_exists("public.aegis_reports"):
        clauses = ["generation_date >= :start", "generation_date < :end"]
        params = {"start": start, "end": end, "limit": limit}
        if selected_banks:
            clauses.append("bank_symbol = ANY(:bank_symbols)")
            params["bank_symbols"] = selected_banks
        if selected_years:
            clauses.append("fiscal_year = ANY(:fiscal_years)")
            params["fiscal_years"] = sorted(selected_years)
        if selected_quarters:
            clauses.append("quarter = ANY(:quarters)")
            params["quarters"] = sorted(selected_quarters)
        try:
            rows = await fetch_all(
                f"""
                SELECT id, report_name, report_type, bank_symbol, bank_name, fiscal_year, quarter, generation_date
                FROM public.aegis_reports
                WHERE {" AND ".join(clauses)}
                ORDER BY generation_date ASC
                LIMIT :limit
                """,
                params,
            )
        except SQLAlchemyError:
            rows = []
        for row in rows:
            symbol = str(row.get("bank_symbol") or "")
            category, _ = bank_category(symbol, [])
            events.append(
                ReleaseEvent(
                    id=f"report_{row.get('id')}",
                    event_type="report_generated",
                    title=str(row.get("report_name") or f"{symbol} report generated"),
                    event_date=row.get("generation_date") or start,
                    bank_symbol=symbol,
                    bank_name=str(row.get("bank_name") or ""),
                    bank_category=category,
                    fiscal_year=int(row.get("fiscal_year") or 0),
                    quarter=str(row.get("quarter") or ""),
                )
            )

    category_filters = {_category_key(category) for category in bank_categories or []}
    if category_filters:
        events = [event for event in events if _category_key(event.bank_category) in category_filters]
    if selected_years:
        events = [event for event in events if event.fiscal_year in selected_years]
    if selected_quarters:
        events = [event for event in events if (event.quarter or "").upper() in selected_quarters]

    events = sorted(events, key=lambda event: event.event_date)[:limit]
    return ReleaseCalendarResponse(
        month=normalized_month,
        events=events,
        event_types=sorted({event.event_type for event in events} | {"data_refresh", "report_generated"}),
    )
