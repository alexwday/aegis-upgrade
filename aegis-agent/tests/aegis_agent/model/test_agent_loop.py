"""Tests for the single agent loop."""

from __future__ import annotations

import pytest

from aegis_agent.model.agents.aegis_agent import (
    _source_scope_message,
    run_aegis_agent,
)
from aegis_agent.model.agents.schemas import DEFAULT_DOCUMENT_SOURCES


@pytest.fixture(autouse=True)
def stub_system_prompt(monkeypatch) -> None:
    """Keep loop tests independent from the live prompt database."""
    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent._load_system_prompt",
        lambda: "You are Aegis.",
    )


def test_source_scope_message_lists_all_default_sources() -> None:
    """The model should see all six sources as the unfiltered default scope."""
    message = _source_scope_message({"db_names": list(DEFAULT_DOCUMENT_SOURCES)})

    assert "all 6 sources" in message
    assert "transcripts, event_transcripts, investor_slides" in message
    assert "supplementary_financials, rts, pillar3" in message
    assert "all four" not in message


def test_source_scope_message_honors_user_filter() -> None:
    """The model should know when the UI selected a narrower source scope."""
    message = _source_scope_message(
        {"source_filter": ["event_transcripts", "transcripts"]}
    )

    assert "User-selected source filter" in message
    assert "event_transcripts, transcripts" in message
    assert "Only call run_research with those source IDs" in message
    assert "all 6 sources" not in message


@pytest.mark.asyncio
async def test_agent_emits_choice_card(monkeypatch) -> None:
    """The agent should surface small clarification choices as ui_card events."""

    async def fake_stream_with_tools(*_args, **_kwargs):
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "tool-1",
                                "type": "function",
                                "function": {
                                    "name": "present_choice_card",
                                    "arguments": (
                                        '{"question":"Which quarter?","options":['
                                        '{"id":"q1","label":"Q1 2026"},'
                                        '{"id":"q4","label":"Q4 2025"}]}'
                                    ),
                                },
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )
    events = [
        event
        async for event in run_aegis_agent(
            [{"role": "user", "content": "Compare RBC and TD for last quarter"}],
            {"execution_id": "test"},
        )
    ]

    assert events[0]["type"] == "ui_card"
    assert events[0]["content"]["question"] == "Which quarter?"


@pytest.mark.asyncio
async def test_agent_rejects_incomplete_research_tool_call(monkeypatch) -> None:
    """Incomplete research tool calls should loop back to the model, not retrieval."""
    calls = 0

    async def fake_stream_with_tools(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {"choices": [{"delta": {"content": "I'll use all four sources for RBC. "}}]}
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "tool-1",
                                    "type": "function",
                                    "function": {
                                        "name": "run_research",
                                        "arguments": (
                                            '{"question":"credit quality",'
                                            '"combinations":[{"bank_symbol":"RY-CA"}]}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return
        yield {
            "choices": [
                {
                    "delta": {
                        "content": "Which fiscal year and quarter should I use?",
                    }
                }
            ]
        }

    async def fake_run_research_tool(*_args, **_kwargs):
        raise AssertionError("research retrieval should not run")

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )
    monkeypatch.setattr(
        "aegis_agent.model.agents.tools.run_research_tool",
        fake_run_research_tool,
    )
    events = [
        event
        async for event in run_aegis_agent(
            [{"role": "user", "content": "What did RBC say about credit quality?"}],
            {"execution_id": "test"},
        )
    ]

    assert events == [
        {
            "type": "agent",
            "name": "aegis",
            "content": "Which fiscal year and quarter should I use?",
        }
    ]


@pytest.mark.asyncio
async def test_agent_research_shell_then_streams_body(monkeypatch) -> None:
    """Research answers should declare the final shell before body chunks stream."""
    calls = 0

    async def fake_stream_with_tools(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "research-1",
                                    "type": "function",
                                    "function": {
                                        "name": "run_research",
                                        "arguments": (
                                            '{"question":"CET1 capital",'
                                            '"sources":["investor_slides"],'
                                            '"combinations":[{"bank_symbol":"RY-CA",'
                                            '"fiscal_year":2026,"quarter":"Q1"}]}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return
        if calls == 2:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "final-1",
                                    "type": "function",
                                    "function": {
                                        "name": "start_final_response",
                                        "arguments": (
                                            '{"render_mode":"default_brief",'
                                            '"body_style":"default_brief",'
                                            '"summary":{"headline":"RBC CET1 was solid"},'
                                            '"tiles":[{"label":"CET1",'
                                            '"value":"13.2%","evidence_ids":["E1"]}]}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return
        yield {"choices": [{"delta": {"content": "RBC reported CET1 strength "}}]}
        yield {"choices": [{"delta": {"content": "supported by capital disclosures. [[E1]]"}}]}

    async def fake_run_research_tool(*_args, **_kwargs):
        return {"status": "success", "quick_summary": "Research complete", "findings": []}

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )
    monkeypatch.setattr(
        "aegis_agent.model.agents.tools.run_research_tool",
        fake_run_research_tool,
    )

    events = [
        event
        async for event in run_aegis_agent(
            [{"role": "user", "content": "Using investor slides, summarize RBC Q1 2026 CET1."}],
            {"execution_id": "test"},
        )
    ]

    assert [event["type"] for event in events] == [
        "final_response_start",
        "agent",
        "agent",
    ]
    assert all("I'll use all four sources" not in str(event) for event in events)
    assert events[0]["content"]["summary"]["headline"] == "RBC CET1 was solid"
    assert "RBC reported CET1" in events[1]["content"]
    assert "[[E1]]" in events[2]["content"]
