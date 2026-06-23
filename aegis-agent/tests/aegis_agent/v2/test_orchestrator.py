"""Tests for the first V2 websocket orchestration loop."""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from aegis_agent.v2.agent.conversation import (
    ContextFinalResponse,
    ConversationContext,
)
from aegis_agent.v2.agent.models import normalize_turn
from aegis_agent.v2.agent.retrieval import RetrievalResult
from aegis_agent.v2.agent.tool_agent import AgentDecision, AgentToolCall
from aegis_agent.v2.orchestrator import (
    MAX_AGENT_STEPS,
    V2SessionState,
    _quick_evidence_feedback,
    _run_quick_research_turn,
    _tool_result_message,
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

    step_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return AgentDecision(
                kind="tool",
                tool_call=AgentToolCall(name="check_data_availability", arguments={}),
            )
        # Availability is non-terminal: the agent reads the coverage back and
        # authors the reply (F7).
        assert scratchpad and scratchpad[-1]["role"] == "tool"
        assert "check_data_availability" in scratchpad[-1]["content"]
        return AgentDecision(
            kind="direct", content="Coverage: RY-CA Q1 2026 (investor_slides)."
        )

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.optional_context",
        fake_check_data_availability,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event async for event in run_turn({"content": "what data is available?"}, state)
    ]

    types = [event["type"] for event in events]
    # Tool/widget lifecycle still emitted, in order, with no templated message.
    assert types[:4] == [
        "tool.started",
        "widget.created",
        "tool.completed",
        "widget.completed",
    ]
    assert events[2]["payload"]["name"] == "check_data_availability"
    assert state.latest_availability is not None
    assert events[3]["payload"]["widget"]["data"]["rows"][0]["bank_symbol"] == "RY-CA"
    # The agent authored the coverage reply; the turn closes with it.
    assert types[-1] == "chat.message"
    assert events[-1]["payload"].get("final") is True
    assert events[-1]["payload"]["content"] == "Coverage: RY-CA Q1 2026 (investor_slides)."


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

    async def fake_run_agent_step(_turn, conversation_context, _llm_context=None, scratchpad=None, on_delta=None):
        assert conversation_context is context
        return AgentDecision(kind="direct", content="Prior context answer")

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("quick research should not run for context follow-up")

    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.load_conversation_context",
        fake_load_conversation_context,
    )
    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fail_retrieve_quick_evidence,
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
    assert events[0]["payload"]["decision"] == "direct_response"
    assert [event["type"] for event in events[1:]] == [
        "chat.delta",
        "chat.message",
    ]
    assert events[1]["payload"]["content"] == "Prior context answer"


@pytest.mark.asyncio
async def test_greeting_routes_through_agent(monkeypatch) -> None:
    """Greetings go through the agent loop, not a hardcoded shortcut."""

    seen: dict[str, str] = {}

    async def fake_run_agent_step(
        turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        seen["user_message"] = turn.content
        if on_delta is not None:
            on_delta("Hey! ")
            on_delta("What can I dig into for you?")
        return AgentDecision(kind="direct", content="Hey! What can I dig into for you?")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)

    state = V2SessionState(session_id="session_test")
    events = [event async for event in run_turn({"content": "hi"}, state)]

    # The agent owns the turn: it saw the greeting and authored the reply.
    assert seen["user_message"] == "hi"
    assert [event["type"] for event in events] == [
        "tool.completed",
        "chat.delta",
        "chat.delta",
        "chat.message",
    ]
    assert events[0]["payload"]["decision"] == "direct_response"
    assert [events[1]["payload"]["content"], events[2]["payload"]["content"]] == [
        "Hey! ",
        "What can I dig into for you?",
    ]
    assert "aegis_final_shell" not in events[-1]["payload"]["content"]
    assert events[-1]["payload"]["content"] == "Hey! What can I dig into for you?"


