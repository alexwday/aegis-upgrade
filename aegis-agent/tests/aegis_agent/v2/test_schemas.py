"""Tests for Aegis V2 wire contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aegis_agent.v2.schemas import HtmlWidget, V2Event, V2QueryRequest


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


def test_v2_query_request_normalizes_canonical_contract() -> None:
    """Canonical websocket requests should use the documented V2 field names."""
    request = V2QueryRequest(
        user_id="00000000-0000-0000-0000-000000000001",
        conversation_id="230029be-7349-469c-adba-8b432f4388d4",
        query="Compare CET1 for RBC and TD",
        filters={"data_sources": ["rts", "pillar3"]},
        optional_context={
            "bank_tickers": ["RY", "TD"],
            "fiscal_years": [2026],
            "quarters": ["1"],
        },
        model_selection="large",
        search_selection="deep",
    )

    assert request.filters.data_sources == ["rts", "pillar3"]
    assert request.optional_context.quarters == ["Q1"]
    assert request.model_selection == "large"
    assert request.search_selection == "deep"


def test_v2_query_request_rejects_contract_drift() -> None:
    """The canonical request schema should reject unsupported public values."""
    with pytest.raises(ValidationError):
        V2QueryRequest(
            user_id="00000000-0000-0000-0000-000000000001",
            query="Run research",
            model_selection="medium",
        )

    with pytest.raises(ValidationError):
        V2QueryRequest(
            user_id="00000000-0000-0000-0000-000000000001",
            query="Run research",
            filters={"source_ids": ["rts"]},
        )
