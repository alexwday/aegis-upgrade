"""Tests for the first V2 websocket orchestration loop."""

from __future__ import annotations

from datetime import datetime

import pytest

from aegis_agent.v2.agent.conversation import (
    ContextFinalResponse,
    ConversationContext,
)
from aegis_agent.v2.agent.planner import TurnPlan
from aegis_agent.v2.orchestrator import V2SessionState, run_turn
from aegis_agent.v2.schemas import DataAvailabilityResponse, DataAvailabilityRow


@pytest.mark.asyncio
async def test_availability_turn_streams_tool_widget_and_message(monkeypatch) -> None:
    """Availability requests should stream the planned V2 lifecycle events."""

    async def fake_check_data_availability(_filters):
        return DataAvailabilityResponse(
            rows=[
                DataAvailabilityRow(
                    bank_id=1,
                    bank_name="Royal Bank of Canada",
                    bank_symbol="RY-CA",
                    bank_category="Canadian Banks",
                    bank_category_id="Canadian_Banks",
                    fiscal_year=2026,
                    quarter="Q1",
                    source_ids=["investor_slides"],
                    last_refreshed_at=datetime(2026, 6, 1, 12, 0),
                )
            ],
            fiscal_years=[2026],
            quarters=["Q1"],
            bank_categories=["Canadian Banks"],
        )

    async def fake_plan_turn(_turn, _conversation_context):
        return TurnPlan(action="availability", rationale="availability request")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fake_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.optional_context",
        fake_check_data_availability,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event async for event in run_turn({"content": "what data is available?"}, state)
    ]

    assert [event["type"] for event in events] == [
        "tool.started",
        "widget.created",
        "tool.completed",
        "widget.completed",
        "chat.message",
    ]
    assert state.latest_availability is not None
    assert events[3]["payload"]["widget"]["data"]["rows"][0]["bank_symbol"] == "RY-CA"


@pytest.mark.asyncio
async def test_context_follow_up_routes_to_general_without_research(
    monkeypatch,
) -> None:
    """Follow-ups about prior output should not default into quick research."""
    context = ConversationContext(
        final_responses=[
            ContextFinalResponse(
                headline="Quick search found 12 evidence chunks",
                dek="CET1 trends",
                body_excerpt="RBC CET1 moved higher in the selected period.",
            )
        ]
    )

    async def fake_load_conversation_context(*_args, **_kwargs):
        return context

    async def fake_plan_turn(_turn, _conversation_context):
        return TurnPlan(action="conversation", rationale="context follow-up")

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("quick research should not run for context follow-up")

    async def fake_stream_synthesis(_turn, **kwargs):
        assert kwargs["conversation_context"] is context
        yield "Prior context answer"

    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.load_conversation_context",
        fake_load_conversation_context,
    )
    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fake_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fail_retrieve_quick_evidence,
    )
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.stream_synthesis",
        fake_stream_synthesis,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {
                "content": "summarize the previous answer",
                "conversation_id": "conversation-1",
            },
            state,
        )
    ]

    assert events[0]["type"] == "tool.completed"
    assert events[0]["payload"]["decision"] == "general_conversation"
    assert [event["type"] for event in events[1:]] == [
        "chat.delta",
        "chat.message",
    ]
    assert events[1]["payload"]["content"] == "Prior context answer"


@pytest.mark.asyncio
async def test_greeting_routes_to_plain_general_chat(monkeypatch) -> None:
    """Simple greetings should behave like chat, not research output."""

    async def fake_stream_synthesis(_turn, **_kwargs):
        yield "Hi. What would you like to work on?"

    async def fake_plan_turn(_turn, _conversation_context):
        return TurnPlan(action="conversation", rationale="greeting")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fake_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.stream_synthesis",
        fake_stream_synthesis,
    )

    state = V2SessionState(session_id="session_test")
    events = [event async for event in run_turn({"content": "hi"}, state)]

    assert [event["type"] for event in events] == [
        "tool.completed",
        "chat.delta",
        "chat.message",
    ]
    assert events[0]["payload"]["decision"] == "general_conversation"
    assert "aegis_final_shell" not in events[-1]["payload"]["content"]


@pytest.mark.asyncio
async def test_research_without_scope_emits_clarification_widget(monkeypatch) -> None:
    """Research intent without bank/period scope should ask before retrieval."""

    async def fake_optional_context(_filters):
        return DataAvailabilityResponse(
            rows=[
                DataAvailabilityRow(
                    bank_id=1,
                    bank_name="Royal Bank of Canada",
                    bank_symbol="RY-CA",
                    bank_category="Canadian Banks",
                    bank_category_id="Canadian_Banks",
                    fiscal_year=2026,
                    quarter="Q2",
                    source_ids=["rts", "pillar3"],
                    last_refreshed_at=datetime(2026, 6, 1, 12, 0),
                )
            ],
            fiscal_years=[2026],
            quarters=["Q2"],
            bank_categories=["Canadian Banks"],
        )

    async def fake_plan_turn(_turn, _conversation_context):
        return TurnPlan(action="research", rationale="source-backed request")

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("quick research should not run before clarification")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fake_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.optional_context",
        fake_optional_context,
    )
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fail_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "compare capital trends", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    assert [event["type"] for event in events] == [
        "tool.completed",
        "widget.completed",
    ]
    assert events[0]["payload"]["decision"] == "needs_clarification"
    widget = events[1]["payload"]["widget"]
    assert widget["kind"] == "clarification"
    assert widget["actions"][0]["action_type"] == "clarification_reply"
    assert widget["actions"][0]["payload"]["filters"]["bank_symbols"] == ["RY-CA"]
