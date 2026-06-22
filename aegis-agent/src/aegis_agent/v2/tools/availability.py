"""Data availability tool for the V2 analyst workstation."""

from __future__ import annotations

from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import yaml

from ...connections.postgres_connector import fetch_all
from ..schemas import (
    AvailabilityFilters,
    DataAvailabilityGap,
    DataAvailabilityResponse,
    DataAvailabilityRow,
    source_summaries,
)
from ..sources import SOURCE_IDS, normalize_source_ids, source_label


_PROJECT_ROOT = Path(__file__).resolve().parents[5]
_INSTITUTION_REGISTRY_PATH = _PROJECT_ROOT / "scripts" / "agent_monitored_institutions.yaml"
_INSTITUTION_REGISTRY: dict[str, dict[str, Any]] | None = None


def _category_display(value: str) -> str:
    """Convert registry category ids into UI labels."""
    normalized = str(value or "Uncategorized").strip() or "Uncategorized"
    return normalized.replace("_", " ")


def _category_key(value: str) -> str:
    """Normalize category values for filter comparison."""
    return str(value or "").strip().replace(" ", "_").lower()


def _load_institution_registry() -> dict[str, dict[str, Any]]:
    """Load monitored institution metadata once."""
    global _INSTITUTION_REGISTRY  # pylint: disable=global-statement
    if _INSTITUTION_REGISTRY is not None:
        return _INSTITUTION_REGISTRY
    if not _INSTITUTION_REGISTRY_PATH.exists():
        _INSTITUTION_REGISTRY = {}
        return _INSTITUTION_REGISTRY

    loaded = yaml.safe_load(_INSTITUTION_REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    _INSTITUTION_REGISTRY = {
        str(symbol): dict(metadata or {}) for symbol, metadata in loaded.items()
    }
    return _INSTITUTION_REGISTRY


def bank_category(bank_symbol: str, bank_tags: list[str] | None = None) -> tuple[str, str]:
    """Return display/category id for a bank symbol."""
    registry = _load_institution_registry()
    metadata = registry.get(str(bank_symbol or ""))
    if metadata and metadata.get("type"):
        category_id = str(metadata["type"])
        return _category_display(category_id), category_id

    tags = [str(tag) for tag in bank_tags or [] if str(tag).strip()]
    category_tag = next((tag for tag in tags if tag.endswith("_bank") or tag.endswith("_banks")), "")
    if category_tag:
        return _category_display(category_tag), category_tag
    return "Uncategorized", "uncategorized"


def banks_for_categories(categories: list[str]) -> set[str]:
    """Return monitored bank symbols that belong to selected categories."""
    requested = {_category_key(category) for category in categories if str(category).strip()}
    if not requested:
        return set()
    symbols = set()
    for symbol, metadata in _load_institution_registry().items():
        category_id = str(metadata.get("type") or "")
        if _category_key(category_id) in requested or _category_key(_category_display(category_id)) in requested:
            symbols.add(symbol)
    return symbols


def _array_values(value: Any) -> list[str]:
    """Normalize Postgres text[] values from async drivers."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip().strip('"') for part in value.strip("{}").split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def _source_ids_from_database_names(value: Any) -> list[str]:
    """Map availability database_names to known V2 source ids."""
    names = _array_values(value)
    return [source_id for source_id in SOURCE_IDS if source_id in names]


def _matches_keyword(row: DataAvailabilityRow, keyword: str | None) -> bool:
    """Return whether an availability row matches a UI keyword."""
    if not keyword:
        return True
    term = keyword.lower().strip()
    if not term:
        return True
    haystack = " ".join(
        [
            row.bank_name,
            row.bank_symbol,
            row.bank_category,
            row.bank_category_id,
            row.quarter,
            str(row.fiscal_year),
            " ".join(row.bank_tags),
            " ".join(source_label(source_id) for source_id in row.source_ids),
        ]
    ).lower()
    return term in haystack or _is_subsequence(term, haystack)


def _is_subsequence(needle: str, haystack: str) -> bool:
    """Tiny fuzzy match for short analyst search terms."""
    if len(needle) < 3:
        return False
    cursor = 0
    for char in haystack:
        if cursor < len(needle) and needle[cursor] == char:
            cursor += 1
    return cursor == len(needle)


def _row_from_record(record: dict[str, Any], selected_sources: list[str]) -> DataAvailabilityRow | None:
    """Convert a database record into a V2 availability row."""
    all_sources = _source_ids_from_database_names(record.get("database_names"))
    source_ids = [source_id for source_id in all_sources if source_id in selected_sources]
    if not source_ids:
        return None

    tags = _array_values(record.get("bank_tags"))
    category, category_id = bank_category(str(record.get("bank_symbol") or ""), tags)
    last_updated = record.get("last_updated")
    if last_updated is not None and not isinstance(last_updated, datetime):
        last_updated = None
    return DataAvailabilityRow(
        bank_id=int(record["bank_id"]),
        bank_name=str(record["bank_name"]),
        bank_symbol=str(record["bank_symbol"]),
        bank_category=category,
        bank_category_id=category_id,
        bank_tags=tags,
        fiscal_year=int(record["fiscal_year"]),
        quarter=str(record["quarter"]),
        source_ids=source_ids,
        last_refreshed_at=last_updated,
    )


def _build_where(filters: AvailabilityFilters, selected_category_banks: set[str]) -> tuple[str, dict[str, Any]]:
    """Build safe SQL predicates for availability filters."""
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": filters.limit}

    bank_symbols = [symbol.upper() for symbol in filters.bank_symbols]
    if selected_category_banks:
        bank_symbols = sorted(set(bank_symbols).intersection(selected_category_banks)) if bank_symbols else sorted(selected_category_banks)
    if bank_symbols:
        clauses.append("bank_symbol = ANY(:bank_symbols)")
        params["bank_symbols"] = bank_symbols
    elif filters.bank_categories:
        return "1=0", params

    if filters.fiscal_years:
        clauses.append("fiscal_year = ANY(:fiscal_years)")
        params["fiscal_years"] = filters.fiscal_years
    if filters.quarters:
        clauses.append("quarter = ANY(:quarters)")
        params["quarters"] = [quarter.upper() for quarter in filters.quarters]

    return " AND ".join(clauses), params


async def check_data_availability(filters: AvailabilityFilters | None = None) -> DataAvailabilityResponse:
    """Read and normalize aegis_data_availability for the V2 UI."""
    filters = filters or AvailabilityFilters()
    selected_sources = normalize_source_ids(filters.source_ids)
    selected_category_banks = banks_for_categories(filters.bank_categories)
    where_clause, params = _build_where(filters, selected_category_banks)

    records = await fetch_all(
        f"""
        SELECT
            bank_id,
            bank_name,
            bank_symbol,
            bank_tags,
            fiscal_year,
            quarter,
            database_names,
            last_updated
        FROM public.aegis_data_availability
        WHERE {where_clause}
        ORDER BY fiscal_year DESC, quarter DESC, bank_symbol ASC
        LIMIT :limit
        """,
        params,
    )

    rows: list[DataAvailabilityRow] = []
    for record in records:
        row = _row_from_record(record, selected_sources)
        if row and _matches_keyword(row, filters.keyword):
            rows.append(row)

    category_filters = {_category_key(category) for category in filters.bank_categories}
    if category_filters:
        rows = [row for row in rows if _category_key(row.bank_category_id) in category_filters]

    missing: list[DataAvailabilityGap] = []
    for row in rows:
        missing_sources = [source_id for source_id in selected_sources if source_id not in row.source_ids]
        if missing_sources:
            missing.append(
                DataAvailabilityGap(
                    bank_symbol=row.bank_symbol,
                    bank_name=row.bank_name,
                    fiscal_year=row.fiscal_year,
                    quarter=row.quarter,
                    missing_source_ids=missing_sources,
                )
            )

    return DataAvailabilityResponse(
        rows=rows,
        missing=missing,
        sources=source_summaries(rows),
        bank_categories=sorted({row.bank_category for row in rows}),
        fiscal_years=sorted({row.fiscal_year for row in rows}, reverse=True),
        quarters=sorted({row.quarter for row in rows}),
    )


def availability_widget_html(response: DataAvailabilityResponse) -> str:
    """Build the trusted static HTML summary for chat widgets."""
    row_count = len(response.rows)
    bank_count = len({row.bank_symbol for row in response.rows})
    source_count = len({source_id for row in response.rows for source_id in row.source_ids})
    updated_values = [
        row.last_refreshed_at
        for row in response.rows
        if isinstance(row.last_refreshed_at, datetime)
    ]
    refreshed = max(updated_values).strftime("%Y-%m-%d %H:%M") if updated_values else "Not available"

    source_bits = [
        f"<span>{escape(summary.label)}: <strong>{summary.available_rows}</strong></span>"
        for summary in response.sources
        if summary.available_rows
    ]
    source_html = "".join(source_bits) or "<span>No source coverage found</span>"

    return (
        '<div class="v2-widget-summary">'
        f"<p><strong>{row_count}</strong> bank-period rows across "
        f"<strong>{bank_count}</strong> banks and <strong>{source_count}</strong> sources.</p>"
        f'<div class="v2-widget-sources">{source_html}</div>'
        f'<p class="v2-widget-muted">Latest refresh: {escape(refreshed)}</p>'
        "</div>"
    )