@pytest.mark.asyncio
async def test_capabilities_question_routes_through_agent(monkeypatch) -> None:
    """Capability/help questions also go through the agent, with no canned text."""

    seen: dict[str, str] = {}

    async def fake_run_agent_step(
        turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        seen["user_message"] = turn.content
        if on_delta is not None:
            on_delta("I can check coverage ")
            on_delta("and run scoped research on bank disclosures.")
        return AgentDecision(
            kind="direct",
            content="I can check coverage and run scoped research on bank disclosures.",
        )

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)

    state = V2SessionState(session_id="session_test")
    events = [event async for event in run_turn({"content": "what can you do"}, state)]

    assert seen["user_message"] == "what can you do"
    assert [event["type"] for event in events] == [
        "tool.completed",
        "chat.delta",
        "chat.delta",
        "chat.message",
    ]
    assert events[0]["payload"]["decision"] == "direct_response"
    content = events[-1]["payload"]["content"]
    assert content == "I can check coverage and run scoped research on bank disclosures."


@pytest.mark.asyncio
async def test_direct_answer_delta_yields_before_agent_step_finishes(monkeypatch) -> None:
    """Direct answer tokens are visible while the agent step is still running."""

    proceed = asyncio.Event()

    async def fake_run_agent_step(
        turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        assert turn.content == "hi"
        assert on_delta is not None
        on_delta("Live ")
        await proceed.wait()
        on_delta("answer.")
        return AgentDecision(kind="direct", content="Live answer.")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)

    state = V2SessionState(session_id="session_test")
    stream = run_turn({"content": "hi"}, state).__aiter__()

    first_event = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
    assert first_event["type"] == "tool.completed"
    assert first_event["payload"]["decision"] == "direct_response"

    first_delta = await asyncio.wait_for(stream.__anext__(), timeout=0.5)
    assert first_delta["type"] == "chat.delta"
    assert first_delta["payload"]["content"] == "Live "

    proceed.set()
    remaining = [event async for event in stream]
    assert [event["type"] for event in remaining] == [
        "chat.delta",
        "chat.message",
    ]
    assert remaining[0]["payload"]["content"] == "answer."
    assert remaining[-1]["payload"]["content"] == "Live answer."


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

    step_calls = {"n": 0}

    async def fake_run_agent_step(_turn, _cc, _llm=None, scratchpad=None, on_delta=None):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return AgentDecision(
                kind="tool",
                tool_call=AgentToolCall(
                    name="check_data_availability",
                    arguments={"bank_symbols": ["RY-CA"]},
                ),
            )
        return AgentDecision(kind="direct", content="RBC has RY-CA Q1 2026 coverage.")

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("availability should not run quick search")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
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

    types = [event["type"] for event in events]
    assert types[:4] == [
        "tool.started",
        "widget.created",
        "tool.completed",
        "widget.completed",
    ]
    assert events[2]["payload"]["name"] == "check_data_availability"
    assert events[3]["payload"]["widget"]["kind"] == "data_availability"
    assert events[3]["payload"]["widget"]["data"]["rows"][0]["bank_symbol"] == "RY-CA"
    # Coverage is fed back and the agent authors the reply (not a templated line).
    assert types[-1] == "chat.message"
    assert events[-1]["payload"]["content"] == "RBC has RY-CA Q1 2026 coverage."


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

    async def fake_run_agent_step(_turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None):
        return AgentDecision(
            kind="tool",
            tool_call=AgentToolCall(
                name="ask_clarification",
                arguments={
                    "question": "Which bank and fiscal period should I use?",
                    "presentation": "widget",
                    "missing_scope": ["bank", "fiscal_year", "quarter"],
                },
            ),
        )

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("quick research should not run before clarification")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
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
    assert events[0]["payload"]["name"] == "ask_clarification"
    assert events[0]["payload"]["presentation"] == "widget"
    widget = events[1]["payload"]["widget"]
    assert widget["kind"] == "clarification"
    assert widget["actions"][0]["action_type"] == "clarification_reply"
    assert widget["actions"][0]["payload"]["filters"]["bank_symbols"] == ["RY-CA"]


