"""Stable V2 process monitor taxonomy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


PROCESS_MONITOR_SCHEMA_VERSION = "aegis.v2.process_monitor.v1"
STATUS_RUNNING = "Running"
STATUS_SUCCESS = "Success"
STATUS_FAILURE = "Failure"
SAFE_PAYLOAD_KEYS = {
    "artifact_id",
    "chunk_count",
    "chunk_limit",
    "conversation_id",
    "decision",
    "error",
    "final",
    "gap_count",
    "has_context",
    "has_filters",
    "message",
    "message_id",
    "missing_count",
    "model_selection",
    "model_plan",
    "name",
    "role",
    "row_count",
    "run_uuid",
    "search_selection",
    "sources",
    "status",
    "stream_id",
    "tool_id",
    "widget_id",
}


@dataclass(frozen=True)
class ProcessMonitorStage:
    """One normalized process monitor row payload."""

    stage_name: str
    status: str
    decision_details: str | None
    error_message: str | None
    custom_metadata: dict[str, Any]


def process_stage_from_event(
    item: dict[str, Any],
    *,
    conversation_id: str,
    run_uuid: str,
) -> ProcessMonitorStage:
    """Map one streamed UI event to a stable process monitor stage."""
    event_type = str(item.get("type") or "event")
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    category, subject, action = _event_parts(event_type, payload)
    status = _event_status(event_type)
    error_message = (
        str(payload.get("error") or "") if status == STATUS_FAILURE else None
    )
    decision_details = _decision_details(event_type, payload, category, subject, action)
    stage_name = _stage_name(category, subject, action)
    return ProcessMonitorStage(
        stage_name=stage_name,
        status=status,
        decision_details=decision_details,
        error_message=error_message,
        custom_metadata=_metadata(
            item,
            payload=payload,
            conversation_id=conversation_id,
            run_uuid=run_uuid,
            category=category,
            subject=subject,
            action=action,
        ),
    )


def turn_process_stage(
    *,
    action: str,
    status: str,
    conversation_id: str | None,
    run_uuid: str | None = None,
    details: str | None = None,
    error_message: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ProcessMonitorStage:
    """Build a normalized process monitor row for WebSocket turn lifecycle events."""
    normalized_action = _name_token(action)
    return ProcessMonitorStage(
        stage_name=_stage_name("turn", "websocket", normalized_action),
        status=status,
        decision_details=details,
        error_message=error_message,
        custom_metadata={
            "schema_version": PROCESS_MONITOR_SCHEMA_VERSION,
            "stage_category": "turn",
            "stage_subject": "websocket",
            "stage_action": normalized_action,
            "conversation_id": conversation_id,
            "run_uuid": run_uuid,
            "payload_summary": _payload_summary(payload or {}),
        },
    )


def _event_parts(event_type: str, payload: dict[str, Any]) -> tuple[str, str, str]:
    """Return category, subject, and action tokens for an event."""
    if event_type.startswith("tool."):
        return "tool", _name_token(payload.get("name") or "tool"), _suffix(event_type)
    if event_type.startswith("widget."):
        widget = (
            payload.get("widget") if isinstance(payload.get("widget"), dict) else {}
        )
        return (
            "widget",
            _name_token(widget.get("kind") or payload.get("name") or "widget"),
            _suffix(event_type),
        )
    if event_type.startswith("artifact."):
        artifact = (
            payload.get("artifact") if isinstance(payload.get("artifact"), dict) else {}
        )
        return (
            "artifact",
            _name_token(artifact.get("kind") or "artifact"),
            _suffix(event_type),
        )
    if event_type == "final_response.started":
        return "final_response", "assistant", "started"
    if event_type == "chat.message":
        return (
            "chat",
            _name_token(payload.get("role") or "message"),
            "message_persisted",
        )
    if event_type == "preview.open":
        return "preview", _name_token(payload.get("kind") or "target"), "opened"
    return "event", _name_token(event_type.replace(".", "_")), "observed"


def _event_status(event_type: str) -> str:
    """Return a stable monitor status for a UI event."""
    if event_type.endswith(".failed"):
        return STATUS_FAILURE
    if event_type.endswith(".started") or event_type.endswith(".progress"):
        return STATUS_RUNNING
    return STATUS_SUCCESS


def _decision_details(
    event_type: str,
    payload: dict[str, Any],
    category: str,
    subject: str,
    action: str,
) -> str:
    """Return a concise human-readable process detail."""
    message = str(payload.get("message") or "").strip()
    if message:
        return message[:500]
    if event_type == "final_response.started":
        return "Final response shell emitted."
    if category == "tool":
        return f"Tool {subject} {action.replace('_', ' ')}."
    if category == "widget":
        return f"Widget {subject} {action.replace('_', ' ')}."
    if category == "artifact":
        return f"Artifact {subject} {action.replace('_', ' ')}."
    if category == "chat":
        role = str(payload.get("role") or "message")
        return f"Persisted {role} chat message."
    return f"Observed {event_type}."


def _metadata(
    item: dict[str, Any],
    *,
    payload: dict[str, Any],
    conversation_id: str,
    run_uuid: str,
    category: str,
    subject: str,
    action: str,
) -> dict[str, Any]:
    """Build safe structured process metadata."""
    return {
        "schema_version": PROCESS_MONITOR_SCHEMA_VERSION,
        "event_type": str(item.get("type") or ""),
        "event_id": item.get("event_id"),
        "conversation_id": conversation_id,
        "run_uuid": run_uuid,
        "stage_category": category,
        "stage_subject": subject,
        "stage_action": action,
        "payload_summary": _payload_summary(payload),
    }


def _payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Return monitor-safe payload metadata without large content/html bodies."""
    summary = {
        key: _safe_value(value)
        for key, value in payload.items()
        if key in SAFE_PAYLOAD_KEYS
    }
    widget = payload.get("widget") if isinstance(payload.get("widget"), dict) else None
    if widget:
        summary["widget"] = {
            "id": widget.get("id"),
            "kind": widget.get("kind"),
            "title": widget.get("title"),
            "status": widget.get("status"),
        }
    artifact = (
        payload.get("artifact") if isinstance(payload.get("artifact"), dict) else None
    )
    if artifact:
        summary["artifact"] = {
            "id": artifact.get("id"),
            "kind": artifact.get("kind"),
            "title": artifact.get("title"),
            "evidence_count": len(artifact.get("evidence_ids") or []),
        }
    if payload.get("content") is not None:
        summary["content_length"] = len(str(payload.get("content") or ""))
    return summary


def _safe_value(value: Any) -> Any:
    """Return compact JSON-safe metadata values."""
    if isinstance(value, dict):
        return {
            str(key): _safe_value(nested)
            for key, nested in value.items()
            if key not in {"html", "content"}
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:20]]
    if isinstance(value, str):
        return value[:500]
    return value


def _stage_name(category: str, subject: str, action: str) -> str:
    """Return a V1-schema-compatible stage name under 100 chars."""
    raw = f"V2_{_title_token(category)}_{_title_token(subject)}_{_title_token(action)}"
    return raw[:100]


def _name_token(value: Any) -> str:
    """Normalize a value into a snake-ish stage token."""
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_") or "unknown"


def _title_token(value: str) -> str:
    """Convert a normalized token into stage-name title case."""
    return "_".join(part.capitalize() for part in _name_token(value).split("_"))


def _suffix(event_type: str) -> str:
    """Return the final event suffix as an action token."""
    return _name_token(event_type.rsplit(".", 1)[-1])
