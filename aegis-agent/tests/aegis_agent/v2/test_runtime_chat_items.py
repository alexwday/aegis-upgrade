"""Tests for V2 runtime chat item hydration."""

from __future__ import annotations

import pytest

from aegis_agent.v2.agent.conversation import WIDGET_MARKER_CLOSE, WIDGET_MARKER_OPEN
from aegis_agent.v2.schemas import ChatMessageRecord, HtmlWidget
from aegis_agent.v2.tools import runtime
from aegis_agent.v2.tools.runtime import chat_history_items_from_messages


def test_chat_history_items_parse_persisted_widgets() -> None:
    """Bootstrap history should expose persisted widgets as renderable items."""
    widget = HtmlWidget(
        id="widget-history",
        kind="data_availability",
        title="Data Availability",
        status="complete",
        html="<p>Loaded</p>",
    )
    items = chat_history_items_from_messages(
        [
            ChatMessageRecord(id="m1", role="user", content="what data exists?"),
            ChatMessageRecord(
                id="m2",
                role="tool",
                content=(
                    f"{WIDGET_MARKER_OPEN}"
                    f"{widget.model_dump_json()}"
                    f"{WIDGET_MARKER_CLOSE}"
                ),
            ),
            ChatMessageRecord(id="m3", role="assistant", content="Here is the data."),
        ]
    )

    assert [item.type for item in items] == ["message", "widget", "message"]
    assert items[1].widget.id == "widget-history"
    assert items[1].widget.html == "<p>Loaded</p>"


def test_chat_history_items_hide_non_widget_tool_messages() -> None:
    """Internal tool messages should not become visible chat rows."""
    items = chat_history_items_from_messages(
        [
            ChatMessageRecord(id="m1", role="user", content="hello"),
            ChatMessageRecord(id="m2", role="tool", content="internal detail"),
        ]
    )

    assert len(items) == 1
    assert items[0].type == "message"
    assert items[0].message.content == "hello"


@pytest.mark.asyncio
async def test_resolve_clarification_widget_replaces_hidden_widget(
    monkeypatch,
) -> None:
    """A selected clarification option should turn the widget row into a question."""
    widget = HtmlWidget(
        id="widget-clarify",
        kind="clarification",
        title="Clarification",
        status="complete",
        html="<p>Which bank should I use?</p>",
    )
    captured: dict[str, object] = {}

    async def fake_list_conversation_messages(_conversation_id):
        return [
            ChatMessageRecord(id="m1", role="user", content="compare CET1"),
            ChatMessageRecord(
                id="m2",
                role="tool",
                content=(
                    f"{WIDGET_MARKER_OPEN}"
                    f"{widget.model_dump_json()}"
                    f"{WIDGET_MARKER_CLOSE}"
                ),
            ),
        ]

    async def fake_fetch_one(_query, params=None, execution_id=None):
        _ = execution_id
        captured.update(params or {})
        return {
            "message_id": "m2",
            "role": "assistant",
            "content": params["content"],
        }

    monkeypatch.setattr(
        runtime, "list_conversation_messages", fake_list_conversation_messages
    )
    monkeypatch.setattr(runtime, "fetch_one", fake_fetch_one)

    message = await runtime.resolve_clarification_widget(
        conversation_id="conversation-1",
        user_id="user-1",
        widget_id="widget-clarify",
        question="Which bank should I use?",
    )

    assert message is not None
    assert message.id == "m2"
    assert message.role == "assistant"
    assert message.content == "Which bank should I use?"
    assert captured["message_id"] == "m2"
    assert captured["conversation_id"] == "conversation-1"
    assert captured["user_id"] == "user-1"


@pytest.mark.asyncio
async def test_reset_runtime_chat_deletes_user_conversations(monkeypatch) -> None:
    """Runtime reset should delete parent conversations and rely on DB cascades."""
    captured: dict[str, object] = {}

    async def fake_execute_query(query, params=None, execution_id=None):
        _ = execution_id
        captured["query"] = query
        captured.update(params or {})
        return 2

    monkeypatch.setattr(runtime, "execute_query", fake_execute_query)

    deleted = await runtime.reset_runtime_chat(user_id="user-1")

    assert deleted == 2
    assert "DELETE FROM public.chat_conversations" in str(captured["query"])
    assert captured["user_id"] == "user-1"
