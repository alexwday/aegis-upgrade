"""Tests for the V2 single tool-calling agent contract."""

from __future__ import annotations

import json

import pytest

import aegis_agent.v2.agent.tool_agent as tool_agent_module
from aegis_agent.v2.agent.conversation import ConversationContext
from aegis_agent.v2.agent.models import normalize_turn
from aegis_agent.v2.agent.tool_agent import (
    FALLBACK_SYSTEM_PROMPT,
    RUNTIME_SYSTEM_RULES,
    AgentDecision,
    run_agent_step,
    warm_system_prompt,
)
from aegis_agent.v2.sources import SOURCE_IDS


@pytest.fixture(autouse=True)
def isolate_system_prompt(monkeypatch):
    """Keep prompt loading off the real DB; default to the inline fallback."""
    tool_agent_module._SYSTEM_PROMPT_CACHE = None

    def _no_db(*_args, **_kwargs):
        raise RuntimeError("prompt DB not available in tests")

    monkeypatch.setattr(tool_agent_module, "load_prompt_from_db", _no_db)
    yield
    tool_agent_module._SYSTEM_PROMPT_CACHE = None


def _content_chunk(text: str) -> dict:
    return {"choices": [{"delta": {"content": text}}]}


def _tool_chunk(name: str, arguments: str, call_id: str = "call_1") -> dict:
    return {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                }
            }
        ]
    }


@pytest.mark.asyncio
async def test_run_agent_step_streams_direct_answer(monkeypatch) -> None:
    """The agent streams a direct answer token-by-token via on_delta."""
    captured: dict[str, object] = {}
    deltas: list[str] = []

    async def fake_stream_with_tools(messages, tools, _context, llm_params):
        captured["messages"] = messages
        captured["tools"] = tools
        captured["llm_params"] = llm_params
        for piece in ["Hi. ", "I can help ", "with Aegis research."]:
            yield _content_chunk(piece)

    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.stream_with_tools",
        fake_stream_with_tools,
    )

    decision = await run_agent_step(
        normalize_turn({"content": "hi"}),
        ConversationContext(),
        llm_context={"execution_id": "test"},
        on_delta=deltas.append,
    )

    assert decision == AgentDecision(
        kind="direct", content="Hi. I can help with Aegis research."
    )
    assert deltas == ["Hi. ", "I can help ", "with Aegis research."]
    tool_names = {
        tool["function"]["name"] for tool in captured["tools"]  # type: ignore[index]
    }
    assert tool_names == {
        "ask_clarification",
        "check_data_availability",
        "run_research",
        "present_final_response",
    }
    assert captured["llm_params"]["tool_choice"] == "auto"  # type: ignore[index]


@pytest.mark.asyncio
async def test_run_agent_step_parses_streamed_availability_tool_call(monkeypatch) -> None:
    """Coverage requests become an availability tool call with no streamed body."""
    deltas: list[str] = []

    async def fake_stream_with_tools(_messages, _tools, _context, _llm_params):
        yield _tool_chunk(
            "check_data_availability", json.dumps({"bank_symbols": ["RY-CA"]})
        )

    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.stream_with_tools",
        fake_stream_with_tools,
    )

    decision = await run_agent_step(
        normalize_turn({"content": "what data do you have for rbc"}),
        ConversationContext(),
        llm_context={"execution_id": "test"},
        on_delta=deltas.append,
    )

    assert decision.kind == "tool"
    assert decision.tool_call is not None
    assert decision.tool_call.name == "check_data_availability"
    assert decision.tool_call.arguments == {"bank_symbols": ["RY-CA"]}
    assert decision.tool_call.id == "call_1"
    assert deltas == []


@pytest.mark.asyncio
async def test_run_agent_step_accumulates_split_tool_argument_deltas(monkeypatch) -> None:
    """Tool-call argument fragments split across chunks are reassembled."""

    async def fake_stream_with_tools(_messages, _tools, _context, _llm_params):
        yield _tool_chunk("run_research", '{"question": "CET1", ', call_id="call_9")
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '"combinations": []}'},
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.stream_with_tools",
        fake_stream_with_tools,
    )

    decision = await run_agent_step(
        normalize_turn({"content": "research"}),
        ConversationContext(),
        llm_context={"execution_id": "test"},
    )

    assert decision.kind == "tool"
    assert decision.tool_call is not None
    assert decision.tool_call.name == "run_research"
    assert decision.tool_call.arguments == {"question": "CET1", "combinations": []}
    assert decision.tool_call.id == "call_9"


@pytest.mark.asyncio
async def test_agent_loads_system_prompt_from_db(monkeypatch) -> None:
    """The system prompt comes from the versioned DB row when available."""
    captured: dict[str, object] = {}

    async def fake_stream_with_tools(messages, _tools, _context, _llm_params):
        captured["messages"] = messages
        yield _content_chunk("ok")

    def fake_load_prompt(layer, name, **_kwargs):
        assert (layer, name) == ("agent", "orchestrator")
        return {"system_prompt": "DB ORCHESTRATOR PROMPT"}

    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.stream_with_tools", fake_stream_with_tools
    )
    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.load_prompt_from_db", fake_load_prompt
    )
    tool_agent_module._SYSTEM_PROMPT_CACHE = None

    await run_agent_step(
        normalize_turn({"content": "hi"}),
        ConversationContext(),
        llm_context={"execution_id": "test"},
    )

    system_message = captured["messages"][0]  # type: ignore[index]
    assert system_message["role"] == "system"
    assert system_message["content"] == f"DB ORCHESTRATOR PROMPT\n\n{RUNTIME_SYSTEM_RULES}"


