"""Tests for the first V2 websocket orchestration loop."""

from __future__ import annotations

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
    _run_quick_research_turn,
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

    async def fake_run_agent_step(_turn, _conversation_context, _llm_context=None, scratchpad=None, on_delta=None):
        return AgentDecision(
            kind="tool",
            tool_call=AgentToolCall(
                name="check_data_availability",
                arguments={},
            ),
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
async def test_greeting_routes_to_plain_general_chat(monkeypatch) -> None:
    """Simple greetings should behave like chat, not research output."""

    async def fake_run_agent_step(*_args, **_kwargs):
        return AgentDecision(
            kind="direct",
            content=(
                "Hi. I can check data availability, find source documents, and "
                "run research."
            ),
        )

    async def fail_stream_synthesis(*_args, **_kwargs):
        raise AssertionError("greeting should not need LLM synthesis")
        if False:
            yield ""

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
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
    assert events[0]["payload"]["decision"] == "direct_response"
    assert "aegis_final_shell" not in events[-1]["payload"]["content"]
    assert "Hi. I can check data availability" in events[-1]["payload"]["content"]


@pytest.mark.asyncio
async def test_capabilities_question_gets_static_chat_not_prompt_echo(monkeypatch) -> None:
    """The help path should be useful chat, not a leaked prompt/context response."""

    async def fake_run_agent_step(*_args, **_kwargs):
        return AgentDecision(
            kind="direct",
            content="I can check which sources are available and run research.",
        )

    async def fail_stream_synthesis(*_args, **_kwargs):
        raise AssertionError("capabilities help should not need LLM synthesis")
        if False:
            yield ""

    monkeypatch.setattr("aegis_agent.v2.orchestrator.run_agent_step", fake_run_agent_step)
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

    async def fake_run_agent_step(*_args, **_kwargs):
        return AgentDecision(
            kind="tool",
            tool_call=AgentToolCall(
                name="check_data_availability",
                arguments={"bank_symbols": ["RY-CA"]},
            ),
        )

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
