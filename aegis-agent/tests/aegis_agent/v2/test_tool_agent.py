"""Tests for the V2 single tool-calling agent contract."""

from __future__ import annotations

import json

import pytest

from aegis_agent.v2.agent.conversation import ConversationContext
from aegis_agent.v2.agent.models import normalize_turn
from aegis_agent.v2.agent.tool_agent import AgentDecision, run_agent_step


@pytest.mark.asyncio
async def test_run_agent_step_allows_direct_answers(monkeypatch) -> None:
    """The agent can answer directly without a tool call."""
    captured: dict[str, object] = {}

    async def fake_complete_with_tools(messages, tools, _context, llm_params):
        captured["messages"] = messages
        captured["tools"] = tools
        captured["llm_params"] = llm_params
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Hi. I can help with Aegis research.",
                    }
                }
            ]
        }

    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.complete_with_tools",
        fake_complete_with_tools,
    )

    decision = await run_agent_step(
        normalize_turn({"content": "hi"}),
        ConversationContext(),
        llm_context={"execution_id": "test"},
    )

    assert decision == AgentDecision(
        kind="direct", content="Hi. I can help with Aegis research."
    )
    tool_names = {
        tool["function"]["name"] for tool in captured["tools"]  # type: ignore[index]
    }
    assert tool_names == {
        "ask_clarification",
        "check_data_availability",
        "run_research",
    }
    assert captured["llm_params"]["tool_choice"] == "auto"  # type: ignore[index]


@pytest.mark.asyncio
async def test_run_agent_step_parses_availability_tool_call(monkeypatch) -> None:
    """Coverage requests can become an explicit availability tool call."""

    async def fake_complete_with_tools(_messages, _tools, _context, _llm_params):
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "check_data_availability",
                                    "arguments": json.dumps(
                                        {"bank_symbols": ["RY-CA"]}
                                    ),
                                },
                            }
                        ],
                    }
                }
            ]
        }

    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.complete_with_tools",
        fake_complete_with_tools,
    )

    decision = await run_agent_step(
        normalize_turn({"content": "what data do you have for rbc"}),
        ConversationContext(),
        llm_context={"execution_id": "test"},
    )

    assert decision.kind == "tool"
    assert decision.tool_call is not None
    assert decision.tool_call.name == "check_data_availability"
    assert decision.tool_call.arguments == {"bank_symbols": ["RY-CA"]}
