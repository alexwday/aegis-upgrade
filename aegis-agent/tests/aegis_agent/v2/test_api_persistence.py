"""Tests for V2 websocket event persistence helpers."""

from __future__ import annotations

import json

import pytest

from aegis_agent.v2 import api
from aegis_agent.v2.schemas import ChatMessageRecord, HtmlWidget


@pytest.mark.asyncio
async def test_completed_widget_is_persisted_as_hidden_tool_message(
    monkeypatch,
) -> None:
    """Completed widgets should be reloadable from chat history."""
    captured: dict[str, str] = {}
    logged: dict[str, object] = {}

    async def fake_append_chat_message(**kwargs):
        captured.update(kwargs)
        return ChatMessageRecord(
            id="message-1", role=kwargs["role"], content=kwargs["content"]
        )

    async def fake_log_process_monitor_stage(**kwargs):
        logged.update(kwargs)
        return None

    monkeypatch.setattr(api, "append_chat_message", fake_append_chat_message)
    monkeypatch.setattr(
        api, "log_process_monitor_stage", fake_log_process_monitor_stage
    )

    widget = HtmlWidget(
        id="widget-test",
        kind="data_availability",
        title="Data Availability",
        status="complete",
        html="<p>Loaded</p>",
    )
    await api._persist_stream_event(
        {
            "type": "widget.completed",
            "event_id": "event-1",
            "payload": {"widget": widget.model_dump(mode="json")},
        },
        conversation_id="00000000-0000-0000-0000-000000000001",
        run_uuid="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000003",
    )

    assert captured["role"] == "tool"
    assert captured["content"].startswith(api.WIDGET_MARKER_OPEN)
    payload = (
        captured["content"]
        .removeprefix(api.WIDGET_MARKER_OPEN)
        .removesuffix(api.WIDGET_MARKER_CLOSE)
    )
    assert json.loads(payload)["id"] == "widget-test"
    assert logged["stage_name"] == "V2_Widget_Data_Availability_Completed"
    assert logged["status"] == "Success"
    metadata = logged["custom_metadata"]
    assert metadata["schema_version"] == "aegis.v2.process_monitor.v1"
    assert metadata["stage_category"] == "widget"
    assert metadata["payload_summary"]["widget"]["id"] == "widget-test"
