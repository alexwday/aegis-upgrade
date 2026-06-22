"""Tests for Aegis V2 wire contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aegis_agent.v2.schemas import HtmlWidget, V2Event


def test_v2_event_rejects_unknown_event_type() -> None:
    """The event envelope should reject accidental protocol drift."""
    with pytest.raises(ValidationError):
        V2Event(type="unknown.event", session_id="session_1", payload={})


def test_v2_event_rejects_extra_fields() -> None:
    """The event envelope should be explicit about public fields."""
    with pytest.raises(ValidationError):
        V2Event(
            type="session.ready",
            session_id="session_1",
            payload={},
            accidental=True,
        )


def test_html_widget_preserves_required_fields() -> None:
    """Widgets should carry the chat-rendered HTML and structured data."""
    widget = HtmlWidget(
        kind="data_availability",
        title="Data availability",
        status="complete",
        html="<p>Ready</p>",
        data={"rows": []},
    )

    assert widget.id.startswith("widget_")
    assert widget.status == "complete"
    assert widget.data["rows"] == []