@pytest.mark.asyncio
async def test_research_with_explicit_text_scope_runs_quick_search(monkeypatch) -> None:
    """Research should feed evidence back so the same agent authors the answer."""
    step_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return AgentDecision(
                kind="tool",
                tool_call=AgentToolCall(
                    name="run_research",
                    id="call_research",
                    arguments={
                        "question": "Compare RBC Q1 2026 CET1 trends",
                        "search_mode": "quick",
                        "source_ids": ["rts"],
                        "combinations": [
                            {
                                "bank_symbol": "RY-CA",
                                "fiscal_year": 2026,
                                "quarter": "Q1",
                            }
                        ],
                    },
                ),
            )
        # Second step: the retrieved evidence is fed back through the scratchpad,
        # and the same agent authors the final answer in its own voice.
        assert scratchpad, "evidence should be fed back to the agent"
        assert scratchpad[-1]["role"] == "tool"
        return AgentDecision(kind="direct", content="No matching evidence.")

    async def fake_retrieve_quick_evidence(turn, **_kwargs):
        assert turn.bank_symbols == ["RY-CA"]
        assert turn.fiscal_years == [2026]
        assert turn.quarters == ["Q1"]
        return RetrievalResult(chunks=[], gaps=[])

    async def fail_stream_synthesis(*_args, **_kwargs):
        raise AssertionError("agent authors the answer; synthesis is not the path")
        if False:
            yield ""

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.stream_synthesis",
        fail_stream_synthesis,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "Compare RBC Q1 2026 CET1 trends", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    types = [event["type"] for event in events]
    assert step_calls["n"] == 2
    assert "widget.completed" not in types
    assert "artifact.created" in types
    assert "final_response.started" in types
    assert types[-1] == "chat.message"
    assert "aegis_final_shell" in events[-1]["payload"]["content"]
    assert events[-1]["payload"]["content"].rstrip().endswith("No matching evidence.")


@pytest.mark.asyncio
async def test_research_feeds_evidence_back_with_citation_ids(monkeypatch) -> None:
    """Retrieved evidence reaches the agent with stable [[source:chunk]] ids."""
    from aegis_agent.v2.agent.models import EvidenceChunk

    seen_scratchpad: dict[str, object] = {}
    step_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return AgentDecision(
                kind="tool",
                tool_call=AgentToolCall(
                    name="run_research",
                    id="call_research",
                    arguments={
                        "question": "RBC Q1 2026 CET1",
                        "search_mode": "quick",
                        "source_ids": ["rts"],
                        "combinations": [
                            {"bank_symbol": "RY-CA", "fiscal_year": 2026, "quarter": "Q1"}
                        ],
                    },
                ),
            )
        seen_scratchpad["tool_message"] = scratchpad[-1]
        return AgentDecision(
            kind="direct", content="RBC CET1 was 13.7% [[rts:chunk-1]]."
        )

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        chunk = EvidenceChunk(
            source_name="rts",
            source_display_name="Reports to shareholders",
            bank_ticker="RY-CA",
            fiscal_year=2026,
            quarter="Q1",
            page_number=9,
            chunk_id="chunk-1",
            chunk_content="CET1 ratio was 13.7% at quarter end.",
        )
        return RetrievalResult(chunks=[chunk], gaps=[])

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 CET1", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    tool_message = seen_scratchpad["tool_message"]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_research"
    assert "rts:chunk-1" in tool_message["content"]
    assert "13.7%" in tool_message["content"]
    assert events[-1]["payload"]["content"].rstrip().endswith("[[rts:chunk-1]].")


