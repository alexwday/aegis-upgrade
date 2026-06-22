"""FastAPI routes for the Aegis V2 workstation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

from fastapi import (
    APIRouter,
    FastAPI,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .mock_data import (
    mock_availability_response,
    mock_document_search_response,
    mock_release_calendar_response,
    mock_report_search_response,
)
from .agent.conversation import WIDGET_MARKER_CLOSE, WIDGET_MARKER_OPEN
from .orchestrator import V2SessionState, event, run_turn
from .process_monitor import (
    STATUS_FAILURE,
    STATUS_SUCCESS,
    process_stage_from_event,
    turn_process_stage,
)
from .schemas import (
    AvailabilityFilters,
    Artifact,
    ArtifactListResponse,
    BootstrapResponse,
    ConversationDetailResponse,
    ConversationListResponse,
    DataAvailabilityResponse,
    DataSourceRegistryResponse,
    DocumentFilters,
    DocumentSearchResponse,
    HtmlWidget,
)
from .tools.catalog import list_data_sources, optional_context
from .tools.availability import check_data_availability
from .tools.documents import list_documents
from .tools.release_calendar import list_release_events
from .tools.runtime import (
    DEFAULT_USER_ID,
    append_chat_message,
    bootstrap_runtime,
    ensure_conversation,
    get_artifact,
    get_conversation_detail,
    list_conversations,
    log_process_monitor_stage,
    persist_artifact,
)
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


def _widget_message_content(widget: HtmlWidget) -> str:
    """Serialize a widget into a hidden chat message for conversation reloads."""
    return (
        f"{WIDGET_MARKER_OPEN}"
        f"{json.dumps(widget.model_dump(mode='json'), default=str)}"
        f"{WIDGET_MARKER_CLOSE}"
    )


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


def _conversation_title(content: str) -> str:
    """Derive a compact default title from the first user message."""
    title = " ".join(content.strip().split())
    if not title:
        return "New conversation"
    return title[:77] + "..." if len(title) > 80 else title


async def _persist_stream_event(
    item: dict,
    *,
    conversation_id: str,
    run_uuid: str,
    user_id: str,
) -> dict:
    """Persist side effects from one streamed event before sending it to the UI."""
    event_type = str(item.get("type") or "")
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    payload["conversation_id"] = conversation_id
    payload["run_uuid"] = run_uuid
    item["payload"] = payload

    if event_type == "chat.delta":
        return item

    if event_type == "chat.message":
        role = str(payload.get("role") or "assistant")
        content = str(payload.get("content") or "")
        if role != "user" and content:
            message = await append_chat_message(
                conversation_id=conversation_id,
                run_uuid=run_uuid,
                role=role,
                content=content,
            )
            payload["message_id"] = message.id
            item["payload"] = payload

    if event_type in {"widget.completed", "widget.failed"} and isinstance(
        payload.get("widget"), dict
    ):
        widget = HtmlWidget(**payload["widget"])
        await append_chat_message(
            conversation_id=conversation_id,
            run_uuid=run_uuid,
            role="tool",
            content=_widget_message_content(widget),
        )

    if event_type in {"artifact.created", "artifact.updated"} and isinstance(
        payload.get("artifact"), dict
    ):
        artifact = Artifact(**payload["artifact"])
        persisted = await persist_artifact(
            conversation_id=conversation_id,
            run_uuid=run_uuid,
            artifact=artifact,
        )
        payload["artifact"] = persisted.model_dump(mode="json")
        item["payload"] = payload

    stage = process_stage_from_event(
        item,
        conversation_id=conversation_id,
        run_uuid=run_uuid,
    )
    await log_process_monitor_stage(
        run_uuid=run_uuid,
        user_id=user_id,
        stage_name=stage.stage_name,
        status=stage.status,
        decision_details=stage.decision_details,
        error_message=stage.error_message,
        custom_metadata=stage.custom_metadata,
    )
    return item


@router.get("/availability", response_model=DataAvailabilityResponse)
async def availability_endpoint(
    sources: Optional[List[str]] = Query(default=None),
    banks: Optional[List[str]] = Query(default=None),
    bank_categories: Optional[List[str]] = Query(default=None),
    years: Optional[List[str]] = Query(default=None),
    quarters: Optional[List[str]] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    mock: bool = Query(default=False),
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


@router.get("/data-sources", response_model=DataSourceRegistryResponse)
async def data_sources_endpoint() -> DataSourceRegistryResponse:
    """Return V2 data source registry rows for the filters UI."""
    return await list_data_sources()


@router.get("/optional-context", response_model=DataAvailabilityResponse)
async def optional_context_endpoint(
    sources: Optional[List[str]] = Query(default=None),
    banks: Optional[List[str]] = Query(default=None),
    bank_categories: Optional[List[str]] = Query(default=None),
    years: Optional[List[str]] = Query(default=None),
    quarters: Optional[List[str]] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=5000),
) -> DataAvailabilityResponse:
    """Return catalog-backed bank, period, and source availability options."""
    filters = AvailabilityFilters(
        source_ids=_split_values(sources),
        bank_symbols=_split_values(banks),
        bank_categories=_split_values(bank_categories),
        fiscal_years=_split_ints(years),
        quarters=_split_values(quarters),
        keyword=keyword,
        limit=limit,
    )
    return await optional_context(filters)


@router.get("/bootstrap", response_model=BootstrapResponse)
async def bootstrap_endpoint(
    user_id: str = Query(default=DEFAULT_USER_ID),
    conversation_id: Optional[str] = Query(default=None),
) -> BootstrapResponse:
    """Return the persisted V2 runtime state needed for initial UI render."""
    return await bootstrap_runtime(user_id=user_id, conversation_id=conversation_id)


@router.get("/conversations", response_model=ConversationListResponse)
async def conversations_endpoint(
    user_id: str = Query(default=DEFAULT_USER_ID),
    limit: int = Query(default=25, ge=1, le=100),
) -> ConversationListResponse:
    """Return persisted chat conversations for the current user."""
    return await list_conversations(user_id=user_id, limit=limit)


@router.get(
    "/conversations/{conversation_id}", response_model=ConversationDetailResponse
)
async def conversation_detail_endpoint(
    conversation_id: str,
    user_id: str = Query(default=DEFAULT_USER_ID),
) -> ConversationDetailResponse:
    """Return one persisted conversation with messages and artifacts."""
    detail = await get_conversation_detail(
        conversation_id=conversation_id, user_id=user_id
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return detail


@router.get(
    "/conversations/{conversation_id}/artifacts", response_model=ArtifactListResponse
)
async def conversation_artifacts_endpoint(
    conversation_id: str,
    user_id: str = Query(default=DEFAULT_USER_ID),
) -> ArtifactListResponse:
    """Return persisted artifacts for one conversation."""
    detail = await get_conversation_detail(
        conversation_id=conversation_id, user_id=user_id
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return ArtifactListResponse(artifacts=detail.artifacts)


@router.get("/artifacts/{artifact_id}", response_model=Artifact)
async def artifact_detail_endpoint(
    artifact_id: str,
    user_id: str = Query(default=DEFAULT_USER_ID),
) -> Artifact:
    """Return one persisted artifact for preview."""
    artifact = await get_artifact(artifact_id=artifact_id, user_id=user_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


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
    await websocket.send_json(
        event(state.session_id, "session.ready", {"session_id": state.session_id})
    )
    try:
        while True:
            payload = await websocket.receive_json()
            content = str(payload.get("content") or "").strip()
            user_id = str(payload.get("user_id") or DEFAULT_USER_ID)
            conversation = await ensure_conversation(
                user_id=user_id,
                conversation_id=payload.get("conversation_id"),
                title=_conversation_title(content),
            )
            run_uuid = str(uuid4())
            state.user_id = user_id
            state.conversation_id = conversation.conversation_id
            state.run_uuid = run_uuid

            if content:
                await append_chat_message(
                    conversation_id=conversation.conversation_id,
                    run_uuid=run_uuid,
                    role="user",
                    content=content,
                )
            accepted_stage = turn_process_stage(
                action="user_message_received",
                status=STATUS_SUCCESS,
                conversation_id=conversation.conversation_id,
                run_uuid=run_uuid,
                details="V2 websocket message accepted.",
                payload={
                    "has_filters": bool(payload.get("filters")),
                    "has_context": bool(
                        payload.get("context") or payload.get("optional_context")
                    ),
                    "model_selection": payload.get("model_selection")
                    or payload.get("model_mode"),
                    "search_selection": payload.get("search_selection")
                    or payload.get("search_mode"),
                },
            )
            await log_process_monitor_stage(
                run_uuid=run_uuid,
                user_id=user_id,
                stage_name=accepted_stage.stage_name,
                status=accepted_stage.status,
                decision_details=accepted_stage.decision_details,
                error_message=accepted_stage.error_message,
                custom_metadata=accepted_stage.custom_metadata,
            )

            turn_payload = {
                **payload,
                "user_id": user_id,
                "conversation_id": conversation.conversation_id,
                "run_uuid": run_uuid,
            }
            async for item in run_turn(turn_payload, state):
                persisted_item = await _persist_stream_event(
                    item,
                    conversation_id=conversation.conversation_id,
                    run_uuid=run_uuid,
                    user_id=user_id,
                )
                await websocket.send_json(persisted_item)
            completed_stage = turn_process_stage(
                action="completed",
                status=STATUS_SUCCESS,
                conversation_id=conversation.conversation_id,
                run_uuid=run_uuid,
                details="V2 websocket turn completed.",
            )
            await log_process_monitor_stage(
                run_uuid=run_uuid,
                user_id=user_id,
                stage_name=completed_stage.stage_name,
                status=completed_stage.status,
                decision_details=completed_stage.decision_details,
                error_message=completed_stage.error_message,
                custom_metadata=completed_stage.custom_metadata,
            )
    except WebSocketDisconnect:
        return
    except Exception as exc:  # pylint: disable=broad-exception-caught
        if state.run_uuid:
            failed_stage = turn_process_stage(
                action="failed",
                status=STATUS_FAILURE,
                conversation_id=state.conversation_id,
                run_uuid=state.run_uuid,
                details="V2 websocket turn failed.",
                error_message=str(exc),
            )
            await log_process_monitor_stage(
                run_uuid=state.run_uuid,
                user_id=state.user_id,
                stage_name=failed_stage.stage_name,
                status=failed_stage.status,
                decision_details=failed_stage.decision_details,
                error_message=failed_stage.error_message,
                custom_metadata=failed_stage.custom_metadata,
            )
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
        app.mount(
            "/v2/assets", StaticFiles(directory=str(assets_dir)), name="v2-assets"
        )

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
