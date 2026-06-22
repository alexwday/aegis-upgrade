"""First V2 agent orchestration loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Any, AsyncIterator
from uuid import uuid4

from .mock_data import mock_availability_response
from .schemas import (
    Artifact,
    AvailabilityFilters,
    ChatMessagePayload,
    DataAvailabilityResponse,
    HtmlWidget,
    V2Event,
    WidgetAction,
)
from .tools.availability import availability_widget_html, check_data_availability


@dataclass
class V2SessionState:
    """State scoped to one V2 websocket connection."""

    session_id: str = field(default_factory=lambda: f"session_{uuid4().hex}")
    widgets: dict[str, HtmlWidget] = field(default_factory=dict)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    latest_availability: DataAvailabilityResponse | None = None
    latest_availability_widget_id: str | None = None


def event(session_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-serializable V2 event envelope."""
    envelope = V2Event(type=event_type, session_id=session_id, payload=payload or {})
    return envelope.model_dump(mode="json")


def _message_payload(role: str, content: str) -> dict[str, Any]:
    """Return a validated chat message payload."""
    return ChatMessagePayload(role=role, content=content).model_dump(mode="json")


def _is_availability_request(message: str) -> bool:
    """Return whether a user message should run the availability tool."""
    normalized = message.lower()
    triggers = ("available", "availability", "coverage", "data do we have", "what data")
    return any(trigger in normalized for trigger in triggers)


def _is_report_request(message: str) -> bool:
    """Return whether a user message is asking to create an artifact/report."""
    normalized = message.lower()
    return "report" in normalized or "artifact" in normalized or "write up" in normalized


def _availability_filters_from_payload(payload: dict[str, Any]) -> AvailabilityFilters:
    """Build filters from a websocket payload."""
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    return AvailabilityFilters(
        source_ids=[str(value) for value in filters.get("source_ids", [])],
        bank_symbols=[str(value) for value in filters.get("bank_symbols", [])],
        bank_categories=[str(value) for value in filters.get("bank_categories", [])],
        fiscal_years=[int(value) for value in filters.get("fiscal_years", [])],
        quarters=[str(value) for value in filters.get("quarters", [])],
        keyword=str(filters.get("keyword")).strip() if filters.get("keyword") else None,
    )

def _availability_actions(response: DataAvailabilityResponse) -> list[WidgetAction]:
    """Return structured actions for coverage rows."""
    actions: list[WidgetAction] = []
    seen: set[tuple[str, int, str]] = set()
    for row in response.rows[:24]:
        key = (row.bank_symbol, row.fiscal_year, row.quarter)
        if key in seen:
            continue
        seen.add(key)
        actions.append(
            WidgetAction(
                id=f"open_documents_{row.bank_symbol}_{row.fiscal_year}_{row.quarter}",
                label=f"Open {row.bank_symbol} {row.quarter} {row.fiscal_year} documents",
                action_type="filter_documents",
                payload={
                    "bank_symbols": [row.bank_symbol],
                    "fiscal_years": [row.fiscal_year],
                    "quarters": [row.quarter],
                    "source_ids": row.source_ids,
                },
            )
        )
    return actions