@pytest.mark.asyncio
async def test_agent_can_ask_plain_clarification_message(monkeypatch) -> None:
    """The agent can clarify with plain text when no option card is needed."""

    async def fake_run_agent_step(_turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None):
        return AgentDecision(
            kind="tool",
            tool_call=AgentToolCall(
                name="ask_clarification",
                arguments={
                    "question": "Which fiscal year should I use?",
                    "presentation": "message",
                    "missing_scope": ["fiscal_year"],
                },
            ),
        )

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("retrieval should not run before clarification")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fail_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "Compare RBC capital trends", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    assert [event["type"] for event in events] == ["tool.completed", "chat.message"]
    assert events[0]["payload"]["presentation"] == "message"
    assert events[-1]["payload"]["content"] == "Which fiscal year should I use?"


@pytest.mark.asyncio
async def test_unscoped_quick_research_clarifies_instead_of_failing(
    monkeypatch,
) -> None:
    """Quick retrieval should not leak its internal scope exception to chat."""

    async def fake_optional_context(_filters):
        return DataAvailabilityResponse(rows=[], fiscal_years=[], quarters=[])

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("unscoped quick search should ask for clarification first")

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
        async for event in _run_quick_research_turn(
            state,
            normalize_turn(
                {"content": "compare capital trends", "filters": {"source_ids": ["rts"]}}
            ),
        )
    ]

    assert [event["type"] for event in events] == [
        "tool.completed",
        "widget.completed",
    ]
    assert events[0]["payload"]["name"] == "ask_clarification"
    assert events[0]["payload"]["missing_scope"] == [
        "bank",
        "fiscal_year",
        "quarter",
    ]


@pytest.mark.asyncio
async def test_research_question_clarification_is_not_overridden(monkeypatch) -> None:
    """A bank/period-only prompt should still clarify the research intent."""

    async def fake_run_agent_step(_turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None):
        return AgentDecision(
            kind="tool",
            tool_call=AgentToolCall(
                name="ask_clarification",
                arguments={
                    "question": "What should I research for RBC Q1 2026?",
                    "presentation": "widget",
                    "missing_scope": ["research_question"],
                },
            ),
        )

    async def fake_optional_context(_filters):
        return DataAvailabilityResponse(rows=[], fiscal_years=[], quarters=[])

    async def fail_retrieve_quick_evidence(*_args, **_kwargs):
        raise AssertionError("retrieval should not run without research intent")

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
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
    assert events[0]["payload"]["name"] == "ask_clarification"
    assert events[0]["payload"]["presentation"] == "widget"
    widget = events[1]["payload"]["widget"]
    assert widget["data"]["missing_scope"] == ["research_question"]
    assert "What should I research" in widget["html"]


@pytest.mark.asyncio
async def test_agent_loop_replans_when_step_requests_continue(monkeypatch) -> None:
    """The V2 loop should support non-terminal agent steps."""
    step_calls = 0

    async def fake_run_agent_step(_turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None):
        nonlocal step_calls
        step_calls += 1
        return AgentDecision(kind="direct", content="step")

    async def fake_run_agent_decision(state, _turn, _decision, *, outcome, step_index):
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

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator._run_agent_decision",
        fake_run_agent_decision,
    )

    state = V2SessionState(session_id="session_test")
    events = [event async for event in run_turn({"content": "continue test"}, state)]

    assert step_calls == 2
    assert events[-1]["type"] == "chat.message"
    assert events[-1]["payload"]["content"] == "completed after replan"


@pytest.mark.asyncio
async def test_agent_loop_limit_surfaces_failure(monkeypatch) -> None:
    """The bounded loop should fail explicitly instead of spinning forever."""
    step_calls = 0

    async def fake_run_agent_step(_turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None):
        nonlocal step_calls
        step_calls += 1
        return AgentDecision(kind="direct", content="never terminal")

    async def fake_run_agent_decision(_state, _turn, _decision, *, outcome, step_index):
        outcome.status = "continue"
        outcome.reason = f"continue_{step_index}"
        if False:
            yield {}

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator._run_agent_decision",
        fake_run_agent_decision,
    )

    state = V2SessionState(session_id="session_test")
    events = [event async for event in run_turn({"content": "loop limit"}, state)]

    assert step_calls == MAX_AGENT_STEPS
    assert events[-2]["type"] == "tool.failed"
    assert events[-2]["payload"]["name"] == "agent_loop"
    assert events[-1]["type"] == "chat.message"


