"""V2 agent orchestration loop."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from html import escape
from typing import Any, AsyncIterator, Literal
from uuid import uuid4

from .agent.artifacts import deep_research_html, evidence_ids, quick_research_html
from .agent.conversation import ConversationContext
from .agent.deep import has_deep_scope, run_deep_research
from .agent.final_response import (
    build_final_shell,
    final_shell_marker,
    stream_synthesis,
)
from .agent.llm_context import build_llm_context
from .agent.models import EvidenceChunk, NormalizedTurn, normalize_turn
from .agent.retrieval import QUICK_SEARCH_CHUNK_LIMIT, retrieve_quick_evidence
from .agent.tool_agent import (
    AgentDecision,
    AgentToolCall,
    ClarificationOption,
    run_agent_step,
)
from .schemas import (
    Artifact,
    AvailabilityFilters,
    ChatMessagePayload,
    DataAvailabilityResponse,
    HtmlWidget,
    V2Event,
    WidgetAction,
)
from .sources import normalize_source_ids
from .tools.availability import availability_widget_html
from .tools.catalog import optional_context
from .tools.runtime import load_conversation_context


MAX_AGENT_STEPS = 3
AgentStepStatus = Literal["continue", "complete", "awaiting_user", "error"]


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
    llm_context: dict[str, Any] | None = None
    latest_availability: DataAvailabilityResponse | None = None
    latest_availability_widget_id: str | None = None


@dataclass
class AgentStepOutcome:
    """Mutable outcome for one bounded V2 agent step."""

    status: AgentStepStatus = "continue"
    reason: str = ""


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


def _missing_research_scope(turn: NormalizedTurn) -> list[str]:
    """Return scope fields needed before running source-backed research."""
    missing: list[str] = []
    if not turn.bank_symbols:
        missing.append("bank")
    if not turn.fiscal_years:
        missing.append("fiscal_year")
    if not turn.quarters:
        missing.append("quarter")
    return missing


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


def _string_list(value: Any) -> list[str]:
    """Return a deduplicated list of non-empty strings."""
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _int_list(value: Any) -> list[int]:
    """Return a deduplicated list of integer values."""
    result: list[int] = []
    for item in _string_list(value):
        try:
            parsed = int(item)
        except ValueError:
            continue
        if parsed not in result:
            result.append(parsed)
    return result


def _quarter_list(value: Any) -> list[str]:
    """Return normalized fiscal quarter labels."""
    quarters: list[str] = []
    for item in _string_list(value):
        quarter = item.upper()
        if quarter in {"1", "2", "3", "4"}:
            quarter = f"Q{quarter}"
        if quarter in {"Q1", "Q2", "Q3", "Q4"} and quarter not in quarters:
            quarters.append(quarter)
    return quarters


def _bank_symbols(value: Any) -> list[str]:
    """Return canonical-ish bank symbols, using the turn normalizer for aliases."""
    symbols: list[str] = []
    for item in _string_list(value):
        inferred = normalize_turn({"content": item}).bank_symbols
        candidates = inferred or [item.upper()]
        for candidate in candidates:
            if candidate not in symbols:
                symbols.append(candidate)
    return symbols


def _availability_turn_from_arguments(
    turn: NormalizedTurn, arguments: dict[str, Any]
) -> NormalizedTurn:
    """Apply agent-selected availability filters to the current turn."""
    return replace(
        turn,
        source_ids=normalize_source_ids(arguments.get("source_ids") or turn.source_ids),
        bank_symbols=_bank_symbols(arguments.get("bank_symbols")) or turn.bank_symbols,
        bank_categories=(
            _string_list(arguments.get("bank_categories")) or turn.bank_categories
        ),
        fiscal_years=_int_list(arguments.get("fiscal_years")) or turn.fiscal_years,
        quarters=_quarter_list(arguments.get("quarters")) or turn.quarters,
        keyword=str(arguments.get("keyword") or turn.keyword or "").strip() or None,
    )


def _research_turn_from_arguments(
    turn: NormalizedTurn, arguments: dict[str, Any]
) -> NormalizedTurn:
    """Apply agent-selected research scope to the current turn."""
    combinations = arguments.get("combinations")
    combos = combinations if isinstance(combinations, list) else []
    bank_values: list[Any] = []
    fiscal_year_values: list[Any] = []
    quarter_values: list[Any] = []
    for combo in combos:
        if not isinstance(combo, dict):
            continue
        bank_values.append(combo.get("bank_symbol"))
        fiscal_year_values.append(combo.get("fiscal_year"))
        quarter_values.append(combo.get("quarter"))
    source_ids = normalize_source_ids(arguments.get("source_ids") or turn.source_ids)
    search_mode = str(arguments.get("search_mode") or turn.search_mode).lower()
    return replace(
        turn,
        content=str(arguments.get("question") or turn.content).strip(),
        source_ids=source_ids,
        bank_symbols=_bank_symbols(bank_values) or turn.bank_symbols,
        fiscal_years=_int_list(fiscal_year_values) or turn.fiscal_years,
        quarters=_quarter_list(quarter_values) or turn.quarters,
        search_mode="deep" if search_mode == "deep" else "quick",
    )


def _clarification_options(arguments: dict[str, Any]) -> list[ClarificationOption]:
    """Return normalized agent-supplied clarification options."""
    options: list[ClarificationOption] = []
    raw_options = arguments.get("options") if isinstance(arguments, dict) else []
    if not isinstance(raw_options, list):
        return options
    for index, item in enumerate(raw_options, start=1):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        option_id = str(item.get("id") or f"option_{index}").strip()
        description = str(item.get("description") or "").strip() or None
        payload = item.get("payload")
        options.append(
            ClarificationOption(
                id=option_id or f"option_{index}",
                label=label,
                description=description,
                payload=payload if isinstance(payload, dict) else {},
            )
        )
    return options


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
        llm_context=state.llm_context,
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


async def _run_direct_response(
    state: V2SessionState, turn: NormalizedTurn, content: str
) -> AsyncIterator[dict[str, Any]]:
    """Emit a direct answer from the single agent."""
    stream_id = turn.run_uuid or f"stream_{uuid4().hex}"
    yield event(
        state.session_id,
        "tool.completed",
        {
            "tool_id": f"tool_{uuid4().hex}",
            "name": "agent_decision",
            "decision": "direct_response",
            "model_plan": turn.model_plan.__dict__ if turn.model_plan else {},
        },
    )
    yield event(
        state.session_id,
        "chat.delta",
        {"stream_id": stream_id, "role": "assistant", "content": content},
    )
    yield event(
        state.session_id,
        "chat.message",
        _message_payload("assistant", content, stream_id=stream_id, final=True),
    )


def _clarification_question(missing: list[str]) -> str:
    """Return a concise clarification question for missing research scope."""
    if missing == ["bank"]:
        return "Which bank should I use for this research?"
    if missing == ["fiscal_year"]:
        return "Which fiscal year should I use for this research?"
    if missing == ["quarter"]:
        return "Which quarter should I use for this research?"
    if missing == ["fiscal_year", "quarter"]:
        return "Which fiscal period should I use for this research?"
    if missing == ["bank", "fiscal_year", "quarter"]:
        return "Which bank and fiscal period should I use for this research?"
    return f"Please clarify the missing research scope: {', '.join(missing)}."


def _clarification_widget_html(question: str, missing: list[str]) -> str:
    """Return trusted HTML for a clarification widget."""
    missing_items = "".join(f"<li>{escape(item)}</li>" for item in missing)
    return (
        '<section class="clarification-widget">'
        f"<p>{escape(question)}</p>"
        "<small>Research will start after this scope is clear.</small>"
        f"<ul>{missing_items}</ul>"
        "</section>"
    )


def _clarification_actions(
    turn: NormalizedTurn, response: DataAvailabilityResponse | None
) -> list[WidgetAction]:
    """Build clickable clarification choices from live availability rows."""
    if response is None:
        return []
    actions: list[WidgetAction] = []
    seen: set[tuple[str, int, str]] = set()
    for row in response.rows:
        key = (row.bank_symbol, row.fiscal_year, row.quarter)
        if key in seen:
            continue
        seen.add(key)
        label = f"{row.bank_symbol} {row.quarter} {row.fiscal_year}"
        source_ids = turn.source_ids or row.source_ids
        actions.append(
            WidgetAction(
                id=f"clarify_{row.bank_symbol}_{row.fiscal_year}_{row.quarter}",
                label=label,
                action_type="clarification_reply",
                payload={
                    "reply": f"Use {label}",
                    "resend_query": turn.content,
                    "filters": {
                        "source_ids": source_ids,
                        "bank_symbols": [row.bank_symbol],
                        "fiscal_years": [row.fiscal_year],
                        "quarters": [row.quarter],
                    },
                },
            )
        )
        if len(actions) >= 6:
            break
    return actions


def _agent_clarification_actions(
    turn: NormalizedTurn, options: list[ClarificationOption]
) -> list[WidgetAction]:
    """Convert agent-suggested choices into clarification widget actions."""
    actions: list[WidgetAction] = []
    for option in options:
        payload = dict(option.payload)
        payload.setdefault("reply", option.label)
        payload.setdefault("resend_query", turn.content)
        actions.append(
            WidgetAction(
                id=f"clarify_{option.id}",
                label=option.label,
                action_type="clarification_reply",
                payload=payload,
            )
        )
    return actions


async def _run_plain_clarification_turn(
    state: V2SessionState,
    turn: NormalizedTurn,
    missing: list[str],
    question: str,
) -> AsyncIterator[dict[str, Any]]:
    """Ask a plain text clarification question without a widget."""
    yield event(
        state.session_id,
        "tool.completed",
        {
            "tool_id": f"tool_{uuid4().hex}",
            "name": "ask_clarification",
            "decision": "needs_clarification",
            "presentation": "message",
            "missing_scope": missing,
            "message": question,
            "model_plan": turn.model_plan.__dict__ if turn.model_plan else {},
        },
    )
    yield event(
        state.session_id,
        "chat.message",
        _message_payload("assistant", question),
    )


async def _run_clarification_turn(
    state: V2SessionState,
    turn: NormalizedTurn,
    missing: list[str],
    *,
    question: str | None = None,
    options: list[ClarificationOption] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Ask the user for missing research scope before running retrieval."""
    tool_id = f"tool_{uuid4().hex}"
    question = question or _clarification_question(missing)
    response: DataAvailabilityResponse | None = None
    try:
        response = await optional_context(_availability_filters_from_turn(turn))
    except Exception:
        response = None
    actions = _agent_clarification_actions(turn, options or [])
    if not actions:
        actions = _clarification_actions(turn, response)
    widget = HtmlWidget(
        kind="clarification",
        title="Clarification",
        status="complete",
        html=_clarification_widget_html(question, missing),
        data={"missing_scope": missing, "query": turn.content},
        actions=actions,
    )
    state.widgets[widget.id] = widget
    yield event(
        state.session_id,
        "tool.completed",
        {
            "tool_id": tool_id,
            "name": "ask_clarification",
            "decision": "needs_clarification",
            "presentation": "widget",
            "missing_scope": missing,
            "message": question,
            "model_plan": turn.model_plan.__dict__ if turn.model_plan else {},
        },
    )
    yield event(
        state.session_id,
        "widget.completed",
        {"widget": widget.model_dump(mode="json")},
    )


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
        result = await retrieve_quick_evidence(
            turn, limit=QUICK_SEARCH_CHUNK_LIMIT, llm_context=state.llm_context
        )
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
        research_result = await run_deep_research(turn, llm_context=state.llm_context)
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

    async for item in _run_agent_loop(state, turn):
        yield item


