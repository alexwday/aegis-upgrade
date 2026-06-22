"""Tests for the first V2 websocket orchestration loop."""

from __future__ import annotations

from datetime import datetime

import pytest

from aegis_agent.v2.agent.conversation import (
    ContextFinalResponse,
    ConversationContext,
)
from aegis_agent.v2.agent.planner import TurnPlan
from aegis_agent.v2.agent.retrieval import RetrievalResult
from aegis_agent.v2.orchestrator import (
    MAX_AGENT_STEPS,
    V2SessionState,
    event,
    run_turn,
)
from aegis_agent.v2.schemas import DataAvailabilityResponse, DataAvailabilityRow


@pytest.fixture(autouse=True)
def fake_llm_context(monkeypatch) -> None:
    """Keep orchestrator tests focused on routing, not live auth setup."""

    async def fake_build_llm_context(_execution_id, _purpose):
        return {
            "execution_id": "test-run",
            "auth_config": {"success": True, "method": "api_key", "token": "test"},
            "ssl_config": {"success": True, "verify": False},
        }

    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.build_llm_context", fake_build_llm_context
    )


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

    async def fake_plan_turn(_turn, _conversation_context, _llm_context=None):
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

    async def fake_plan_turn(_turn, _conversation_context, _llm_context=None):
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

    async def fail_plan_turn(*_args, **_kwargs):
        raise AssertionError("greeting should not need LLM planning")

    async def fail_stream_synthesis(*_args, **_kwargs):
        raise AssertionError("greeting should not need LLM synthesis")
        if False:
            yield ""

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fail_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.stream_synthesis", fail_stream_synthesis
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
    assert "Hi. I can check data availability" in events[-1]["payload"]["content"]


@pytest.mark.asyncio
async def test_capabilities_question_gets_static_chat_not_prompt_echo(monkeypatch) -> None:
    """The help path should be useful chat, not a leaked prompt/context response."""

    async def fail_plan_turn(*_args, **_kwargs):
        raise AssertionError("capabilities help should not need LLM planning")

    async def fail_stream_synthesis(*_args, **_kwargs):
        raise AssertionError("capabilities help should not need LLM synthesis")
        if False:
            yield ""

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fail_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.stream_synthesis", fail_stream_synthesis
    )

    state = V2SessionState(session_id="session_test")
    events = [event async for event in run_turn({"content": "what can you do"}, state)]

    assert [event["type"] for event in events] == [
        "tool.completed",
        "chat.delta",
        "chat.message",
    ]
    content = events[-1]["payload"]["content"]
    assert "I can check which sources are available" in content
    assert "Current user message" not in content
    assert "what can you do" not in content


@pytest.mark.asyncio
async def test_bank_data_request_routes_to_availability_not_quick_search(monkeypatch) -> None:
    """Asking what data exists for a bank is a coverage request, not research."""

    async def fake_optional_context(filters):
        assert filters.bank_symbols == ["RY-CA"]
        assert filters.fiscal_years == []
        assert filters.quarters == []
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
                    source_ids=["rts", "pillar3"],
                    last_refreshed_at=datetime(2026, 6, 1, 12, 0),
                )
            ],
            fiscal_years=[2026],
            quarters=["Q1"],
            bank_categories=["Canadian Banks"],
        )

    async def fail_plan_turn(*_args, **_kwargs):
        raise AssertionError("availability should not need LLM planning")

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("availability should not run quick search")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fail_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.optional_context", fake_optional_context
    )
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fail_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "what data do you have for rbc"}, state
        )
    ]

    assert [event["type"] for event in events] == [
        "tool.started",
        "widget.created",
        "tool.completed",
        "widget.completed",
        "chat.message",
    ]
    assert events[2]["payload"]["name"] == "check_data_availability"
    assert events[3]["payload"]["widget"]["kind"] == "data_availability"
    assert events[3]["payload"]["widget"]["data"]["rows"][0]["bank_symbol"] == "RY-CA"


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

    async def fake_plan_turn(_turn, _conversation_context, _llm_context=None):
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


@pytest.mark.asyncio
async def test_research_with_explicit_text_scope_runs_quick_search(monkeypatch) -> None:
    """Text-derived bank/period scope should avoid an unnecessary clarification turn."""

    async def fake_plan_turn(_turn, _conversation_context, _llm_context=None):
        return TurnPlan(action="research", rationale="source-backed request")

    async def fake_retrieve_quick_evidence(turn, **_kwargs):
        assert turn.bank_symbols == ["RY-CA"]
        assert turn.fiscal_years == [2026]
        assert turn.quarters == ["Q1"]
        return RetrievalResult(chunks=[], gaps=[])

    async def fake_stream_synthesis(_turn, **_kwargs):
        yield "No matching evidence."

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fake_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.stream_synthesis",
        fake_stream_synthesis,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "Compare RBC Q1 2026 CET1 trends", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    assert "widget.completed" not in [event["type"] for event in events]
    assert "artifact.created" in [event["type"] for event in events]


