"""Tests for V2 runtime chat item hydration."""

from __future__ import annotations

from aegis_agent.v2.agent.conversation import WIDGET_MARKER_CLOSE, WIDGET_MARKER_OPEN
from aegis_agent.v2.schemas import ChatMessageRecord, HtmlWidget
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
