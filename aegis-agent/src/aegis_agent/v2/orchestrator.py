"""V2 agent orchestration loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Any, AsyncIterator
from uuid import uuid4

from .agent.artifacts import deep_research_html, evidence_ids, quick_research_html
from .agent.conversation import ConversationContext
from .agent.deep import has_deep_scope, run_deep_research
from .agent.final_response import (
    build_final_shell,
    final_shell_marker,
    stream_synthesis,
)
from .agent.models import EvidenceChunk, NormalizedTurn, normalize_turn
from .agent.retrieval import QUICK_SEARCH_CHUNK_LIMIT, retrieve_quick_evidence
from .schemas import (
    Artifact,
    AvailabilityFilters,
    ChatMessagePayload,
    DataAvailabilityResponse,
    HtmlWidget,
    V2Event,
    WidgetAction,
)
from .tools.availability import availability_widget_html
from .tools.catalog import optional_context
from .tools.runtime import load_conversation_context


@dataclass
class V2SessionState:
    """State scoped to one V2 websocket connection."""

    session_id: str = field(default_factory=lambda: f"session_{uuid4().hex}")
    user_id: str | None = None
    conversation_id: str | None = None
    run_uuid: str | None = None
    widgets: dict[str, HtmlWidget] = field(default_factory=dict)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    conversation_context: ConversationContext = field(
        default_factory=ConversationContext
    )
    latest_availability: DataAvailabilityResponse | None = None
    latest_availability_widget_id: str | None = None


def event(
    session_id: str, event_type: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a JSON-serializable V2 event envelope."""
    envelope = V2Event(type=event_type, session_id=session_id, payload=payload or {})
    return envelope.model_dump(mode="json")


def _message_payload(role: str, content: str, **extra: Any) -> dict[str, Any]:
    """Return a chat message payload while allowing V2 transition metadata."""
    payload = ChatMessagePayload(role=role, content=content).model_dump(mode="json")
    payload.update(extra)
    return payload


def _is_availability_request(message: str) -> bool:
    """Return whether a user message should run the availability tool."""
    normalized = message.lower()
    triggers = (
        "available",
        "availability",
        "coverage",
        "data do we have",
        "what data",
        "which data",
    )
    return any(trigger in normalized for trigger in triggers)


def _is_general_request(message: str) -> bool:
    """Return whether the turn can be answered without source retrieval."""
    normalized = message.lower().strip()
    triggers = (
        "what can you do",
        "how do you work",
        "what is aegis",
        "help",
        "hello",
        "hi",
    )
    return any(trigger in normalized for trigger in triggers)


def _is_context_follow_up(
    message: str, conversation_context: ConversationContext
) -> bool:
    """Return whether the turn is asking about prior conversation state."""
    if not conversation_context.has_context:
        return False
    normalized = message.lower().strip()
    exact_triggers = {
        "summarize that",
        "summarise that",
        "explain that",
        "expand on that",
        "what about that",
        "tell me more about that",
    }
    if normalized in exact_triggers:
        return True
    phrase_triggers = (
        "previous answer",
        "previous response",
        "prior answer",
        "prior response",
        "last answer",
        "last response",
        "last artifact",
        "latest artifact",
        "previous artifact",
        "prior artifact",
        "that artifact",
        "the artifact",
        "earlier",
        "above",
        "recap",
    )
    return any(trigger in normalized for trigger in phrase_triggers)


def _availability_filters_from_turn(turn: NormalizedTurn) -> AvailabilityFilters:
    """Build availability filters from a normalized turn."""
    return AvailabilityFilters(
        source_ids=turn.source_ids,
        bank_symbols=turn.bank_symbols,
        bank_categories=turn.bank_categories,
        fiscal_years=turn.fiscal_years,
        quarters=turn.quarters,
        keyword=turn.keyword,
        limit=500,
    )


def _availability_actions(response: DataAvailabilityResponse) -> list[WidgetAction]:
    """Return structured actions for coverage rows."""
    actions: list[WidgetAction] = []
    seen: set[tuple[str, int, str]] = set()
    for row in response.rows[:24]:
        key = (row.bank_symbol, row.fiscal_year, row.quarter)
        if key in seen:
            continue
        seen.add(key)
        actions.append(
            WidgetAction(
                id=f"open_documents_{row.bank_symbol}_{row.fiscal_year}_{row.quarter}",
                label=f"Open {row.bank_symbol} {row.quarter} {row.fiscal_year} documents",
                action_type="filter_documents",
                payload={
                    "bank_symbols": [row.bank_symbol],
                    "fiscal_years": [row.fiscal_year],
                    "quarters": [row.quarter],
                    "source_ids": row.source_ids,
                },
            )
        )
    return actions