@pytest.mark.asyncio
async def test_loop_limit_with_open_shell_closes_the_stream(monkeypatch) -> None:
    """If the loop limit is hit after a shell was emitted, the open answer stream
    is closed with a final message instead of being orphaned (F10)."""

    def _present_decision() -> AgentDecision:
        return AgentDecision(
            kind="tool",
            tool_call=AgentToolCall(
                name="present_final_response",
                id="call_present",
                arguments={
                    "headline": "Capital",
                    "tiles": [{"label": "CET1", "value": "13.7%"}],
                },
            ),
        )

    step_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return _research_tool_decision()
        return _present_decision()  # never writes the body -> burns the budget

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        return RetrievalResult(chunks=[], gaps=[])

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 capital", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    types = [event["type"] for event in events]
    assert "tool.failed" in types
    assert types[-1] == "chat.message"
    # The open answer stream is closed: final + bound to the stream + carries shell.
    assert events[-1]["payload"].get("final") is True
    assert events[-1]["payload"].get("stream_id") == state.turn_stream_id
    assert "aegis_final_shell" in events[-1]["payload"]["content"]


def _research_tool_decision() -> AgentDecision:
    return AgentDecision(
        kind="tool",
        tool_call=AgentToolCall(
            name="run_research",
            id="call_research",
            arguments={
                "question": "RBC Q1 2026 CET1",
                "search_mode": "quick",
                "source_ids": ["rts"],
                "combinations": [
                    {"bank_symbol": "RY-CA", "fiscal_year": 2026, "quarter": "Q1"}
                ],
            },
        ),
    )


def test_quick_feedback_returns_full_sixty_chunk_budget() -> None:
    """Quick research should feed all retained chunks back to the agent compactly."""
    from aegis_agent.v2.agent.models import EvidenceChunk

    chunks = [
        EvidenceChunk(
            source_name="rts",
            source_display_name="Reports to shareholders",
            bank_ticker="RY",
            fiscal_year=2026,
            quarter="Q1",
            page_number=index + 1,
            chunk_id=f"chunk-{index}",
            chunk_content=" ".join(["capital"] * 80),
        )
        for index in range(60)
    ]

    feedback = _quick_evidence_feedback(chunks, [])
    message = _tool_result_message("call_1", feedback)

    assert feedback["retained_chunks"] == 60
    assert len(feedback["evidence"]) == 60
    assert feedback["evidence"][-1]["evidence_id"] == "rts:chunk-59"
    assert len(feedback["evidence"][0]["text"]) <= 320
    assert "rts:chunk-59" in message["content"]


@pytest.mark.asyncio
async def test_research_answer_streams_tokens_live(monkeypatch) -> None:
    """The agent's answer body streams as live chat.delta tokens after the shell."""
    step_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return _research_tool_decision()
        for token in ["CET1 ", "was ", "13.7%."]:
            if on_delta is not None:
                on_delta(token)
        return AgentDecision(kind="direct", content="CET1 was 13.7%.")

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        return RetrievalResult(chunks=[], gaps=[])

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 CET1", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    types = [event["type"] for event in events]
    delta_contents = [
        event["payload"]["content"] for event in events if event["type"] == "chat.delta"
    ]
    assert delta_contents == ["CET1 ", "was ", "13.7%."]
    assert types.index("final_response.started") < types.index("chat.delta")
    assert types[-1] == "chat.message"
    assert "aegis_final_shell" in events[-1]["payload"]["content"]
    assert events[-1]["payload"]["content"].rstrip().endswith("13.7%.")


