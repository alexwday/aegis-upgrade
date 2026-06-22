"""Report downloader and scheduler tools for Aegis V2."""

from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

from fastapi import HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError

from ...connections.postgres_connector import fetch_all, fetch_one
from ..mock_data import mock_report_html, mock_report_search_response
from ..schemas import (
    ReportSearchResponse,
    ReportSubscription,
    ReportSubscriptionRequest,
    ReportSubscriptionResponse,
    ReportSummary,
)
from .availability import bank_category, banks_for_categories
from .documents import _table_exists  # Reuse the safe existence probe.


_SUBSCRIPTIONS: dict[str, ReportSubscription] = {}


def _category_key(value: str) -> str:
    """Normalize category values for comparison."""
    return str(value or "").strip().replace(" ", "_").lower()


def _preview_url(report_id: int) -> str:
    """Return preview URL for a generated report."""
    return f"/api/v2/reports/{report_id}/preview"


def _download_url(report_id: int) -> str:
    """Return download URL for a generated report."""
    return f"/api/v2/reports/{report_id}/download"


def _report_from_record(record: dict[str, Any]) -> ReportSummary:
    """Convert an aegis_reports row into V2 report metadata."""
    symbol = str(record.get("bank_symbol") or "")
    category, _ = bank_category(symbol, [])
    generated_at = record.get("generation_date")
    if not isinstance(generated_at, datetime):
        generated_at = datetime.utcnow()
    report_id = int(record["id"])
    return ReportSummary(
        id=report_id,
        title=str(record.get("report_name") or f"{symbol} report"),
        description=str(record.get("report_description") or ""),
        report_type=str(record.get("report_type") or "report"),
        bank_id=int(record.get("bank_id") or 0),
        bank_name=str(record.get("bank_name") or ""),
        bank_symbol=symbol,
        bank_category=category,
        fiscal_year=int(record.get("fiscal_year") or 0),
        quarter=str(record.get("quarter") or ""),
        generated_at=generated_at,
        preview_url=_preview_url(report_id),
        download_url=_download_url(report_id),
    )


async def list_reports(
    *,
    bank_categories: list[str] | None = None,
    bank_symbols: list[str] | None = None,
    fiscal_years: list[int] | None = None,
    quarters: list[str] | None = None,
    report_types: list[str] | None = None,
    limit: int = 100,
) -> ReportSearchResponse:
    """Return pre-generated reports from aegis_reports when available."""
    if not await _table_exists("public.aegis_reports"):
        return ReportSearchResponse()

    category_banks = banks_for_categories(bank_categories or [])
    selected_banks = [symbol.upper() for symbol in bank_symbols or [] if symbol]
    if category_banks:
        selected_banks = sorted(set(selected_banks).intersection(category_banks)) if selected_banks else sorted(category_banks)
    if bank_categories and not selected_banks:
        return ReportSearchResponse()

    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": limit}
    if selected_banks:
        clauses.append("bank_symbol = ANY(:bank_symbols)")
        params["bank_symbols"] = selected_banks
    if fiscal_years:
        clauses.append("fiscal_year = ANY(:fiscal_years)")
        params["fiscal_years"] = fiscal_years
    if quarters:
        clauses.append("quarter = ANY(:quarters)")
        params["quarters"] = [quarter.upper() for quarter in quarters]
    if report_types:
        clauses.append("report_type = ANY(:report_types)")
        params["report_types"] = report_types

    try:
        records = await fetch_all(
            f"""
            SELECT
                id,
                report_name,
                report_description,
                report_type,
                bank_id,
                bank_name,
                bank_symbol,
                fiscal_year,
                quarter,
                generation_date
            FROM public.aegis_reports
            WHERE {" AND ".join(clauses)}
            ORDER BY generation_date DESC, fiscal_year DESC, quarter DESC, bank_symbol ASC
            LIMIT :limit
            """,
            params,
        )
    except SQLAlchemyError:
        return ReportSearchResponse()

    reports = [_report_from_record(record) for record in records]
    category_filters = {_category_key(category) for category in bank_categories or []}
    if category_filters:
        reports = [
            report
            for report in reports
            if _category_key(report.bank_category) in category_filters
        ]
    return ReportSearchResponse(
        reports=reports,
        report_types=sorted({report.report_type for report in reports}),
    )


async def report_html_response(report_id: int, *, download: bool = False) -> Response:
    """Return report content as browser-previewable HTML."""
    mock_report = mock_report_html(report_id)
    if mock_report is not None:
        title, html = mock_report
        headers = {"Cache-Control": "no-store"}
        if download:
            filename = title.replace("/", "_").replace("\\", "_").replace('"', "_")
            headers["Content-Disposition"] = f'attachment; filename="{filename}.html"'
        return Response(content=html, media_type="text/html", headers=headers)

    if not await _table_exists("public.aegis_reports"):
        raise HTTPException(status_code=404, detail="Reports table is not available")
    row = await fetch_one(
        """
        SELECT report_name, markdown_content
        FROM public.aegis_reports
        WHERE id = :report_id
        LIMIT 1
        """,
        {"report_id": report_id},
    )
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")

    title = str(row.get("report_name") or f"Report {report_id}")
    markdown = str(row.get("markdown_content") or "")
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:Inter,Arial,sans-serif;margin:32px;color:#17202a;"
        "line-height:1.5;max-width:980px}pre{white-space:pre-wrap;background:#f5f7fa;"
        "padding:16px;border:1px solid #d8e1eb}</style></head><body>"
        f"<h1>{escape(title)}</h1><pre>{escape(markdown or 'No report body is available.')}</pre>"
        "</body></html>"
    )
    headers = {"Cache-Control": "no-store"}
    if download:
        filename = title.replace("/", "_").replace("\\", "_").replace('"', "_")
        headers["Content-Disposition"] = f'attachment; filename="{filename}.html"'
    return Response(content=html, media_type="text/html", headers=headers)


async def report_scheduler_state() -> ReportSubscriptionResponse:
    """Return available report types and current in-process subscriptions."""
    mock_response = mock_report_search_response(limit=500)
    report_types = sorted(set(mock_response.report_types) | {"earnings_update", "peer_comparison", "capital_credit"})
    return ReportSubscriptionResponse(
        report_types=report_types,
        subscriptions=list(_SUBSCRIPTIONS.values()),
    )


async def subscribe_to_report(request: ReportSubscriptionRequest) -> ReportSubscriptionResponse:
    """Create an in-process report subscription placeholder."""
    subscription = ReportSubscription(**request.model_dump())
    _SUBSCRIPTIONS[subscription.id] = subscription
    return await report_scheduler_state()