async def _run_agent_loop(
    state: V2SessionState, turn: NormalizedTurn
) -> AsyncIterator[dict[str, Any]]:
    """Run bounded V2 single-agent tool-choice steps for one user turn."""
    for step_index in range(MAX_AGENT_STEPS):
        if state.llm_context is None:
            try:
                state.llm_context = await build_llm_context(
                    turn.run_uuid or "v2-agent", "turn execution"
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                yield event(
                    state.session_id,
                    "tool.failed",
                    {
                        "tool_id": f"tool_{uuid4().hex}",
                        "name": "setup_llm_context",
                        "error": str(exc),
                        "agent_step": step_index,
                    },
                )
                yield event(
                    state.session_id,
                    "chat.message",
                    _message_payload(
                        "assistant", f"Aegis could not set up LLM access: {exc}"
                    ),
                )
                return

        try:
            decision = await run_agent_step(
                turn, state.conversation_context, state.llm_context
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            yield event(
                state.session_id,
                "tool.failed",
                {
                    "tool_id": f"tool_{uuid4().hex}",
                    "name": "agent_step",
                    "error": str(exc),
                    "agent_step": step_index,
                },
            )
            yield event(
                state.session_id,
                "chat.message",
                _message_payload(
                    "assistant", f"Aegis could not decide the next step: {exc}"
                ),
            )
            return

        outcome = AgentStepOutcome()
        async for item in _run_agent_decision(
            state, turn, decision, outcome=outcome, step_index=step_index
        ):
            yield item

        if outcome.status in {"complete", "awaiting_user", "error"}:
            return

    yield event(
        state.session_id,
        "tool.failed",
        {
            "tool_id": f"tool_{uuid4().hex}",
            "name": "agent_loop",
            "error": "Aegis reached its V2 agent loop limit before completing the turn.",
            "max_steps": MAX_AGENT_STEPS,
        },
    )
    yield event(
        state.session_id,
        "chat.message",
        _message_payload(
            "assistant",
            "Aegis reached its agent loop limit before completing the turn.",
        ),
    )


async def _run_agent_decision(
    state: V2SessionState,
    turn: NormalizedTurn,
    decision: AgentDecision,
    *,
    outcome: AgentStepOutcome,
    step_index: int,
) -> AsyncIterator[dict[str, Any]]:
    """Execute one single-agent direct answer or tool call."""
    if decision.kind == "direct":
        async for item in _run_direct_response(state, turn, decision.content):
            yield item
        outcome.status = "complete"
        outcome.reason = "direct_response_completed"
        return

    if decision.tool_call is None:
        yield event(
            state.session_id,
            "tool.failed",
            {
                "tool_id": f"tool_{uuid4().hex}",
                "name": "agent_step",
                "error": "Aegis returned a tool decision without a tool call.",
                "agent_step": step_index,
            },
        )
        outcome.status = "error"
        outcome.reason = "missing_tool_call"
        return

    async for item in _run_agent_tool_call(
        state,
        turn,
        decision.tool_call,
        outcome=outcome,
        step_index=step_index,
    ):
        yield item


async def _run_agent_tool_call(
    state: V2SessionState,
    turn: NormalizedTurn,
    tool_call: AgentToolCall,
    *,
    outcome: AgentStepOutcome,
    step_index: int,
) -> AsyncIterator[dict[str, Any]]:
    """Execute one tool selected by the single agent."""
    arguments = tool_call.arguments

    if tool_call.name == "check_data_availability":
        availability_turn = _availability_turn_from_arguments(turn, arguments)
        async for item in _run_availability_turn(state, availability_turn):
            yield item
        outcome.status = "complete"
        outcome.reason = "availability_completed"
        return

    if tool_call.name == "ask_clarification":
        question = str(arguments.get("question") or "").strip()
        missing = _string_list(arguments.get("missing_scope")) or ["research_question"]
        options = _clarification_options(arguments)
        if str(arguments.get("presentation") or "message") == "widget" or options:
            async for item in _run_clarification_turn(
                state,
                turn,
                missing,
                question=question or None,
                options=options,
            ):
                yield item
        else:
            async for item in _run_plain_clarification_turn(
                state,
                turn,
                missing,
                question or _clarification_question(missing),
            ):
                yield item
        outcome.status = "awaiting_user"
        outcome.reason = "clarification_requested"
        return

    if tool_call.name == "run_research":
        research_turn = _research_turn_from_arguments(turn, arguments)
        missing = _missing_research_scope(research_turn)
        if not research_turn.content:
            missing.append("research_question")
        if missing:
            async for item in _run_clarification_turn(
                state,
                research_turn,
                missing,
                question=None,
                options=[],
            ):
                yield item
            outcome.status = "awaiting_user"
            outcome.reason = "research_scope_missing"
            return

        if research_turn.search_mode == "deep":
            async for item in _run_deep_research_turn(state, research_turn):
                yield item
        else:
            async for item in _run_quick_research_turn(state, research_turn):
                yield item
        outcome.status = "complete"
        outcome.reason = "research_completed"
        return

    yield event(
        state.session_id,
        "tool.failed",
        {
            "tool_id": f"tool_{uuid4().hex}",
            "name": "agent_step",
            "error": f"Unsupported agent tool call: {tool_call.name}",
            "agent_step": step_index,
        },
    )
    outcome.status = "error"
    outcome.reason = "unsupported_tool"