@pytest.mark.asyncio
async def test_answer_step_failure_falls_back_to_synthesis(monkeypatch) -> None:
    """If the agent fails to author after research, synthesis renders the answer."""
    step_calls = {"n": 0}
    synth_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return _research_tool_decision()
        raise RuntimeError("model exploded mid-answer")

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        return RetrievalResult(chunks=[], gaps=[])

    async def fake_stream_synthesis(_turn, **_kwargs):
        synth_calls["n"] += 1
        yield "Fallback synthesized answer."

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.stream_synthesis", fake_stream_synthesis
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 CET1", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    types = [event["type"] for event in events]
    assert synth_calls["n"] == 1
    assert "artifact.created" in types
    assert "final_response.started" in types
    assert types[-1] == "chat.message"
    assert "Fallback synthesized answer." in events[-1]["payload"]["content"]
    assert "aegis_final_shell" in events[-1]["payload"]["content"]


@pytest.mark.asyncio
async def test_body_failure_after_present_reuses_shell_and_closes_stream(
    monkeypatch,
) -> None:
    """If the body step fails after present, synthesis reuses the emitted shell and
    closes the stream rather than orphaning it (F6)."""
    step_calls = {"n": 0}
    synth_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return _research_tool_decision()
        if step_calls["n"] == 2:
            return AgentDecision(
                kind="tool",
                tool_call=AgentToolCall(
                    name="present_final_response",
                    id="call_present",
                    arguments={
                        "headline": "Agent shell",
                        "tiles": [{"label": "CET1", "value": "13.7%"}],
                    },
                ),
            )
        raise RuntimeError("model exploded while writing the body")

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        return RetrievalResult(chunks=[], gaps=[])

    async def fake_stream_synthesis(_turn, **_kwargs):
        synth_calls["n"] += 1
        yield "Fallback body."

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.stream_synthesis", fake_stream_synthesis
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 capital", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    types = [event["type"] for event in events]
    # Exactly one shell was emitted (the agent's present shell), not a second one.
    assert types.count("final_response.started") == 1
    started = next(e for e in events if e["type"] == "final_response.started")
    assert started["payload"]["shell"]["summary"]["headline"] == "Agent shell"
    # Synthesis rendered the body and the stream is closed with a final message.
    assert synth_calls["n"] == 1
    assert types[-1] == "chat.message"
    assert events[-1]["payload"].get("final") is True
    assert "Fallback body." in events[-1]["payload"]["content"]
    assert "aegis_final_shell" in events[-1]["payload"]["content"]


@pytest.mark.asyncio
async def test_agent_presents_structured_evidence_backed_tiles(monkeypatch) -> None:
    """present_final_response tiles are agent-authored and evidence-id validated."""
    from aegis_agent.v2.agent.models import EvidenceChunk

    step_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return _research_tool_decision()
        if step_calls["n"] == 2:
            return AgentDecision(
                kind="tool",
                tool_call=AgentToolCall(
                    name="present_final_response",
                    id="call_present",
                    arguments={
                        "headline": "RBC Q1 2026 capital",
                        "tiles": [
                            {
                                "label": "CET1 ratio",
                                "value": "13.7%",
                                "context": "RY-CA Q1 2026",
                                # one valid id, one hallucinated id that must drop
                                "evidence_ids": ["rts:chunk-1", "rts:made-up"],
                            }
                        ],
                    },
                ),
            )
        return AgentDecision(kind="direct", content="Capital strengthened [[rts:chunk-1]].")

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        chunk = EvidenceChunk(
            source_name="rts",
            source_display_name="Reports to shareholders",
            bank_ticker="RY-CA",
            fiscal_year=2026,
            quarter="Q1",
            chunk_id="chunk-1",
            # Note: no numeric value in the text, so a regex extractor could not
            # have produced the 13.7% tile — only the agent could.
            chunk_content="Management described capital as strong this quarter.",
        )
        return RetrievalResult(chunks=[chunk], gaps=[])

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 capital", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    assert step_calls["n"] == 3
    started = next(e for e in events if e["type"] == "final_response.started")
    shell = started["payload"]["shell"]
    assert shell["summary"]["headline"] == "RBC Q1 2026 capital"
    assert len(shell["tiles"]) == 1
    tile = shell["tiles"][0]
    assert tile["label"] == "CET1 ratio"
    assert tile["value"] == "13.7%"
    # Hallucinated evidence id was filtered out; only the retrieved id remains.
    assert tile["evidence_ids"] == ["rts:chunk-1"]
    assert events[-1]["type"] == "chat.message"
    assert "aegis_final_shell" in events[-1]["payload"]["content"]


