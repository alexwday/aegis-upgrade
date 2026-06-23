"""V2 agent orchestration loop."""

from __future__ import annotations

import asyncio
import json
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
from .agent.models import (
    EvidenceChunk,
    FinalResponseShell,
    FinalResponseSummary,
    FinalResponseTile,
    NormalizedTurn,
    evidence_id_for_chunk,
    normalize_turn,
)
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


MAX_AGENT_STEPS = 6
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
    # Transient per-turn scratch, reset at the start of each agent loop.
    turn_scratchpad: list[dict[str, Any]] = field(default_factory=list)
    turn_answer_mode: str | None = None
    turn_answer_chunks: list[EvidenceChunk] = field(default_factory=list)
    turn_answer_research: dict[str, Any] | None = None
    turn_tool_feedback: dict[str, Any] | None = None
    turn_disposition: AgentStepStatus = "continue"
    # Per-step streaming state for the live answer body.
    turn_stream_id: str = ""
    turn_shell: Any | None = None
    turn_shell_emitted: bool = False
    turn_streamed_any: bool = False
    turn_streamed_text: str = ""


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


def _reset_turn_state(state: V2SessionState, turn: NormalizedTurn) -> None:
    """Reset transient answer/tool state for one user turn."""
    state.turn_scratchpad = []
    state.turn_answer_mode = None
    state.turn_answer_chunks = []
    state.turn_answer_research = None
    state.turn_tool_feedback = None
    state.turn_disposition = "continue"
    state.turn_stream_id = turn.run_uuid or f"stream_{uuid4().hex}"
    state.turn_shell = None
    state.turn_shell_emitted = False
    state.turn_streamed_any = False
    state.turn_streamed_text = ""


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
    """Run the catalog-backed V2 availability widget workflow.

    Non-terminal: stores a compact coverage summary on the session so the same
    agent reads it back and authors the reply in its own voice (F7). Only a hard
    availability failure terminates the turn.
    """
    state.turn_disposition = "continue"
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
        state.turn_disposition = "error"
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
    # Feed the coverage back instead of emitting a templated message; the same
    # agent authors the reply on the next loop step (F7).
    state.turn_tool_feedback = _availability_feedback(response)


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
    existing_shell: Any | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Synthesis-based final response, wired as the fallback path.

    The live path lets the single agent author the research answer in its own
    voice, streamed token-by-token (see ``_emit_answer_delta`` / ``_finish_answer``).
    This separate-synthesis renderer is invoked only when the agent step fails or
    yields no usable body after research has run (see ``_handle_step_failure``).

    When ``existing_shell`` is provided the shell has already been emitted this
    turn (``present_final_response`` succeeded, then the body step failed): the
    renderer reuses it and the open ``turn_stream_id`` instead of emitting a
    second ``final_response.started``, so the open answer stream is closed rather
    than orphaned.
    """
    if existing_shell is not None:
        stream_id = state.turn_stream_id
        shell = existing_shell
    else:
        stream_id = turn.run_uuid or f"stream_{uuid4().hex}"
        shell = build_final_shell(
            turn, mode=mode, chunks=chunks, research_result=research_result
        )
        yield event(
            state.session_id,
            "final_response.started",
            {"stream_id": stream_id, "shell": shell.model_dump(mode="json")},
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


def _assistant_tool_message(tool_call: AgentToolCall, call_id: str) -> dict[str, Any]:
    """Reconstruct the assistant tool-call message for the agent scratchpad."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.arguments, default=str),
                },
            }
        ],
    }