@pytest.mark.asyncio
async def test_agent_falls_back_to_inline_prompt_when_db_missing(monkeypatch) -> None:
    """A missing DB prompt row falls back to the inline prompt, keeping the agent live."""
    captured: dict[str, object] = {}

    async def fake_stream_with_tools(messages, _tools, _context, _llm_params):
        captured["messages"] = messages
        yield _content_chunk("ok")

    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.stream_with_tools", fake_stream_with_tools
    )
    # The autouse fixture already patches load_prompt_from_db to raise.

    await run_agent_step(
        normalize_turn({"content": "hi"}),
        ConversationContext(),
        llm_context={"execution_id": "test"},
    )

    system_message = captured["messages"][0]  # type: ignore[index]
    assert system_message["content"] == f"{FALLBACK_SYSTEM_PROMPT}\n\n{RUNTIME_SYSTEM_RULES}"
    assert "present_final_response" in system_message["content"]


def test_inline_fallback_prompt_matches_yaml() -> None:
    """The inline fallback must not drift from the versioned YAML system prompt.

    Production uses the inline copy until the DB row is pushed, so a silent drift
    here means the live agent runs different instructions than the reviewed YAML.
    """
    import re
    from pathlib import Path

    import yaml

    yaml_path = (
        Path(__file__).resolve().parents[4]
        / "aegis-prompts"
        / "agent"
        / "orchestrator.yaml"
    )
    if not yaml_path.exists():
        pytest.skip("orchestrator.yaml not present in this checkout")

    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    yaml_system = str(data["system_prompt"])

    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    assert _norm(FALLBACK_SYSTEM_PROMPT) == _norm(yaml_system)


def test_warm_system_prompt_is_nonfatal_and_caches_on_success(monkeypatch) -> None:
    """Startup preload never raises; it caches on a DB hit and falls back otherwise."""
    # Autouse fixture makes load_prompt_from_db raise -> fallback, no cache, no raise.
    assert warm_system_prompt() is False
    assert tool_agent_module._SYSTEM_PROMPT_CACHE is None

    def _ok_db(*_args, **_kwargs):
        return {"system_prompt": "DB ORCHESTRATOR PROMPT"}

    monkeypatch.setattr(tool_agent_module, "load_prompt_from_db", _ok_db)
    assert warm_system_prompt() is True
    assert tool_agent_module._SYSTEM_PROMPT_CACHE is not None


@pytest.mark.asyncio
async def test_agent_prompt_omits_default_filters_and_empty_optional_context(monkeypatch) -> None:
    """Default all-source UI state should not read as an explicit selection."""
    captured: dict[str, object] = {}

    async def fake_stream_with_tools(messages, _tools, _context, _llm_params):
        captured["messages"] = messages
        yield _content_chunk("Hi.")

    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.stream_with_tools", fake_stream_with_tools
    )

    await run_agent_step(
        normalize_turn(
            {
                "content": "hi",
                "filters": {"source_ids": list(SOURCE_IDS)},
                "optional_context": {
                    "bank_symbols": [],
                    "bank_categories": [],
                    "fiscal_years": [],
                    "quarters": [],
                },
                "context": {
                    "sources": list(SOURCE_IDS),
                    "model_mode": "small",
                    "search_mode": "quick",
                },
            }
        ),
        ConversationContext(),
        llm_context={"execution_id": "test"},
    )

    user_message = captured["messages"][1]  # type: ignore[index]
    payload = json.loads(user_message["content"])
    turn_payload = payload["turn"]
    assert turn_payload["user_message"] == "hi"
    assert "ui_selected_sources" not in turn_payload
    assert "explicit_source_filter" not in turn_payload
    assert "optional_context" not in turn_payload


@pytest.mark.asyncio
async def test_agent_prompt_includes_explicit_filter_and_optional_context(monkeypatch) -> None:
    """Only non-default UI selections are inserted into the agent prompt."""
    captured: dict[str, object] = {}

    async def fake_stream_with_tools(messages, _tools, _context, _llm_params):
        captured["messages"] = messages
        yield _content_chunk("ok")

    monkeypatch.setattr(
        "aegis_agent.v2.agent.tool_agent.stream_with_tools", fake_stream_with_tools
    )

    await run_agent_step(
        normalize_turn(
            {
                "content": "compare capital",
                "filters": {"source_ids": ["rts"]},
                "optional_context": {
                    "bank_symbols": ["RY-CA"],
                    "fiscal_years": [2026],
                    "quarters": ["Q1"],
                },
            }
        ),
        ConversationContext(),
        llm_context={"execution_id": "test"},
    )

    user_message = captured["messages"][1]  # type: ignore[index]
    turn_payload = json.loads(user_message["content"])["turn"]
    assert turn_payload["explicit_source_filter"] == ["rts"]
    assert turn_payload["optional_context"] == {
        "bank_symbols": ["RY-CA"],
        "fiscal_years": [2026],
        "quarters": ["Q1"],
    }
