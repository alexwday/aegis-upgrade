"""Catalog table reads for the V2 analyst workstation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...connections.postgres_connector import fetch_all
from ..schemas import (
    AvailabilityFilters,
    DataAvailabilityGap,
    DataAvailabilityResponse,
    DataAvailabilityRow,
    DataSourceRegistryItem,
    DataSourceRegistryResponse,
    SourceSummary,
)


def _source_list(value: Any) -> list[str]:
    """Normalize Postgres text[] values."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip().strip('"') for part in value.strip("{}").split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def _category_key(value: str) -> str:
    """Normalize category values for filter comparison."""
    return str(value or "").strip().replace(" ", "_").lower()


def _build_optional_context_where(filters: AvailabilityFilters) -> tuple[str, dict[str, Any]]:
    """Build safe SQL predicates for optional-context reads."""
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": filters.limit}

    if filters.source_ids:
        clauses.append("availability.data_source_list && :source_ids")
        params["source_ids"] = filters.source_ids
    if filters.bank_symbols:
        clauses.append("availability.bank_ticker = ANY(:bank_symbols)")
        params["bank_symbols"] = filters.bank_symbols
    if filters.bank_categories:
        clauses.append("institution.bank_category = ANY(:bank_categories)")
        params["bank_categories"] = filters.bank_categories
    if filters.fiscal_years:
        clauses.append("availability.fiscal_year = ANY(:fiscal_years)")
        params["fiscal_years"] = filters.fiscal_years
    if filters.quarters:
        clauses.append("availability.quarter = ANY(:quarters)")
        params["quarters"] = [quarter.upper() for quarter in filters.quarters]

    return " AND ".join(clauses), params


async def list_data_sources() -> DataSourceRegistryResponse:
    """Return source registry rows for the filters popover."""
    records = await fetch_all(
        """
        SELECT
            data_source_name,
            data_source_display_name,
            data_source_description
        FROM public.data_source_registry
        ORDER BY
            CASE data_source_name
                WHEN 'rts' THEN 1
                WHEN 'pillar3' THEN 2
                WHEN 'supplementary_financials' THEN 3
                WHEN 'investor_slides' THEN 4
                WHEN 'transcripts' THEN 5
                WHEN 'event_transcripts' THEN 6
                ELSE 99
            END,
            data_source_display_name ASC,
            data_source_name ASC
        """
    )
    return DataSourceRegistryResponse(
        data_sources=[DataSourceRegistryItem(**record) for record in records]
    )


async def optional_context(filters: AvailabilityFilters | None = None) -> DataAvailabilityResponse:
    """Return bank/period/source availability from the V2 catalog tables."""
    filters = filters or AvailabilityFilters(limit=2000)
    where_clause, params = _build_optional_context_where(filters)
    registry = await list_data_sources()
    source_labels = {
        source.data_source_name: source.data_source_display_name
        for source in registry.data_sources
    }

    records = await fetch_all(
        f"""
        SELECT
            dense_rank() OVER (ORDER BY institution.bank_ticker) AS bank_id,
            institution.bank_name,
            institution.bank_display_name,
            institution.bank_ticker,
            institution.bank_category,
            availability.fiscal_year,
            availability.quarter,
            availability.data_source_list,
            availability.updated_at
        FROM public.data_source_availability availability
        JOIN public.monitored_institutions institution
          ON institution.bank_ticker = availability.bank_ticker
        WHERE {where_clause}
        ORDER BY
            availability.fiscal_year DESC,
            availability.quarter DESC,
            institution.bank_category ASC,
            institution.bank_ticker ASC
        LIMIT :limit
        """,
        params,
    )

    selected_sources = filters.source_ids or list(source_labels)
    rows: list[DataAvailabilityRow] = []
    for record in records:
        source_ids = [
            source_id
            for source_id in _source_list(record.get("data_source_list"))
            if source_id in selected_sources
        ]
        if not source_ids:
            continue
        refreshed = record.get("updated_at")
        if refreshed is not None and not isinstance(refreshed, datetime):
            refreshed = None
        rows.append(
            DataAvailabilityRow(
                bank_id=int(record["bank_id"]),
                bank_name=str(record["bank_display_name"]),
                bank_symbol=str(record["bank_ticker"]),
                bank_category=str(record["bank_category"]),
                bank_category_id=str(record["bank_category"]),
                bank_tags=[],
                fiscal_year=int(record["fiscal_year"]),
                quarter=str(record["quarter"]),
                source_ids=source_ids,
                last_refreshed_at=refreshed,
            )
        )

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

    counts = {source_id: 0 for source_id in source_labels}
    for row in rows:
        for source_id in row.source_ids:
            if source_id in counts:
                counts[source_id] += 1

    return DataAvailabilityResponse(
        rows=rows,
        missing=missing,
        sources=[
            SourceSummary(
                id=source_id,
                label=source_labels[source_id],
                available_rows=counts[source_id],
            )
            for source_id in source_labels
        ],
        bank_categories=sorted({row.bank_category for row in rows}),
        fiscal_years=sorted({row.fiscal_year for row in rows}, reverse=True),
        quarters=sorted({row.quarter for row in rows}),
    )
