"""Tests for agent tool dispatch behavior."""

from __future__ import annotations

import pytest

from aegis_agent.model.agents.tools import AGENT_TOOLS, dispatch_tool_call, is_research_scope_complete
from aegis_agent.model.agents.schemas import DEFAULT_DOCUMENT_SOURCES, ResearchRequest


def _tool_call(name: str, arguments: str) -> dict:
    return {"id": "tool-1", "function": {"name": name, "arguments": arguments}}


def test_incomplete_research_scope_is_not_complete() -> None:
    """Research scope should require bank, year, quarter, and question."""
    assert not is_research_scope_complete(
        {"question": "credit quality", "combinations": [{"bank_symbol": "RY-CA"}]}
    )


def test_research_request_default_sources_include_transcript_sources() -> None:
    """An omitted sources list should resolve to the full six-source default."""
    request = ResearchRequest.model_validate(
        {
            "question": "earnings performance",
            "combinations": [
                {"bank_symbol": "RY-CA", "fiscal_year": 2026, "quarter": "Q1"}
            ],
        }
    )

    assert request.sources == DEFAULT_DOCUMENT_SOURCES
    assert "transcripts" in request.sources
    assert "event_transcripts" in request.sources


def test_final_response_tool_is_not_exposed_to_live_model() -> None:
    """The live model should stream the shell inline instead of making a tool round trip."""
    tool_names = {tool["function"]["name"] for tool in AGENT_TOOLS}

    assert "start_final_response" not in tool_names


@pytest.mark.asyncio
async def test_dispatch_does_not_run_research_before_scope_is_clear(monkeypatch) -> None:
    """The dispatcher should reject underspecified research before retrieval."""
    called = False

    async def fake_run_research_tool(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"status": "success"}

    monkeypatch.setattr("aegis_agent.model.agents.tools.run_research_tool", fake_run_research_tool)
    result = await dispatch_tool_call(
        _tool_call(
            "run_research", '{"question":"credit quality","combinations":[{"bank_symbol":"RY-CA"}]}'
        ),
        context={"execution_id": "test"},
    )

    assert result["status"] == "needs_clarification"
    assert not called


@pytest.mark.asyncio
async def test_dispatch_applies_context_source_filter(monkeypatch) -> None:
    """A user-selected source filter should override model-supplied sources."""
    captured_arguments = {}

    async def fake_run_research_tool(arguments, *_args, **_kwargs):
        captured_arguments.update(arguments)
        return {"status": "success"}

    monkeypatch.setattr("aegis_agent.model.agents.tools.run_research_tool", fake_run_research_tool)
    result = await dispatch_tool_call(
        _tool_call(
            "run_research",
            (
                '{"question":"credit quality","sources":["investor_slides","rts"],'
                '"combinations":[{"bank_symbol":"RY-CA","fiscal_year":2026,"quarter":"Q1"}]}'
            ),
        ),
        context={"execution_id": "test", "source_filter": ["transcripts"]},
    )

    assert result["status"] == "success"
    assert captured_arguments["sources"] == ["transcripts"]


@pytest.mark.asyncio
async def test_dispatch_emits_choice_card() -> None:
    """Choice-card tools should emit a ui_card event and await the next user turn."""
    queue = __import__("asyncio").Queue()
    result = await dispatch_tool_call(
        _tool_call(
            "present_choice_card",
            (
                '{"question":"Which quarter?","options":['
                '{"id":"q1","label":"Q1 2026"},{"id":"q4","label":"Q4 2025"}]}'
            ),
        ),
        context={"execution_id": "test"},
        output_queue=queue,
    )
    event = await queue.get()

    assert result["status"] == "awaiting_user"
    assert event["type"] == "ui_card"
    assert event["content"]["question"] == "Which quarter?"


@pytest.mark.asyncio
async def test_dispatch_emits_final_response_start() -> None:
    """The final-response tool should emit a structured shell event."""
    queue = __import__("asyncio").Queue()
    result = await dispatch_tool_call(
        _tool_call(
            "start_final_response",
            (
                '{"render_mode":"default_brief","body_style":"default_brief",'
                '"summary":{"headline":"RBC capital remains strong","dek":"Q1 readout"},'
                '"tiles":[{"label":"CET1","value":"13.2%","context":"Reported ratio",'
                '"evidence_ids":["E1"]}]}'
            ),
        ),
        context={"execution_id": "test"},
        output_queue=queue,
    )
    event = await queue.get()

    assert result["status"] == "final_response_started"
    assert event["type"] == "final_response_start"
    assert event["content"]["summary"]["headline"] == "RBC capital remains strong"
    assert event["content"]["tiles"][0]["evidence_ids"] == ["E1"]