async def _run_availability_turn(
    state: V2SessionState,
    turn: NormalizedTurn,
) -> AsyncIterator[dict[str, Any]]:
    """Run the catalog-backed V2 availability widget workflow."""
    tool_id = f"tool_{uuid4().hex}"
    widget = HtmlWidget(
        kind="data_availability",
        title="Data Availability",
        status="running",
        html="<p>Checking source coverage...</p>",
    )
    state.widgets[widget.id] = widget
    state.latest_availability_widget_id = widget.id

    yield event(
        state.session_id,
        "tool.started",
        {"tool_id": tool_id, "name": "check_data_availability", "widget_id": widget.id},
    )
    yield event(
        state.session_id, "widget.created", {"widget": widget.model_dump(mode="json")}
    )

    try:
        response = await optional_context(_availability_filters_from_turn(turn))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        now = datetime.now(timezone.utc)
        widget.status = "failed"
        widget.html = f"<p>Availability check failed: {escape(str(exc))}</p>"
        widget.updated_at = now
        yield event(
            state.session_id,
            "tool.failed",
            {"tool_id": tool_id, "name": "check_data_availability", "error": str(exc)},
        )
        yield event(
            state.session_id,
            "widget.failed",
            {"widget": widget.model_dump(mode="json")},
        )
        yield event(
            state.session_id,
            "chat.message",
            _message_payload("assistant", f"I could not check availability: {exc}"),
        )
        return

    now = datetime.now(timezone.utc)
    widget.status = "complete"
    widget.html = availability_widget_html(response)
    widget.data = response.model_dump(mode="json")
    widget.actions = _availability_actions(response)
    widget.updated_at = now
    state.widgets[widget.id] = widget
    state.latest_availability = response

    yield event(
        state.session_id,
        "tool.completed",
        {
            "tool_id": tool_id,
            "name": "check_data_availability",
            "row_count": len(response.rows),
            "missing_count": len(response.missing),
        },
    )
    yield event(
        state.session_id, "widget.completed", {"widget": widget.model_dump(mode="json")}
    )
    yield event(
        state.session_id,
        "chat.message",
        _message_payload(
            "assistant",
            f"I found {len(response.rows)} available bank-period rows. "
            "Use the availability widget in the chat to inspect coverage.",
        ),
    )


def _artifact(
    state: V2SessionState,
    *,
    kind: str,
    title: str,
    html: str,
    chunks: list[EvidenceChunk] | None = None,
) -> Artifact:
    """Build a V2 artifact envelope."""
    return Artifact(
        session_id=state.conversation_id or state.session_id,
        kind=kind,
        title=title,
        html=html,
        evidence_ids=evidence_ids(chunks or []),
    )