@pytest.mark.asyncio
async def test_complete_scope_overrides_planner_clarify(monkeypatch) -> None:
    """Planner clarification should not block a fully scoped research request."""

    async def fake_plan_turn(_turn, _conversation_context, _llm_context=None):
        return TurnPlan(
            action="clarify",
            rationale="mistakenly asks for scope",
            missing_scope=["bank", "fiscal_year", "quarter"],
        )

    async def fake_retrieve_quick_evidence(turn, **_kwargs):
        assert turn.bank_symbols == ["RY-CA"]
        assert turn.fiscal_years == [2026]
        assert turn.quarters == ["Q1"]
        return RetrievalResult(chunks=[], gaps=[])

    async def fake_stream_synthesis(_turn, **_kwargs):
        yield "No matching evidence."

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fake_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.stream_synthesis",
        fake_stream_synthesis,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "Compare RBC Q1 2026 CET1 trends", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    assert "widget.completed" not in [event["type"] for event in events]
    assert "quick_research" in [
        event["payload"].get("name")
        for event in events
        if isinstance(event.get("payload"), dict)
    ]


@pytest.mark.asyncio
async def test_research_question_clarification_is_not_overridden(monkeypatch) -> None:
    """A bank/period-only prompt should still clarify the research intent."""

    async def fake_plan_turn(_turn, _conversation_context, _llm_context=None):
        return TurnPlan(
            action="clarify",
            rationale="missing research intent",
            clarification_question="What should I research for RBC Q1 2026?",
            missing_scope=["research_question"],
        )

    async def fake_optional_context(_filters):
        return DataAvailabilityResponse(rows=[], fiscal_years=[], quarters=[])

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("retrieval should not run without research intent")

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
            {"content": "RBC Q1 2026", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    assert [event["type"] for event in events] == [
        "tool.completed",
        "widget.completed",
    ]
    assert events[0]["payload"]["decision"] == "needs_clarification"
    widget = events[1]["payload"]["widget"]
    assert widget["data"]["missing_scope"] == ["research_question"]
    assert "What should I research" in widget["html"]


@pytest.mark.asyncio
async def test_agent_loop_replans_when_step_requests_continue(monkeypatch) -> None:
    """The V2 loop should support non-terminal agent steps."""
    plan_calls = 0

    async def fake_plan_turn(_turn, _conversation_context, _llm_context=None):
        nonlocal plan_calls
        plan_calls += 1
        return TurnPlan(action="conversation", rationale="step")

    async def fake_run_planned_action(state, _turn, _plan, *, outcome, step_index):
        if step_index == 0:
            outcome.status = "continue"
            outcome.reason = "needs_next_step"
            if False:
                yield {}
            return
        outcome.status = "complete"
        outcome.reason = "done"
        yield event(
            state.session_id,
            "chat.message",
            {"role": "assistant", "content": "completed after replan"},
        )

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fake_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator._run_planned_action",
        fake_run_planned_action,
    )

    state = V2SessionState(session_id="session_test")
    events = [event async for event in run_turn({"content": "continue test"}, state)]

    assert plan_calls == 2
    assert events[-1]["type"] == "chat.message"
    assert events[-1]["payload"]["content"] == "completed after replan"


@pytest.mark.asyncio
async def test_agent_loop_limit_surfaces_failure(monkeypatch) -> None:
    """The bounded loop should fail explicitly instead of spinning forever."""
    plan_calls = 0

    async def fake_plan_turn(_turn, _conversation_context, _llm_context=None):
        nonlocal plan_calls
        plan_calls += 1
        return TurnPlan(action="conversation", rationale="never terminal")

    async def fake_run_planned_action(_state, _turn, _plan, *, outcome, step_index):
        outcome.status = "continue"
        outcome.reason = f"continue_{step_index}"
        if False:
            yield {}

    monkeypatch.setattr("aegis_agent.v2.orchestrator.plan_turn", fake_plan_turn)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator._run_planned_action",
        fake_run_planned_action,
    )

    state = V2SessionState(session_id="session_test")
    events = [event async for event in run_turn({"content": "loop limit"}, state)]

    assert plan_calls == MAX_AGENT_STEPS
    assert events[-2]["type"] == "tool.failed"
    assert events[-2]["payload"]["name"] == "agent_loop"
    assert events[-1]["type"] == "chat.message"
