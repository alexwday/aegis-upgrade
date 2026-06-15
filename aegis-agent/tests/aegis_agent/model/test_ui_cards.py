"""Tests for structured choice card events."""

from __future__ import annotations

from aegis_agent.model.agents.ui_cards import build_choice_card_event


def test_build_choice_card_event() -> None:
    """Choice card events should be websocket-ready structured payloads."""
    event = build_choice_card_event(
        question="Which quarter?",
        options=[
            {"id": "q1", "label": "Q1 2026"},
            {"id": "q4", "label": "Q4 2025"},
        ],
        card_id="card-1",
    )

    assert event["type"] == "ui_card"
    assert event["content"]["card_id"] == "card-1"
    assert event["content"]["options"][0]["label"] == "Q1 2026"