async def _stream_final_response(
    state: V2SessionState,
    turn: NormalizedTurn,
    *,
    mode: str,
    chunks: list[EvidenceChunk] | None = None,
    research_result: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Emit final shell, deltas, and one persisted final chat message."""
    stream_id = turn.run_uuid or f"stream_{uuid4().hex}"
    shell = build_final_shell(
        turn, mode=mode, chunks=chunks, research_result=research_result
    )
    shell_json = shell.model_dump(mode="json")
    yield event(
        state.session_id,
        "final_response.started",
        {"stream_id": stream_id, "shell": shell_json},
    )

    body_parts: list[str] = []
    async for delta in stream_synthesis(
        turn,
        mode=mode,
        chunks=chunks,
        research_result=research_result,
        conversation_context=state.conversation_context,
    ):
        if not delta:
            continue
        body_parts.append(delta)
        yield event(
            state.session_id,
            "chat.delta",
            {"stream_id": stream_id, "role": "assistant", "content": delta},
        )

    body = "".join(body_parts).strip()
    persisted_content = final_shell_marker(shell) + body
    yield event(
        state.session_id,
        "chat.message",
        _message_payload(
            "assistant", persisted_content, stream_id=stream_id, final=True
        ),
    )


async def _run_general_turn(
    state: V2SessionState, turn: NormalizedTurn
) -> AsyncIterator[dict[str, Any]]:
    """Answer a non-research user turn."""
    yield event(
        state.session_id,
        "tool.completed",
        {
            "tool_id": f"tool_{uuid4().hex}",
            "name": "classify_turn",
            "decision": "general_conversation",
            "model_plan": turn.model_plan.__dict__ if turn.model_plan else {},
        },
    )
    async for item in _stream_final_response(state, turn, mode="general"):
        yield item


async def _run_quick_research_turn(
    state: V2SessionState, turn: NormalizedTurn
) -> AsyncIterator[dict[str, Any]]:
    """Run quick evidence retrieval, create an artifact, then stream synthesis."""
    tool_id = f"tool_{uuid4().hex}"
    yield event(
        state.session_id,
        "tool.started",
        {
            "tool_id": tool_id,
            "name": "quick_research",
            "chunk_limit": QUICK_SEARCH_CHUNK_LIMIT,
            "sources": turn.source_ids,
            "model_plan": turn.model_plan.__dict__ if turn.model_plan else {},
        },
    )
    try:
        result = await retrieve_quick_evidence(turn, limit=QUICK_SEARCH_CHUNK_LIMIT)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        yield event(
            state.session_id,
            "tool.failed",
            {"tool_id": tool_id, "name": "quick_research", "error": str(exc)},
        )
        yield event(
            state.session_id,
            "chat.message",
            _message_payload("assistant", f"Quick search failed: {exc}"),
        )
        return
    yield event(
        state.session_id,
        "tool.progress",
        {
            "tool_id": tool_id,
            "name": "quick_research",
            "message": f"Retained {len(result.chunks)} chunk(s) across {len(turn.source_ids)} selected source(s).",
            "gaps": result.gaps,
        },
    )
    html = quick_research_html(turn, result.chunks, result.gaps)
    artifact = _artifact(
        state,
        kind="quick_search",
        title=f"Quick search - {turn.content[:72]}",
        html=html,
        chunks=result.chunks,
    )
    state.artifacts[artifact.id] = artifact
    yield event(
        state.session_id,
        "artifact.created",
        {"artifact": artifact.model_dump(mode="json")},
    )
    yield event(
        state.session_id,
        "tool.completed",
        {
            "tool_id": tool_id,
            "name": "quick_research",
            "chunk_count": len(result.chunks),
            "gap_count": len(result.gaps),
            "artifact_id": artifact.id,
        },
    )
    try:
        async for item in _stream_final_response(
            state, turn, mode="quick", chunks=result.chunks
        ):
            yield item
    except Exception as exc:  # pylint: disable=broad-exception-caught
        yield event(
            state.session_id,
            "tool.failed",
            {
                "tool_id": tool_id,
                "name": "quick_research_response",
                "error": str(exc),
            },
        )
        yield event(
            state.session_id,
            "chat.message",
            _message_payload("assistant", f"Quick search response failed: {exc}"),
        )


async def _run_deep_research_turn(
    state: V2SessionState, turn: NormalizedTurn
) -> AsyncIterator[dict[str, Any]]:
    """Run scoped deep research, create an artifact, then stream synthesis."""
    if not has_deep_scope(turn):
        yield event(
            state.session_id,
            "chat.message",
            _message_payload(
                "assistant",
                "Deep search needs a selected bank, fiscal year, and quarter. "
                "Set those in optional context, then send the query again.",
            ),
        )
        return

    tool_id = f"tool_{uuid4().hex}"
    yield event(
        state.session_id,
        "tool.started",
        {
            "tool_id": tool_id,
            "name": "deep_research",
            "sources": turn.source_ids,
            "model_plan": turn.model_plan.__dict__ if turn.model_plan else {},
        },
    )
    try:
        research_result = await run_deep_research(turn)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        yield event(
            state.session_id,
            "tool.failed",
            {"tool_id": tool_id, "name": "deep_research", "error": str(exc)},
        )
        yield event(
            state.session_id,
            "chat.message",
            _message_payload("assistant", f"Deep search failed: {exc}"),
        )
        return

    gaps: list[str] = []
    for gap in (
        research_result.get("gaps", [])
        if isinstance(research_result.get("gaps"), list)
        else []
    ):
        if isinstance(gap, dict):
            reason = str(gap.get("reason") or "")
            if reason:
                gaps.append(reason)

    html = deep_research_html(turn, research_result, [], gaps)
    artifact = _artifact(
        state,
        kind="deep_search",
        title=f"Deep search - {turn.content[:72]}",
        html=html,
        chunks=[],
    )
    state.artifacts[artifact.id] = artifact
    yield event(
        state.session_id,
        "artifact.created",
        {"artifact": artifact.model_dump(mode="json")},
    )
    yield event(
        state.session_id,
        "tool.completed",
        {
            "tool_id": tool_id,
            "name": "deep_research",
            "status": research_result.get("status"),
            "finding_count": len(research_result.get("findings") or []),
            "artifact_id": artifact.id,
        },
    )
    try:
        async for item in _stream_final_response(
            state,
            turn,
            mode="deep",
            chunks=[],
            research_result=research_result,
        ):
            yield item
    except Exception as exc:  # pylint: disable=broad-exception-caught
        yield event(
            state.session_id,
            "tool.failed",
            {
                "tool_id": tool_id,
                "name": "deep_research_response",
                "error": str(exc),
            },
        )
        yield event(
            state.session_id,
            "chat.message",
            _message_payload("assistant", f"Deep search response failed: {exc}"),
        )


async def run_turn(
    payload: dict[str, Any], state: V2SessionState
) -> AsyncIterator[dict[str, Any]]:
    """Run one V2 chat turn and stream typed UI events."""
    turn = normalize_turn(payload)
    state.conversation_context = await load_conversation_context(
        turn.conversation_id,
        current_user_content=turn.content,
    )
    if not turn.content:
        yield event(
            state.session_id,
            "chat.message",
            _message_payload("assistant", "Send a question to start."),
        )
        return

    if _is_availability_request(turn.content):
        async for item in _run_availability_turn(state, turn):
            yield item
        return

    if _is_general_request(turn.content) or _is_context_follow_up(
        turn.content, state.conversation_context
    ):
        async for item in _run_general_turn(state, turn):
            yield item
        return

    if turn.search_mode == "deep":
        async for item in _run_deep_research_turn(state, turn):
            yield item
        return

    async for item in _run_quick_research_turn(state, turn):
        yield item