async def _run_availability_turn(
    state: V2SessionState,
    payload: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    """Run the first V2 availability widget workflow."""
    tool_id = f"tool_{uuid4().hex}"
    widget = HtmlWidget(
        kind="data_availability",
        title="Data Availability",
        status="running",
        html="<p>Checking source coverage...</p>",
    )
    state.widgets[widget.id] = widget
    state.latest_availability_widget_id = widget.id

    yield event(
        state.session_id,
        "tool.started",
        {"tool_id": tool_id, "name": "check_data_availability", "widget_id": widget.id},
    )
    yield event(state.session_id, "widget.created", {"widget": widget.model_dump(mode="json")})

    filters = _availability_filters_from_payload(payload)
    try:
        if payload.get("mock", False):
            await asyncio.sleep(2.0)
            response = mock_availability_response(filters)
        else:
            response = await check_data_availability(filters)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        now = datetime.now(timezone.utc)
        widget.status = "failed"
        widget.html = f"<p>Availability check failed: {escape(str(exc))}</p>"
        widget.updated_at = now
        yield event(
            state.session_id,
            "tool.failed",
            {"tool_id": tool_id, "name": "check_data_availability", "error": str(exc)},
        )
        yield event(state.session_id, "widget.failed", {"widget": widget.model_dump(mode="json")})
        yield event(
            state.session_id,
            "chat.message",
            _message_payload("assistant", f"I could not check availability: {exc}"),
        )
        return

    now = datetime.now(timezone.utc)
    widget.status = "complete"
    widget.html = availability_widget_html(response)
    widget.data = response.model_dump(mode="json")
    widget.actions = _availability_actions(response)
    widget.updated_at = now
    state.widgets[widget.id] = widget
    state.latest_availability = response

    yield event(
        state.session_id,
        "tool.completed",
        {
            "tool_id": tool_id,
            "name": "check_data_availability",
            "row_count": len(response.rows),
            "missing_count": len(response.missing),
        },
    )
    yield event(state.session_id, "widget.completed", {"widget": widget.model_dump(mode="json")})
    yield event(
        state.session_id,
        "chat.message",
        _message_payload(
            "assistant",
            f"I found {len(response.rows)} available bank-period rows. "
            "Use the availability widget in the chat to inspect coverage.",
        ),
    )


def _artifact_from_latest_availability(state: V2SessionState) -> Artifact:
    """Create a simple self-contained HTML artifact from the latest availability widget."""
    response = state.latest_availability
    if response is None:
        html = (
            "<!doctype html><html><head><meta charset='utf-8'><title>Aegis report</title></head>"
            "<body><h1>Aegis report</h1><p>No completed research widgets are available yet.</p></body></html>"
        )
        source_widget_ids: list[str] = []
    else:
        rows = "".join(
            "<tr>"
            f"<td>{escape(row.bank_symbol)}</td>"
            f"<td>{escape(row.bank_category)}</td>"
            f"<td>{escape(row.quarter)} {row.fiscal_year}</td>"
            f"<td>{escape(', '.join(row.source_ids))}</td>"
            f"<td>{escape(row.last_refreshed_at.isoformat() if row.last_refreshed_at else 'n/a')}</td>"
            "</tr>"
            for row in response.rows
        )
        html = (
            "<!doctype html><html><head><meta charset='utf-8'><title>Data availability report</title>"
            "<style>body{font-family:Inter,Arial,sans-serif;margin:32px;color:#17202a}"
            "table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #d8e0ea;"
            "padding:8px;text-align:left}th{background:#f3f7fb}</style></head><body>"
            "<h1>Data availability report</h1>"
            f"<p>Generated from {len(response.rows)} bank-period rows.</p>"
            "<table><thead><tr><th>Bank</th><th>Category</th><th>Period</th>"
            "<th>Sources</th><th>Refreshed</th></tr></thead><tbody>"
            f"{rows}</tbody></table></body></html>"
        )
        source_widget_ids = [state.latest_availability_widget_id] if state.latest_availability_widget_id else []

    return Artifact(
        session_id=state.session_id,
        kind="availability_report",
        title="Data availability report",
        html=html,
        source_widget_ids=[widget_id for widget_id in source_widget_ids if widget_id],
    )


async def _run_artifact_turn(state: V2SessionState) -> AsyncIterator[dict[str, Any]]:
    """Create a session artifact from completed widget state."""
    artifact = _artifact_from_latest_availability(state)
    state.artifacts[artifact.id] = artifact
    yield event(state.session_id, "artifact.created", {"artifact": artifact.model_dump(mode="json")})
    yield event(
        state.session_id,
        "chat.message",
        _message_payload("assistant", f"I created an HTML artifact: {artifact.title}."),
    )


async def run_turn(payload: dict[str, Any], state: V2SessionState) -> AsyncIterator[dict[str, Any]]:
    """Run one V2 chat turn and stream typed UI events."""
    content = str(payload.get("content") or "").strip()
    if not content:
        yield event(state.session_id, "chat.message", _message_payload("assistant", "Send a question to start."))
        return

    if _is_availability_request(content):
        async for item in _run_availability_turn(state, payload):
            yield item
        return

    if _is_report_request(content):
        async for item in _run_artifact_turn(state):
            yield item
        return

    yield event(
        state.session_id,
        "chat.message",
        _message_payload(
            "assistant",
            "V2 is wired for data availability, preview, and HTML artifacts. "
            "Ask what data is available, or create a report after running an availability check.",
        ),
    )