@pytest.mark.asyncio
async def test_tile_with_only_hallucinated_evidence_is_dropped(monkeypatch) -> None:
    """A tile whose citations are all hallucinated is dropped; an uncited tile is
    kept (F9)."""
    from aegis_agent.v2.agent.models import EvidenceChunk

    step_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return _research_tool_decision()
        if step_calls["n"] == 2:
            return AgentDecision(
                kind="tool",
                tool_call=AgentToolCall(
                    name="present_final_response",
                    id="call_present",
                    arguments={
                        "headline": "Capital",
                        "tiles": [
                            {
                                "label": "Backed",
                                "value": "13.7%",
                                "evidence_ids": ["rts:chunk-1"],
                            },
                            {
                                "label": "Fabricated",
                                "value": "99%",
                                "evidence_ids": ["rts:made-up"],
                            },
                            {"label": "Uncited", "value": "n/a"},
                        ],
                    },
                ),
            )
        return AgentDecision(kind="direct", content="Body [[rts:chunk-1]].")

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        chunk = EvidenceChunk(
            source_name="rts",
            source_display_name="Reports to shareholders",
            bank_ticker="RY-CA",
            fiscal_year=2026,
            quarter="Q1",
            chunk_id="chunk-1",
            chunk_content="Capital commentary.",
        )
        return RetrievalResult(chunks=[chunk], gaps=[])

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 capital", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    started = next(e for e in events if e["type"] == "final_response.started")
    labels = [tile["label"] for tile in started["payload"]["shell"]["tiles"]]
    assert labels == ["Backed", "Uncited"]  # "Fabricated" dropped


@pytest.mark.asyncio
async def test_two_research_calls_keep_both_evidence_sets_for_citations(
    monkeypatch,
) -> None:
    """A second run_research must not drop the first call's evidence ids (F5)."""
    from aegis_agent.v2.agent.models import EvidenceChunk

    step_calls = {"n": 0}
    retrieve_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] in (1, 2):
            return _research_tool_decision()
        if step_calls["n"] == 3:
            return AgentDecision(
                kind="tool",
                tool_call=AgentToolCall(
                    name="present_final_response",
                    id="call_present",
                    arguments={
                        "headline": "Two-source capital",
                        "tiles": [
                            {
                                "label": "CET1 ratio",
                                "value": "13.7%",
                                # one id from each research call
                                "evidence_ids": ["rts:chunk-1", "rts:chunk-2"],
                            }
                        ],
                    },
                ),
            )
        for token in ["Capital ", "[[rts:chunk-1]] ", "[[rts:chunk-2]]."]:
            if on_delta is not None:
                on_delta(token)
        return AgentDecision(
            kind="direct", content="Capital [[rts:chunk-1]] [[rts:chunk-2]]."
        )

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        retrieve_calls["n"] += 1
        chunk_id = f"chunk-{retrieve_calls['n']}"
        chunk = EvidenceChunk(
            source_name="rts",
            source_display_name="Reports to shareholders",
            bank_ticker="RY-CA",
            fiscal_year=2026,
            quarter="Q1",
            chunk_id=chunk_id,
            chunk_content=f"Capital commentary {chunk_id}.",
        )
        return RetrievalResult(chunks=[chunk], gaps=[])

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 capital", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    assert retrieve_calls["n"] == 2
    started = next(e for e in events if e["type"] == "final_response.started")
    tile = started["payload"]["shell"]["tiles"][0]
    # Both research calls' evidence ids survived validation (neither dropped).
    assert tile["evidence_ids"] == ["rts:chunk-1", "rts:chunk-2"]