def _tool_result_message(call_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    """Build the tool-result message fed back to the agent loop."""
    content = json.dumps(payload or {}, ensure_ascii=False, default=str)
    return {"role": "tool", "tool_call_id": call_id, "content": content[:14000]}


def _quick_evidence_feedback(
    chunks: list[EvidenceChunk], gaps: list[str]
) -> dict[str, Any]:
    """Build a compact, citation-ready quick-evidence payload for the agent."""
    evidence: list[dict[str, Any]] = []
    for chunk in chunks[:24]:
        location = (
            f"p.{chunk.page_number}" if chunk.page_number else chunk.sheet_name
        ) or chunk.section_name
        period = " ".join(
            part for part in [chunk.quarter or "", str(chunk.fiscal_year or "")] if part
        ).strip()
        evidence.append(
            {
                "evidence_id": evidence_id_for_chunk(chunk),
                "source": chunk.source_display_name,
                "bank": chunk.bank_ticker,
                "period": period or None,
                "location": location,
                "text": " ".join(chunk.chunk_content.split())[:800],
            }
        )
    return {
        "mode": "quick",
        "retained_chunks": len(chunks),
        "gaps": gaps,
        "evidence": evidence,
    }


def _deep_research_feedback(research_result: dict[str, Any]) -> dict[str, Any]:
    """Build a compact deep-research payload for the agent to author from."""
    findings = research_result.get("findings")
    gaps = research_result.get("gaps")
    return {
        "mode": "deep",
        "status": research_result.get("status"),
        "quick_summary": research_result.get("quick_summary"),
        "findings": findings[:24] if isinstance(findings, list) else [],
        "gaps": gaps[:24] if isinstance(gaps, list) else [],
    }


def _availability_feedback(response: DataAvailabilityResponse) -> dict[str, Any]:
    """Build a compact coverage summary for the agent to author a reply from."""
    coverage = [
        {
            "bank": row.bank_symbol,
            "fiscal_year": row.fiscal_year,
            "quarter": row.quarter,
            "sources": row.source_ids,
        }
        for row in response.rows[:40]
    ]
    missing = [
        {
            "bank": gap.bank_symbol,
            "fiscal_year": gap.fiscal_year,
            "quarter": gap.quarter,
            "missing_sources": gap.missing_source_ids,
        }
        for gap in response.missing[:40]
    ]
    return {
        "tool": "check_data_availability",
        "row_count": len(response.rows),
        "missing_count": len(response.missing),
        "coverage": coverage,
        "missing": missing,
    }


def _accumulate_chunks(
    existing: list[EvidenceChunk], new_chunks: list[EvidenceChunk]
) -> list[EvidenceChunk]:
    """Append new evidence chunks to the turn's set, de-duplicated by evidence id.

    Research is non-terminal, so the agent may call ``run_research`` more than
    once in a turn. Accumulating (rather than overwriting) keeps every retrieved
    chunk visible to citation validation and the shell, so citations to an
    earlier research call are not dropped as hallucinations.
    """
    seen = {evidence_id_for_chunk(chunk) for chunk in existing}
    merged = list(existing)
    for chunk in new_chunks:
        chunk_id = evidence_id_for_chunk(chunk)
        if chunk_id not in seen:
            seen.add(chunk_id)
            merged.append(chunk)
    return merged


def _known_evidence_ids(state: V2SessionState) -> set[str]:
    """Return the stable evidence ids retrieved this turn for citation validation."""
    ids: set[str] = {
        evidence_id_for_chunk(chunk) for chunk in state.turn_answer_chunks
    }
    research = state.turn_answer_research or {}
    findings = research.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            refs = finding.get("evidence_refs")
            if not isinstance(refs, list):
                continue
            for ref in refs:
                if isinstance(ref, dict):
                    evidence_id = str(ref.get("evidence_id") or "").strip()
                    if evidence_id:
                        ids.add(evidence_id)
    return ids


def _shell_from_present_arguments(
    state: V2SessionState, turn: NormalizedTurn, arguments: dict[str, Any]
) -> FinalResponseShell:
    """Build a validated final shell from the agent's present_final_response call.

    Tile evidence ids are filtered against the ids actually retrieved this turn, so
    hallucinated citations are dropped while the agent-authored values are kept.
    """
    known_ids = _known_evidence_ids(state)
    raw_tiles = arguments.get("tiles") if isinstance(arguments.get("tiles"), list) else []
    tiles: list[FinalResponseTile] = []
    for raw_tile in raw_tiles:
        if not isinstance(raw_tile, dict):
            continue
        label = str(raw_tile.get("label") or "").strip()
        value = str(raw_tile.get("value") or "").strip()
        if not label or not value:
            continue
        context = str(raw_tile.get("context") or "").strip() or None
        # Renamed from ``evidence_ids`` so it no longer shadows the imported
        # ``evidence_ids`` helper (F11).
        raw_evidence_ids = _string_list(raw_tile.get("evidence_ids"))
        valid_evidence_ids = [
            evidence_id for evidence_id in raw_evidence_ids if evidence_id in known_ids
        ]
        # A tile that cited evidence but whose citations were all hallucinated is
        # very likely a fabricated metric: drop it rather than show an unbacked
        # value. Tiles that cited nothing are kept as agent-authored (F9).
        if raw_evidence_ids and not valid_evidence_ids:
            continue
        tiles.append(
            FinalResponseTile(
                label=label[:40],
                value=value[:40],
                context=context,
                evidence_ids=valid_evidence_ids,
            )
        )
        if len(tiles) >= 4:
            break

    headline = str(arguments.get("headline") or "").strip() or "Aegis research brief"
    dek = str(arguments.get("dek") or "").strip() or turn.content[:180] or None
    return FinalResponseShell(
        render_mode="custom",
        summary=FinalResponseSummary(
            headline=headline, dek=dek, eyebrow="Aegis research brief"
        ),
        tiles=tiles,
        body_style="user_requested_format",
    )


async def _open_answer_stream(
    state: V2SessionState, turn: NormalizedTurn
) -> AsyncIterator[dict[str, Any]]:
    """Emit the answer header once, before the first streamed body token.

    Research-backed answers open with a ``final_response.started`` shell; general
    answers open with a ``tool.completed`` agent-decision marker, matching the V1
    direct-response contract.

    Note (F8): the ``build_final_shell`` call below is the *deterministic*
    (regex-derived) shell. It is reached only when the agent went research ->
    direct answer without calling ``present_final_response`` (the agent path sets
    ``state.turn_shell`` directly in the present handler). So the regex shell is a
    fallback, but a reachable one whenever the agent skips ``present`` -- not
    strictly fallback-only as Phase 3 claimed. Whether to force ``present`` via
    ``tool_choice`` is a tuning decision to make with live-model data.
    """
    if state.turn_answer_mode in {"quick", "deep"}:
        state.turn_shell = build_final_shell(
            turn,
            mode=state.turn_answer_mode,
            chunks=state.turn_answer_chunks,
            research_result=state.turn_answer_research,
        )
        yield event(
            state.session_id,
            "final_response.started",
            {
                "stream_id": state.turn_stream_id,
                "shell": state.turn_shell.model_dump(mode="json"),
            },
        )
    else:
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
    state.turn_shell_emitted = True


async def _emit_answer_delta(
    state: V2SessionState, turn: NormalizedTurn, text: str
) -> AsyncIterator[dict[str, Any]]:
    """Stream one body token live, opening the answer header on the first token."""
    if not state.turn_shell_emitted:
        async for item in _open_answer_stream(state, turn):
            yield item
    state.turn_streamed_any = True
    state.turn_streamed_text += text
    yield event(
        state.session_id,
        "chat.delta",
        {"stream_id": state.turn_stream_id, "role": "assistant", "content": text},
    )


async def _finish_answer(
    state: V2SessionState, turn: NormalizedTurn, content: str
) -> AsyncIterator[dict[str, Any]]:
    """Persist the agent's final message after the body has streamed."""
    body = content.strip()
    research_backed = state.turn_answer_mode in {"quick", "deep"}

    if not body and research_backed:
        # The agent produced no usable body: fall back to synthesis rendering.
        async for item in _stream_final_response(
            state,
            turn,
            mode=state.turn_answer_mode,
            chunks=state.turn_answer_chunks,
            research_result=state.turn_answer_research,
        ):
            yield item
        return

    # If nothing streamed live (e.g. a non-streaming agent step), emit the body now.
    if not state.turn_streamed_any:
        async for item in _emit_answer_delta(state, turn, body):
            yield item

    if state.turn_shell is not None:
        persisted_content = final_shell_marker(state.turn_shell) + body
    else:
        persisted_content = body
    yield event(
        state.session_id,
        "chat.message",
        _message_payload(
            "assistant", persisted_content, stream_id=state.turn_stream_id, final=True
        ),
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
    for action in actions:
        action.payload["question"] = question
    widget = HtmlWidget(
        kind="clarification",
        title="Clarification",
        status="complete",
        html=_clarification_widget_html(question, missing),
        data={"missing_scope": missing, "query": turn.content, "question": question},
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
    """Retrieve quick evidence and create an artifact; the agent authors the answer.

    Stores the retained evidence on the session so the single agent can read it
    back and write the final answer in its own voice. Synthesis is no longer a
    separate LLM call; this helper only retrieves and reports.
    """
    state.turn_disposition = "continue"
    missing = _missing_research_scope(turn)
    if missing:
        async for item in _run_clarification_turn(
            state,
            turn,
            missing,
            question=None,
            options=[],
        ):
            yield item
        state.turn_disposition = "awaiting_user"
        return

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
        state.turn_disposition = "error"
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
    # Accumulate across research calls; do not downgrade a deep turn to quick and
    # do not wipe a prior deep result (F5).
    state.turn_answer_mode = state.turn_answer_mode or "quick"
    state.turn_answer_chunks = _accumulate_chunks(state.turn_answer_chunks, result.chunks)
    state.turn_tool_feedback = _quick_evidence_feedback(result.chunks, result.gaps)


async def _run_deep_research_turn(
    state: V2SessionState, turn: NormalizedTurn
) -> AsyncIterator[dict[str, Any]]:
    """Run scoped deep research and create an artifact; the agent authors the answer."""
    state.turn_disposition = "continue"
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
        state.turn_disposition = "complete"
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
        state.turn_disposition = "error"
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
    # Deep presentation wins for the turn; keep any accumulated quick chunks so
    # their citations stay valid (F5).
    state.turn_answer_mode = "deep"
    state.turn_answer_research = research_result
    state.turn_tool_feedback = _deep_research_feedback(research_result)


async def run_turn(
    payload: dict[str, Any], state: V2SessionState
) -> AsyncIterator[dict[str, Any]]:
    """Run one V2 chat turn and stream typed UI events."""
    turn = normalize_turn(payload)
    visible_user_content = str(payload.get("content") or turn.content)
    state.conversation_context = await load_conversation_context(
        turn.conversation_id,
        current_user_content=visible_user_content,
    )
    if not turn.content:
        yield event(
            state.session_id,
            "chat.message",
            _message_payload("assistant", "Send a question to start."),
        )
        return

    # Every non-empty message goes to the agent; the agent owns the whole turn and
    # decides how to respond (greetings, small talk, availability, research). There
    # are no regex shortcuts or canned constants.
    async for item in _run_agent_loop(state, turn):
        yield item


async def _run_agent_loop(
    state: V2SessionState, turn: NormalizedTurn
) -> AsyncIterator[dict[str, Any]]:
    """Run bounded V2 single-agent tool-choice steps for one user turn.

    The agent reads tool results back through ``state.turn_scratchpad`` and
    continues the same conversation, so research is no longer turn-terminal: the
    agent that reads the evidence authors the final answer in its own voice.
    """
    # Answer-stream state is turn-scoped: one answer per turn, but it may span a
    # present_final_response step and the body step, so it must not reset per step.
    _reset_turn_state(state, turn)
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
            stream_live = state.turn_shell_emitted or state.turn_answer_mode not in {
                "quick",
                "deep",
            }
            async for item in _run_streaming_step(
                state, turn, stream_live=stream_live
            ):
                if "decision" in item:
                    decision = item["decision"]
                else:
                    yield item["event"]
        except Exception as exc:  # pylint: disable=broad-exception-caught
            async for item in _handle_step_failure(state, turn, exc, step_index):
                yield item
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
    note = "Aegis reached its agent loop limit before completing the turn."
    if state.turn_shell_emitted:
        # An answer stream was opened (e.g. present ran but the body never
        # finished). Close it with a final message bound to the open stream so the
        # UI does not hang mid-stream (F10).
        marker = final_shell_marker(state.turn_shell) if state.turn_shell else ""
        body = state.turn_streamed_text.strip()
        yield event(
            state.session_id,
            "chat.message",
            _message_payload(
                "assistant",
                marker + (f"{body}\n\n{note}" if body else note),
                stream_id=state.turn_stream_id,
                final=True,
            ),
        )
    else:
        yield event(
            state.session_id,
            "chat.message",
            _message_payload("assistant", note),
        )


async def _run_streaming_step(
    state: V2SessionState, turn: NormalizedTurn, *, stream_live: bool
) -> AsyncIterator[dict[str, Any]]:
    """Run one streamed agent step, emitting body deltas and the decision.

    Yields ``{"event": ...}`` for each streamed ``chat.delta`` (and its answer
    header) and a final ``{"decision": AgentDecision}``. Bridges the agent step's
    ``on_delta`` callback through a queue.

    Direct answers stream live from the first token in general conversation mode
    so simple turns do not sit behind the thinking indicator until the whole step
    ends. Research-backed turns still buffer until ``present_final_response``
    opens the final shell; this preserves the guard against accidental pre-tool
    narration clobbering the agent-authored shell.

    ``stream_live=False`` remains available for callers that need the older
    defensive behavior: content is buffered and flushed only if the step resolves
    to a direct answer; if it resolves to a tool call the buffered preamble is
    discarded.
    """
    delta_queue: asyncio.Queue[str] = asyncio.Queue()
    stream_was_open = state.turn_shell_emitted
    streamed_live_this_step = False

    def _on_delta(text: str) -> None:
        delta_queue.put_nowait(text)

    step_task = asyncio.create_task(
        run_agent_step(
            turn,
            state.conversation_context,
            state.llm_context,
            scratchpad=state.turn_scratchpad,
            on_delta=_on_delta,
        )
    )

    buffered: list[str] = []

    async def _consume(text: str) -> AsyncIterator[dict[str, Any]]:
        nonlocal streamed_live_this_step
        if stream_live:
            streamed_live_this_step = True
            async for item in _emit_answer_delta(state, turn, text):
                yield {"event": item}
        else:
            buffered.append(text)

    while True:
        if step_task.done():
            while not delta_queue.empty():
                async for item in _consume(delta_queue.get_nowait()):
                    yield item
            break

        get_task = asyncio.create_task(delta_queue.get())
        done, _pending = await asyncio.wait(
            {step_task, get_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if get_task in done:
            async for item in _consume(get_task.result()):
                yield item
        else:
            get_task.cancel()
            try:
                await get_task
            except asyncio.CancelledError:
                pass

    # ``result()`` re-raises a failed step here. With ``stream_live=False`` this
    # happens before any buffered preamble is emitted; with live streaming enabled,
    # any already-sent content is treated as part of the user-visible stream.
    decision = step_task.result()

    if decision.kind == "tool" and streamed_live_this_step and not stream_was_open:
        # The model violated the no-preamble-before-tools contract. The leaked UI
        # token cannot be recalled, but the next tool step must still be able to
        # open the correct answer shell.
        state.turn_shell = None
        state.turn_shell_emitted = False
        state.turn_streamed_any = False
        state.turn_streamed_text = ""

    if buffered and decision.kind == "direct":
        for text in buffered:
            async for item in _emit_answer_delta(state, turn, text):
                yield {"event": item}

    yield {"decision": decision}


async def _handle_step_failure(
    state: V2SessionState, turn: NormalizedTurn, exc: Exception, step_index: int
) -> AsyncIterator[dict[str, Any]]:
    """Recover from an agent-step failure, falling back to synthesis after research."""
    research_backed = state.turn_answer_mode in {"quick", "deep"}

    if research_backed and not state.turn_streamed_any:
        # No body has streamed yet. Render a coherent answer via synthesis. If the
        # shell was already emitted (present_final_response succeeded, then the
        # body step failed), reuse it rather than emitting a second shell.
        async for item in _stream_final_response(
            state,
            turn,
            mode=state.turn_answer_mode,
            chunks=state.turn_answer_chunks,
            research_result=state.turn_answer_research,
            existing_shell=state.turn_shell if state.turn_shell_emitted else None,
        ):
            yield item
        return

    if research_backed and state.turn_shell_emitted:
        # A shell (and some body) already streamed before the failure. Close the
        # open answer stream with a final message carrying whatever streamed, so
        # the UI is not left hanging and reload sees a complete message.
        marker = final_shell_marker(state.turn_shell) if state.turn_shell else ""
        yield event(
            state.session_id,
            "chat.message",
            _message_payload(
                "assistant",
                marker + state.turn_streamed_text.strip(),
                stream_id=state.turn_stream_id,
                final=True,
            ),
        )
        return

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
        _message_payload("assistant", f"Aegis could not decide the next step: {exc}"),
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
        async for item in _finish_answer(state, turn, decision.content):
            yield item
        outcome.status = "complete"
        outcome.reason = "answer_completed"
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

    if tool_call.name == "present_final_response":
        shell = _shell_from_present_arguments(state, turn, arguments)
        if not state.turn_shell_emitted:
            state.turn_shell = shell
            yield event(
                state.session_id,
                "final_response.started",
                {
                    "stream_id": state.turn_stream_id,
                    "shell": shell.model_dump(mode="json"),
                },
            )
            state.turn_shell_emitted = True
        call_id = tool_call.id or f"call_{uuid4().hex}"
        state.turn_scratchpad.append(_assistant_tool_message(tool_call, call_id))
        state.turn_scratchpad.append(
            _tool_result_message(
                call_id,
                {
                    "status": "shell_accepted",
                    "instruction": (
                        "Header accepted. Now write the analyst brief body as "
                        "markdown, citing source-backed claims with the exact "
                        "[[source:chunk_id]] evidence ids. Do not call more tools."
                    ),
                },
            )
        )
        outcome.status = "continue"
        outcome.reason = "final_response_presented"
        return

    if tool_call.name == "check_data_availability":
        availability_turn = _availability_turn_from_arguments(turn, arguments)
        async for item in _run_availability_turn(state, availability_turn):
            yield item
        if state.turn_disposition != "continue":
            outcome.status = state.turn_disposition
            outcome.reason = f"availability_{state.turn_disposition}"
            return
        # Availability succeeded: feed the coverage back so the same agent reads
        # it and authors the reply on the next loop step (F7).
        call_id = tool_call.id or f"call_{uuid4().hex}"
        state.turn_scratchpad.append(_assistant_tool_message(tool_call, call_id))
        state.turn_scratchpad.append(
            _tool_result_message(call_id, state.turn_tool_feedback)
        )
        outcome.status = "continue"
        outcome.reason = "availability_completed_pending_answer"
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

        if state.turn_disposition != "continue":
            outcome.status = state.turn_disposition
            outcome.reason = f"research_{state.turn_disposition}"
            return

        # Research succeeded: feed the evidence back so the same agent authors
        # the final answer in its own voice on the next loop step.
        call_id = tool_call.id or f"call_{uuid4().hex}"
        state.turn_scratchpad.append(_assistant_tool_message(tool_call, call_id))
        state.turn_scratchpad.append(
            _tool_result_message(call_id, state.turn_tool_feedback)
        )
        outcome.status = "continue"
        outcome.reason = "research_completed_pending_answer"
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
