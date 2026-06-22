"""FastAPI routes for the Aegis V2 workstation."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .mock_data import (
    mock_availability_response,
    mock_document_search_response,
    mock_release_calendar_response,
    mock_report_search_response,
)
from .orchestrator import V2SessionState, event, run_turn
from .schemas import AvailabilityFilters, DataAvailabilityResponse, DocumentFilters, DocumentSearchResponse
from .tools.availability import check_data_availability
from .tools.documents import list_documents
from .tools.release_calendar import list_release_events
from .tools.reports import (
    report_html_response,
    report_scheduler_state,
    list_reports,
    subscribe_to_report,
)
from .schemas import (
    ReleaseCalendarResponse,
    ReportSearchResponse,
    ReportSubscriptionRequest,
    ReportSubscriptionResponse,
)


router = APIRouter(prefix="/api/v2", tags=["aegis-v2"])


def _split_values(values: Optional[List[str]]) -> list[str]:
    """Support repeated and comma-separated query params."""
    normalized: list[str] = []
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part and part not in normalized:
                normalized.append(part)
    return normalized


def _split_ints(values: Optional[List[str]]) -> list[int]:
    """Support repeated and comma-separated integer query params."""
    ints: list[int] = []
    for value in _split_values(values):
        try:
            parsed = int(value)
        except ValueError:
            continue
        if parsed not in ints:
            ints.append(parsed)
    return ints


@router.get("/availability", response_model=DataAvailabilityResponse)
async def availability_endpoint(
    sources: Optional[List[str]] = Query(default=None),
    banks: Optional[List[str]] = Query(default=None),
    bank_categories: Optional[List[str]] = Query(default=None),
    years: Optional[List[str]] = Query(default=None),
    quarters: Optional[List[str]] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    mock: bool = Query(default=True),
) -> DataAvailabilityResponse:
    """Return filtered source coverage for the V2 availability view."""
    filters = AvailabilityFilters(
        source_ids=_split_values(sources),
        bank_symbols=_split_values(banks),
        bank_categories=_split_values(bank_categories),
        fiscal_years=_split_ints(years),
        quarters=_split_values(quarters),
        keyword=keyword,
        limit=limit,
    )
    if mock:
        return mock_availability_response(filters)
    return await check_data_availability(filters)


@router.get("/documents", response_model=DocumentSearchResponse)
async def documents_endpoint(
    sources: Optional[List[str]] = Query(default=None),
    banks: Optional[List[str]] = Query(default=None),
    bank_categories: Optional[List[str]] = Query(default=None),
    years: Optional[List[str]] = Query(default=None),
    quarters: Optional[List[str]] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    mock: bool = Query(default=True),
) -> DocumentSearchResponse:
    """Return filtered source documents for the V2 file explorer."""
    filters = DocumentFilters(
        source_ids=_split_values(sources),
        bank_symbols=_split_values(banks),
        bank_categories=_split_values(bank_categories),
        fiscal_years=_split_ints(years),
        quarters=_split_values(quarters),
        keyword=keyword,
        limit=limit,
    )
    if mock:
        return mock_document_search_response(filters)
    return await list_documents(filters)


@router.get("/documents/search", response_model=DocumentSearchResponse)
async def document_search_endpoint(
    q: Optional[str] = Query(default=None),
    sources: Optional[List[str]] = Query(default=None),
    banks: Optional[List[str]] = Query(default=None),
    bank_categories: Optional[List[str]] = Query(default=None),
    years: Optional[List[str]] = Query(default=None),
    quarters: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    mock: bool = Query(default=True),
) -> DocumentSearchResponse:
    """Search source document metadata and indexed source content."""
    filters = DocumentFilters(
        source_ids=_split_values(sources),
        bank_symbols=_split_values(banks),
        bank_categories=_split_values(bank_categories),
        fiscal_years=_split_ints(years),
        quarters=_split_values(quarters),
        keyword=q,
        limit=limit,
    )
    if mock:
        return mock_document_search_response(filters)
    return await list_documents(filters)


@router.get("/release-calendar", response_model=ReleaseCalendarResponse)
async def release_calendar_endpoint(
    month: Optional[str] = Query(default=None),
    banks: Optional[List[str]] = Query(default=None),
    bank_categories: Optional[List[str]] = Query(default=None),
    event_types: Optional[List[str]] = Query(default=None),
    years: Optional[List[str]] = Query(default=None),
    quarters: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    mock: bool = Query(default=True),
) -> ReleaseCalendarResponse:
    """Return monthly release/calendar events for the V2 widget."""
    if mock:
        return mock_release_calendar_response(
            month=month,
            bank_symbols=_split_values(banks),
            bank_categories=_split_values(bank_categories),
            event_types=_split_values(event_types),
            fiscal_years=_split_ints(years),
            quarters=_split_values(quarters),
            limit=limit,
        )
    return await list_release_events(
        month=month,
        bank_symbols=_split_values(banks),
        bank_categories=_split_values(bank_categories),
        event_types=_split_values(event_types),
        fiscal_years=_split_ints(years),
        quarters=_split_values(quarters),
        limit=limit,
    )


@router.get("/reports", response_model=ReportSearchResponse)
async def reports_endpoint(
    banks: Optional[List[str]] = Query(default=None),
    bank_categories: Optional[List[str]] = Query(default=None),
    years: Optional[List[str]] = Query(default=None),
    quarters: Optional[List[str]] = Query(default=None),
    report_types: Optional[List[str]] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    mock: bool = Query(default=True),
) -> ReportSearchResponse:
    """Return generated reports for the downloader widget."""
    if mock:
        return mock_report_search_response(
            bank_symbols=_split_values(banks),
            bank_categories=_split_values(bank_categories),
            fiscal_years=_split_ints(years),
            quarters=_split_values(quarters),
            report_types=_split_values(report_types),
            limit=limit,
        )
    return await list_reports(
        bank_symbols=_split_values(banks),
        bank_categories=_split_values(bank_categories),
        fiscal_years=_split_ints(years),
        quarters=_split_values(quarters),
        report_types=_split_values(report_types),
        limit=limit,
    )


@router.get("/reports/{report_id}/preview")
async def report_preview_endpoint(report_id: int) -> Response:
    """Preview one generated report as HTML."""
    return await report_html_response(report_id, download=False)


@router.get("/reports/{report_id}/download")
async def report_download_endpoint(report_id: int) -> Response:
    """Download one generated report as HTML."""
    return await report_html_response(report_id, download=True)


@router.get("/report-subscriptions", response_model=ReportSubscriptionResponse)
async def report_subscriptions_endpoint() -> ReportSubscriptionResponse:
    """Return report scheduler options and current subscriptions."""
    return await report_scheduler_state()


@router.post("/report-subscriptions", response_model=ReportSubscriptionResponse)
async def create_report_subscription_endpoint(
    request: ReportSubscriptionRequest,
) -> ReportSubscriptionResponse:
    """Subscribe to a generated report scope."""
    return await subscribe_to_report(request)


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """Stream V2 chat, tool, widget, preview, and artifact events."""
    await websocket.accept()
    state = V2SessionState()
    await websocket.send_json(event(state.session_id, "session.ready", {"session_id": state.session_id}))
    try:
        while True:
            payload = await websocket.receive_json()
            async for item in run_turn(payload, state):
                await websocket.send_json(item)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # pylint: disable=broad-exception-caught
        await websocket.send_json(
            event(
                state.session_id,
                "chat.message",
                {"role": "assistant", "content": f"V2 websocket error: {exc}"},
            )
        )


def mount_v2_frontend(app: FastAPI, agent_root: Path) -> None:
    """Mount built V2 frontend assets while keeping V1 routes intact."""
    frontend_root = agent_root / "frontend"
    frontend_dist = frontend_root / "dist"
    assets_dir = frontend_dist / "assets"
    if assets_dir.exists():
        app.mount("/v2/assets", StaticFiles(directory=str(assets_dir)), name="v2-assets")

    @app.get("/v2")
    async def v2_index() -> Response:
        index_path = frontend_dist / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        source_index = frontend_root / "index.html"
        if source_index.exists():
            return HTMLResponse(
                """
                <!doctype html>
                <html lang="en">
                  <head><meta charset="utf-8"><title>Aegis V2</title></head>
                  <body style="font-family: Inter, Arial, sans-serif; margin: 32px;">
                    <h1>Aegis V2 frontend build missing</h1>
                    <p>Run <code>npm install</code> and <code>npm run build</code> inside
                    <code>aegis-agent/frontend</code>, or use <code>npm run dev</code> for the Vite app.</p>
                  </body>
                </html>
                """,
                status_code=503,
            )
        return HTMLResponse("<h1>Aegis V2 frontend not found</h1>", status_code=404)

    @app.get("/v2/{full_path:path}")
    async def v2_spa_fallback(full_path: str) -> Response:
        requested = frontend_dist / full_path
        if requested.is_file():
            return FileResponse(requested)
        index_path = frontend_dist / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        return await v2_index()