@pytest.mark.asyncio
async def test_preamble_before_present_tool_call_does_not_leak_or_clobber_tiles(
    monkeypatch,
) -> None:
    """A content preamble emitted in the same step as present_final_response is
    buffered and discarded, so it neither leaks as chat.delta nor flips the
    shell state that would discard the agent-authored tiles (F1)."""
    from aegis_agent.v2.agent.models import EvidenceChunk

    step_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return _research_tool_decision()
        if step_calls["n"] == 2:
            # Model narrates before calling the tool in the same step. This must
            # not reach the answer stream.
            if on_delta is not None:
                on_delta("Let me line up the capital metrics. ")
            return AgentDecision(
                kind="tool",
                tool_call=AgentToolCall(
                    name="present_final_response",
                    id="call_present",
                    arguments={
                        "headline": "Agent headline",
                        "tiles": [
                            {
                                "label": "CET1 ratio",
                                "value": "13.7%",
                                "evidence_ids": ["rts:chunk-1"],
                            }
                        ],
                    },
                ),
            )
        for token in ["Capital ", "strengthened ", "[[rts:chunk-1]]."]:
            if on_delta is not None:
                on_delta(token)
        return AgentDecision(
            kind="direct", content="Capital strengthened [[rts:chunk-1]]."
        )

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        chunk = EvidenceChunk(
            source_name="rts",
            source_display_name="Reports to shareholders",
            bank_ticker="RY-CA",
            fiscal_year=2026,
            quarter="Q1",
            chunk_id="chunk-1",
            chunk_content="Management described capital as strong this quarter.",
        )
        return RetrievalResult(chunks=[chunk], gaps=[])

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 capital", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    delta_contents = [
        event["payload"]["content"]
        for event in events
        if event["type"] == "chat.delta"
    ]
    # The preamble was discarded; only the real body tokens streamed.
    assert delta_contents == ["Capital ", "strengthened ", "[[rts:chunk-1]]."]
    # The agent-authored shell (not the deterministic regex shell) was emitted.
    started = next(e for e in events if e["type"] == "final_response.started")
    assert started["payload"]["shell"]["summary"]["headline"] == "Agent headline"
    assert started["payload"]["shell"]["tiles"][0]["value"] == "13.7%"
    assert events[-1]["type"] == "chat.message"
    assert "aegis_final_shell" in events[-1]["payload"]["content"]


@pytest.mark.asyncio
async def test_skipping_present_falls_back_to_deterministic_shell(monkeypatch) -> None:
    """If the agent skips present_final_response, deterministic tiles still render."""
    step_calls = {"n": 0}

    async def fake_run_agent_step(
        _turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None
    ):
        step_calls["n"] += 1
        if step_calls["n"] == 1:
            return _research_tool_decision()
        return AgentDecision(kind="direct", content="CET1 was 13.7%. [[rts:chunk-1]]")

    async def fake_retrieve_quick_evidence(_turn, **_kwargs):
        from aegis_agent.v2.agent.models import EvidenceChunk

        chunk = EvidenceChunk(
            source_name="rts",
            source_display_name="Reports to shareholders",
            bank_ticker="RY-CA",
            fiscal_year=2026,
            quarter="Q1",
            chunk_id="chunk-1",
            chunk_content="CET1 ratio was 13.7% at quarter end.",
        )
        return RetrievalResult(chunks=[chunk], gaps=[])

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.retrieve_quick_evidence",
        fake_retrieve_quick_evidence,
    )

    state = V2SessionState(session_id="session_test")
    events = [
        event
        async for event in run_turn(
            {"content": "RBC Q1 2026 CET1", "filters": {"source_ids": ["rts"]}},
            state,
        )
    ]

    assert step_calls["n"] == 2
    started = next(e for e in events if e["type"] == "final_response.started")
    # Deterministic fallback still produces a usable shell with tiles.
    assert started["payload"]["shell"]["tiles"]
    assert events[-1]["type"] == "chat.message"
    assert "aegis_final_shell" in events[-1]["payload"]["content"]
