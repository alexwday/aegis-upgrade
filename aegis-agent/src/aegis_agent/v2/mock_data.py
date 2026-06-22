"""Mock V2 contract data for the analyst workstation prototype."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Any
from urllib.parse import quote

from .schemas import (
    AvailabilityFilters,
    DataAvailabilityGap,
    DataAvailabilityResponse,
    DataAvailabilityRow,
    DocumentFilters,
    DocumentSearchResponse,
    DocumentSummary,
    ReleaseCalendarResponse,
    ReleaseEvent,
    ReportSearchResponse,
    ReportSummary,
    source_summaries,
)
from .sources import SOURCE_IDS, normalize_source_ids, source_label


MOCK_BANKS: tuple[dict[str, Any], ...] = (
    {
        "bank_id": 101,
        "bank_symbol": "RY-CA",
        "bank_name": "Royal Bank of Canada",
        "bank_category": "Canadian Banks",
        "bank_category_id": "canadian_banks",
        "bank_tags": ["canadian_bank", "g_sib"],
    },
    {
        "bank_id": 102,
        "bank_symbol": "TD-CA",
        "bank_name": "Toronto-Dominion Bank",
        "bank_category": "Canadian Banks",
        "bank_category_id": "canadian_banks",
        "bank_tags": ["canadian_bank", "g_sib"],
    },
    {
        "bank_id": 103,
        "bank_symbol": "BMO-CA",
        "bank_name": "Bank of Montreal",
        "bank_category": "Canadian Banks",
        "bank_category_id": "canadian_banks",
        "bank_tags": ["canadian_bank"],
    },
    {
        "bank_id": 201,
        "bank_symbol": "JPM",
        "bank_name": "JPMorgan Chase & Co.",
        "bank_category": "US Money Center Banks",
        "bank_category_id": "us_money_center_banks",
        "bank_tags": ["us_bank", "g_sib"],
    },
    {
        "bank_id": 202,
        "bank_symbol": "BAC",
        "bank_name": "Bank of America",
        "bank_category": "US Money Center Banks",
        "bank_category_id": "us_money_center_banks",
        "bank_tags": ["us_bank", "g_sib"],
    },
    {
        "bank_id": 203,
        "bank_symbol": "WFC",
        "bank_name": "Wells Fargo",
        "bank_category": "US Money Center Banks",
        "bank_category_id": "us_money_center_banks",
        "bank_tags": ["us_bank"],
    },
)

MOCK_PERIODS: tuple[tuple[int, str], ...] = ((2026, "Q1"), (2025, "Q4"))
MOCK_BASE_SOURCES: tuple[str, ...] = (
    "supplementary_financials",
    "investor_slides",
    "rts",
    "transcripts",
)
MOCK_EXTRA_SOURCES: dict[tuple[str, int, str], tuple[str, ...]] = {
    ("RY-CA", 2026, "Q1"): ("pillar3", "event_transcripts"),
    ("TD-CA", 2026, "Q1"): ("event_transcripts",),
    ("BMO-CA", 2025, "Q4"): ("pillar3",),
    ("JPM", 2026, "Q1"): ("pillar3", "event_transcripts"),
    ("BAC", 2025, "Q4"): ("pillar3",),
    ("WFC", 2026, "Q1"): ("event_transcripts",),
}

MOCK_REPORT_TYPES: tuple[str, ...] = ("earnings_update", "peer_comparison", "capital_credit")


def _category_key(value: str) -> str:
    return str(value or "").strip().replace(" ", "_").lower()


def _bank(symbol: str) -> dict[str, Any]:
    return next(bank for bank in MOCK_BANKS if bank["bank_symbol"] == symbol)


def _preview_url(source_id: str, file_id: str) -> str:
    return f"/source-documents/{quote(source_id, safe='')}/{quote(file_id, safe='')}/preview"


def _download_url(source_id: str, file_id: str) -> str:
    return f"/source-documents/{quote(source_id, safe='')}/{quote(file_id, safe='')}/download"


def _file_type(source_id: str) -> str:
    if source_id == "supplementary_financials":
        return "xlsx"
    if source_id in {"transcripts", "event_transcripts"}:
        return "html"
    return "pdf"


def _filename(source_id: str, bank_symbol: str, fiscal_year: int, quarter: str) -> str:
    prefix = bank_symbol.lower().replace("-", "_")
    suffixes = {
        "supplementary_financials": "supplementary_financials.xlsx",
        "investor_slides": "investor_slides.pdf",
        "rts": "report_to_shareholders.pdf",
        "pillar3": "pillar3_disclosures.pdf",
        "transcripts": "earnings_call_transcript.html",
        "event_transcripts": "investor_event_transcript.html",
    }
    suffix = suffixes.get(source_id, f"{source_id}.{_file_type(source_id)}")
    return f"{prefix}_{quarter.lower()}_{fiscal_year}_{suffix}"


def _mock_updated_at(bank_index: int, period_index: int, source_index: int) -> datetime:
    day = 12 + ((bank_index + period_index + source_index) % 4)
    hour = 9 + ((bank_index + source_index) % 7)
    return datetime(2026, 6, day, hour, 30, tzinfo=timezone.utc)


def _source_ids_for(bank_symbol: str, fiscal_year: int, quarter: str) -> tuple[str, ...]:
    extras = MOCK_EXTRA_SOURCES.get((bank_symbol, fiscal_year, quarter), ())
    return tuple(dict.fromkeys((*MOCK_BASE_SOURCES, *extras)))


def _build_mock_documents() -> tuple[DocumentSummary, ...]:
    documents: list[DocumentSummary] = []
    for bank_index, bank in enumerate(MOCK_BANKS):
        symbol = str(bank["bank_symbol"])
        for period_index, (fiscal_year, quarter) in enumerate(MOCK_PERIODS):
            for source_index, source_id in enumerate(_source_ids_for(symbol, fiscal_year, quarter)):
                file_id = f"mock-{source_id}-{fiscal_year}-{quarter.lower()}-{symbol.lower()}"
                documents.append(
                    DocumentSummary(
                        source_id=source_id,
                        source_label=source_label(source_id),
                        file_id=file_id,
                        bank_symbol=symbol,
                        bank_category=str(bank["bank_category"]),
                        fiscal_year=str(fiscal_year),
                        quarter=quarter,
                        filename=_filename(source_id, symbol, fiscal_year, quarter),
                        file_type=_file_type(source_id),
                        preview_url=_preview_url(source_id, file_id),
                        download_url=_download_url(source_id, file_id),
                        preview_status="ready",
                        updated_at=_mock_updated_at(bank_index, period_index, source_index),
                    )
                )
    return tuple(documents)


MOCK_DOCUMENTS: tuple[DocumentSummary, ...] = _build_mock_documents()


def _is_subsequence(needle: str, haystack: str) -> bool:
    if len(needle) < 3:
        return False
    cursor = 0
    for char in haystack:
        if cursor < len(needle) and needle[cursor] == char:
            cursor += 1
    return cursor == len(needle)


def _matches_keyword(document: DocumentSummary, keyword: str | None) -> bool:
    term = str(keyword or "").strip().lower()
    if not term:
        return True
    haystack = " ".join(
        [
            document.source_id,
            document.source_label,
            document.file_id,
            document.bank_symbol,
            document.bank_category,
            document.fiscal_year,
            document.quarter,
            document.filename,
            document.file_type,
        ]
    ).lower()
    return term in haystack or _is_subsequence(term, haystack)


def _filtered_documents(filters: DocumentFilters) -> list[DocumentSummary]:
    selected_sources = normalize_source_ids(filters.source_ids)
    bank_symbols = {symbol.upper() for symbol in filters.bank_symbols}
    category_filters = {_category_key(category) for category in filters.bank_categories}
    fiscal_years = {str(year) for year in filters.fiscal_years}
    quarters = {quarter.upper() for quarter in filters.quarters}

    documents = []
    for document in MOCK_DOCUMENTS:
        if document.source_id not in selected_sources:
            continue
        if bank_symbols and document.bank_symbol.upper() not in bank_symbols:
            continue
        if category_filters and _category_key(document.bank_category) not in category_filters:
            continue
        if fiscal_years and document.fiscal_year not in fiscal_years:
            continue
        if quarters and document.quarter.upper() not in quarters:
            continue
        if not _matches_keyword(document, filters.keyword):
            continue
        documents.append(document)
    return sorted(
        documents,
        key=lambda document: (
            document.source_id,
            int(document.fiscal_year),
            document.quarter,
            document.bank_symbol,
            document.filename,
        ),
    )


def mock_document_search_response(filters: DocumentFilters | None = None) -> DocumentSearchResponse:
    """Return file-explorer mock documents using the public response contract."""
    filters = filters or DocumentFilters()
    documents = _filtered_documents(filters)
    return DocumentSearchResponse(documents=documents[: filters.limit], total=len(documents))


def mock_availability_response(filters: AvailabilityFilters | None = None) -> DataAvailabilityResponse:
    """Return source coverage derived from the mock file catalog."""
    filters = filters or AvailabilityFilters()
    selected_sources = normalize_source_ids(filters.source_ids)
    document_filters = DocumentFilters(
        source_ids=selected_sources,
        bank_symbols=filters.bank_symbols,
        bank_categories=filters.bank_categories,
        fiscal_years=filters.fiscal_years,
        quarters=filters.quarters,
        keyword=filters.keyword,
        limit=500,
    )
    documents = _filtered_documents(document_filters)
    grouped: dict[tuple[str, str, str], list[DocumentSummary]] = {}
    for document in documents:
        grouped.setdefault((document.bank_symbol, document.fiscal_year, document.quarter), []).append(document)

    rows: list[DataAvailabilityRow] = []
    for (bank_symbol, fiscal_year, quarter), group in grouped.items():
        bank = _bank(bank_symbol)
        source_ids = [source_id for source_id in SOURCE_IDS if any(document.source_id == source_id for document in group)]
        if not source_ids:
            continue
        updated_values = [document.updated_at for document in group if document.updated_at]
        rows.append(
            DataAvailabilityRow(
                bank_id=int(bank["bank_id"]),
                bank_name=str(bank["bank_name"]),
                bank_symbol=bank_symbol,
                bank_category=str(bank["bank_category"]),
                bank_category_id=str(bank["bank_category_id"]),
                bank_tags=list(bank["bank_tags"]),
                fiscal_year=int(fiscal_year),
                quarter=quarter,
                source_ids=source_ids,
                last_refreshed_at=max(updated_values) if updated_values else None,
            )
        )

    rows = sorted(rows, key=lambda row: (row.fiscal_year, row.quarter, row.bank_symbol), reverse=True)[: filters.limit]
    missing = [
        DataAvailabilityGap(
            bank_symbol=row.bank_symbol,
            bank_name=row.bank_name,
            fiscal_year=row.fiscal_year,
            quarter=row.quarter,
            missing_source_ids=[source_id for source_id in selected_sources if source_id not in row.source_ids],
        )
        for row in rows
        if any(source_id not in row.source_ids for source_id in selected_sources)
    ]
    return DataAvailabilityResponse(
        rows=rows,
        missing=missing,
        sources=source_summaries(rows),
        bank_categories=sorted({row.bank_category for row in rows}),
        fiscal_years=sorted({row.fiscal_year for row in rows}, reverse=True),
        quarters=sorted({row.quarter for row in rows}),
    )


def _month_bounds(month: str | None) -> tuple[datetime, datetime, str]:
    raw = month or "2026-06"
    year, month_number = [int(part) for part in raw.split("-", 1)]
    start = datetime(year, month_number, 1, tzinfo=timezone.utc)
    if month_number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month_number + 1, 1, tzinfo=timezone.utc)
    return start, end, f"{year:04d}-{month_number:02d}"


def _mock_report_rows() -> tuple[ReportSummary, ...]:
    reports: list[ReportSummary] = []
    report_id = 9001
    for index, bank in enumerate(MOCK_BANKS[:4]):
        for report_type in MOCK_REPORT_TYPES:
            symbol = str(bank["bank_symbol"])
            reports.append(
                ReportSummary(
                    id=report_id,
                    title=f"{symbol} {report_type.replace('_', ' ').title()}",
                    description=f"Mock {report_type.replace('_', ' ')} report for {bank['bank_name']}.",
                    report_type=report_type,
                    bank_id=int(bank["bank_id"]),
                    bank_name=str(bank["bank_name"]),
                    bank_symbol=symbol,
                    bank_category=str(bank["bank_category"]),
                    fiscal_year=2026,
                    quarter="Q1",
                    generated_at=datetime(2026, 6, 17 + (index % 3), 10 + index, 0, tzinfo=timezone.utc),
                    preview_url=f"/api/v2/reports/{report_id}/preview",
                    download_url=f"/api/v2/reports/{report_id}/download",
                )
            )
            report_id += 1
    return tuple(reports)


MOCK_REPORTS: tuple[ReportSummary, ...] = _mock_report_rows()


def mock_report_search_response(
    *,
    bank_categories: list[str] | None = None,
    bank_symbols: list[str] | None = None,
    fiscal_years: list[int] | None = None,
    quarters: list[str] | None = None,
    report_types: list[str] | None = None,
    limit: int = 100,
) -> ReportSearchResponse:
    """Return mock generated-report metadata."""
    selected_banks = {symbol.upper() for symbol in bank_symbols or [] if symbol}
    selected_categories = {_category_key(category) for category in bank_categories or [] if category}
    selected_years = {int(year) for year in fiscal_years or []}
    selected_quarters = {quarter.upper() for quarter in quarters or [] if quarter}
    selected_types = {report_type for report_type in report_types or [] if report_type}
    reports = [
        report
        for report in MOCK_REPORTS
        if (not selected_banks or report.bank_symbol.upper() in selected_banks)
        and (not selected_categories or _category_key(report.bank_category) in selected_categories)
        and (not selected_years or report.fiscal_year in selected_years)
        and (not selected_quarters or report.quarter.upper() in selected_quarters)
        and (not selected_types or report.report_type in selected_types)
    ]
    reports = sorted(reports, key=lambda report: report.generated_at, reverse=True)
    return ReportSearchResponse(
        reports=reports[:limit],
        report_types=sorted({report.report_type for report in reports} | set(MOCK_REPORT_TYPES)),
    )


def mock_release_calendar_response(
    *,
    month: str | None = None,
    bank_categories: list[str] | None = None,
    bank_symbols: list[str] | None = None,
    event_types: list[str] | None = None,
    fiscal_years: list[int] | None = None,
    quarters: list[str] | None = None,
    limit: int = 500,
) -> ReleaseCalendarResponse:
    """Return mock release-calendar events using the public response contract."""
    start, end, normalized_month = _month_bounds(month)
    selected_banks = {symbol.upper() for symbol in bank_symbols or [] if symbol}
    selected_categories = {_category_key(category) for category in bank_categories or [] if category}
    selected_types = {event_type for event_type in event_types or [] if event_type}
    selected_years = {int(year) for year in fiscal_years or []}
    selected_quarters = {quarter.upper() for quarter in quarters or [] if quarter}
    events: list[ReleaseEvent] = []

    for document in MOCK_DOCUMENTS:
        updated_at = document.updated_at
        if not updated_at or not (start <= updated_at < end):
            continue
        if selected_banks and document.bank_symbol.upper() not in selected_banks:
            continue
        if selected_categories and _category_key(document.bank_category) not in selected_categories:
            continue
        if selected_years and int(document.fiscal_year) not in selected_years:
            continue
        if selected_quarters and document.quarter.upper() not in selected_quarters:
            continue
        if selected_types and "data_refresh" not in selected_types:
            continue
        bank = _bank(document.bank_symbol)
        events.append(
            ReleaseEvent(
                id=f"mock-refresh-{document.file_id}",
                event_type="data_refresh",
                title=f"{document.bank_symbol} {document.quarter} {document.fiscal_year} {document.source_label} refreshed",
                event_date=updated_at,
                bank_symbol=document.bank_symbol,
                bank_name=str(bank["bank_name"]),
                bank_category=document.bank_category,
                fiscal_year=int(document.fiscal_year),
                quarter=document.quarter,
                source_id=document.source_id,
            )
        )

    for report in MOCK_REPORTS:
        if not (start <= report.generated_at < end):
            continue
        if selected_banks and report.bank_symbol.upper() not in selected_banks:
            continue
        if selected_categories and _category_key(report.bank_category) not in selected_categories:
            continue
        if selected_years and report.fiscal_year not in selected_years:
            continue
        if selected_quarters and report.quarter.upper() not in selected_quarters:
            continue
        if selected_types and "report_generated" not in selected_types:
            continue
        events.append(
            ReleaseEvent(
                id=f"mock-report-{report.id}",
                event_type="report_generated",
                title=f"{report.bank_symbol} {report.report_type.replace('_', ' ')} generated",
                event_date=report.generated_at,
                bank_symbol=report.bank_symbol,
                bank_name=report.bank_name,
                bank_category=report.bank_category,
                fiscal_year=report.fiscal_year,
                quarter=report.quarter,
            )
        )

    events = sorted(events, key=lambda event: (event.event_date, event.bank_symbol or "", event.title))[:limit]
    return ReleaseCalendarResponse(
        month=normalized_month,
        events=events,
        event_types=["data_refresh", "report_generated"],
    )


def mock_source_document_record(source_id: str, file_id: str) -> dict[str, Any] | None:
    """Return a byte-row shaped mock source document for existing preview routes."""
    document = next(
        (item for item in MOCK_DOCUMENTS if item.source_id == source_id and item.file_id == file_id),
        None,
    )
    if document is None:
        return None
    html = mock_source_document_html(document)
    encoded = html.encode("utf-8")
    return {
        "filename": document.filename,
        "mime_type": "text/html",
        "preview_mime_type": "text/html",
        "preview_bytes": encoded,
        "original_bytes": encoded,
        "preview_error": None,
    }


def mock_source_document_html(document: DocumentSummary) -> str:
    """Build a self-contained HTML preview for one mock source document."""
    label = source_label(document.source_id)
    title = f"{document.bank_symbol} {document.quarter} {document.fiscal_year} {label}"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:Inter,Arial,sans-serif;margin:32px;color:#17202a;"
        "line-height:1.5;max-width:920px}header{border-bottom:1px solid #d8e1eb;"
        "margin-bottom:24px;padding-bottom:16px}.meta{display:grid;grid-template-columns:repeat(4,1fr);"
        "gap:10px;margin:18px 0}.meta div{border:1px solid #d8e1eb;background:#f8fafc;"
        "padding:10px}table{border-collapse:collapse;width:100%;margin-top:18px}td,th{border:1px solid #d8e1eb;"
        "padding:8px;text-align:left}th{background:#eef2f7}</style></head><body>"
        f"<header><p>Mock source document</p><h1>{escape(title)}</h1>"
        f"<p>{escape(document.filename)}</p></header>"
        "<section class='meta'>"
        f"<div><strong>Source</strong><br>{escape(label)}</div>"
        f"<div><strong>Bank</strong><br>{escape(document.bank_symbol)}</div>"
        f"<div><strong>Period</strong><br>{escape(document.quarter)} {escape(document.fiscal_year)}</div>"
        f"<div><strong>Type</strong><br>{escape(document.file_type.upper())}</div>"
        "</section>"
        "<p>This mock preview defines the V2 file explorer contract: source, file id, bank, "
        "category, fiscal period, filename, preview URL, download URL, and preview status.</p>"
        "<table><thead><tr><th>Metric</th><th>Mock value</th><th>Commentary</th></tr></thead><tbody>"
        "<tr><td>Revenue trend</td><td>+4.2%</td><td>Illustrative period-over-period growth.</td></tr>"
        "<tr><td>Expense trend</td><td>+2.1%</td><td>Controlled operating expense growth.</td></tr>"
        "<tr><td>Capital position</td><td>Stable</td><td>Capital levels remain within management target range.</td></tr>"
        "</tbody></table></body></html>"
    )


def mock_report_html(report_id: int) -> tuple[str, str] | None:
    """Return a mock report title/body pair for the report preview route."""
    report = next((item for item in MOCK_REPORTS if item.id == report_id), None)
    if report is None:
        return None
    title = report.title
    body = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:Inter,Arial,sans-serif;margin:32px;color:#17202a;"
        "line-height:1.55;max-width:980px}section{margin-top:22px}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #d8e1eb;"
        "padding:8px;text-align:left}th{background:#eef2f7}</style></head><body>"
        f"<h1>{escape(title)}</h1>"
        f"<p>{escape(report.description)}</p>"
        "<section><h2>Mock Summary</h2><p>This generated artifact preview uses the same report "
        "contract that the downloader and scheduler widgets consume.</p></section>"
        "<section><h2>Highlights</h2><table><tr><th>Theme</th><th>Mock read-through</th></tr>"
        "<tr><td>Revenue</td><td>Growth remains broad-based across lending and fee income.</td></tr>"
        "<tr><td>Credit</td><td>Provisioning is elevated but stable versus prior quarter.</td></tr>"
        "<tr><td>Capital</td><td>Capital ratios remain above internal operating ranges.</td></tr>"
        "</table></section></body></html>"
    )
    return title, body
