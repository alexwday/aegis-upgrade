"""Tests for V2 conversation context handling."""

from __future__ import annotations

import pytest

from aegis_agent.v2.agent.conversation import (
    WIDGET_MARKER_CLOSE,
    WIDGET_MARKER_OPEN,
    build_conversation_context,
)
from aegis_agent.v2.agent.final_response import (
    build_final_shell,
    final_shell_marker,
    stream_synthesis,
)
from aegis_agent.v2.agent.models import normalize_turn
from aegis_agent.v2.schemas import Artifact, ChatMessageRecord, HtmlWidget


def test_build_conversation_context_parses_runtime_records() -> None:
    """Prior messages, final shells, widgets, and artifacts should become context."""
    turn = normalize_turn({"content": "What can you do?"})
    shell = build_final_shell(turn, mode="general")
    widget = HtmlWidget(
        id="widget-1",
        kind="data_availability",
        title="Data Availability",
        status="complete",
        data={"rows": [{"bank_symbol": "RY"}], "missing": []},
    )
    messages = [
        ChatMessageRecord(id="m1", role="user", content="What can you do?"),
        ChatMessageRecord(
            id="m2",
            role="assistant",
            content=final_shell_marker(shell) + "I can run quick research.",
        ),
        ChatMessageRecord(
            id="m3",
            role="tool",
            content=(
                f"{WIDGET_MARKER_OPEN}"
                f"{widget.model_dump_json()}"
                f"{WIDGET_MARKER_CLOSE}"
            ),
        ),
        ChatMessageRecord(id="m4", role="user", content="summarize that"),
    ]
    artifacts = [
        Artifact(
            id="artifact-1",
            session_id="conversation-1",
            kind="quick_search",
            title="Quick search - CET1",
            html="<html></html>",
            evidence_ids=["rts:E1"],
        )
    ]

    context = build_conversation_context(
        messages=messages,
        artifacts=artifacts,
        current_user_content="summarize that",
    )
    prompt_text = context.to_prompt_text()

    assert len(context.messages) == 2
    assert context.final_responses[0].headline == "Aegis assistant"
    assert context.widgets[0].summary == "1 row(s), 0 missing coverage item(s)"
    assert context.artifacts[0].title == "Quick search - CET1"
    assert "summarize that" not in prompt_text
    assert "Quick search - CET1" in prompt_text


@pytest.mark.asyncio
async def test_stream_synthesis_includes_prior_context(monkeypatch) -> None:
    """General synthesis should pass prior context into the LLM prompt."""
    captured: dict[str, str] = {}

    async def fake_stream(messages, _context, _overrides):
        captured["system"] = messages[0]["content"]
        captured["user"] = messages[1]["content"]
        yield {"choices": [{"delta": {"content": "done"}}]}

    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setattr("aegis_agent.v2.agent.final_response.stream", fake_stream)
    context = build_conversation_context(
        messages=[
            ChatMessageRecord(id="m1", role="user", content="First question"),
            ChatMessageRecord(id="m2", role="assistant", content="Prior answer"),
        ],
        artifacts=[
            Artifact(
                id="artifact-1",
                session_id="conversation-1",
                kind="deep_search",
                title="Deep search - PCL",
                html="<html></html>",
            )
        ],
    )
    turn = normalize_turn({"content": "summarize the prior work"})

    output = [
        item
        async for item in stream_synthesis(
            turn,
            mode="general",
            conversation_context=context,
        )
    ]

    assert output == ["done"]
    assert "Prior conversation context follows" in captured["user"]
    assert "Deep search - PCL" in captured["user"]
    assert "Use prior conversation context" in captured["system"]
