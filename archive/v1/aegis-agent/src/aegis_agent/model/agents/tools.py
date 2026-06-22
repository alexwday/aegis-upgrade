"""
OpenAI Chat Completions tools for the single Aegis agent.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .chart_slots import audit_chart_slots
from .progress import ResearchProgressStore, emit_event
from .research import run_research_tool
from .schemas import (
    BankPeriodCombination,
    DEFAULT_DOCUMENT_SOURCES,
    FinalResponseShell,
    ResearchRequest,
)
from .ui_cards import build_choice_card_event


RESEARCH_SOURCE_IDS = {
    "investor_slides",
    "supplementary_financials",
    "rts",
    "pillar3",
    "transcripts",
    "event_transcripts",
}

AGENT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "present_choice_card",
            "description": (
                "Ask the user a clarification question with 2-4 obvious choices. "
                "Use this only when the user can answer by selecting one option."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                                "metadata": {"type": "object"},
                            },
                            "required": ["id", "label"],
                            "additionalProperties": False,
                        },
                    },
                    "allow_free_text": {"type": "boolean", "default": True},
                    "metadata": {"type": "object"},
                },
                "required": ["question", "options"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_research",
            "description": (
                "Run document-source research after the user-confirmed scope is clear. "
                "Requires explicit banks, fiscal years, quarters, and research question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "sources": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "investor_slides",
                                "supplementary_financials",
                                "rts",
                                "pillar3",
                                "transcripts",
                                "event_transcripts",
                            ],
                        },
                        "default": DEFAULT_DOCUMENT_SOURCES,
                    },
                    "combinations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "bank_id": {"type": "string"},
                                "bank_name": {"type": "string"},
                                "bank_symbol": {"type": "string"},
                                "fiscal_year": {"type": "integer"},
                                "quarter": {
                                    "type": "string",
                                    "enum": ["Q1", "Q2", "Q3", "Q4"],
                                },
                            },
                            "required": ["fiscal_year", "quarter"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["question", "combinations"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "audit_chart_slots",
            "description": (
                "Audit intended async chart slots against the latest research before "
                "finalizing. Use this before emitting any CHART_SLOT marker."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slots": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "slot_id": {"type": "string"},
                                "title": {"type": "string"},
                                "chart_type": {
                                    "type": "string",
                                    "enum": [
                                        "peer_rank_bar",
                                        "trend_line",
                                        "trend_bar",
                                        "multi_series_line",
                                        "slopegraph",
                                        "delta_bar",
                                        "composition_stacked_bar",
                                        "composition_100_bar",
                                        "waterfall",
                                        "scatter_plot",
                                        "small_multiple_panel",
                                        "heatmap",
                                    ],
                                },
                                "intent": {"type": "string"},
                                "subtitle": {"type": "string"},
                                "banks": {"type": "array", "items": {"type": "string"}},
                                "periods": {"type": "array", "items": {"type": "string"}},
                                "metrics": {"type": "array", "items": {"type": "string"}},
                                "source_ids": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["slot_id", "title", "chart_type", "intent"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["slots"],
                "additionalProperties": False,
            },
        },
    },
]


def _parse_tool_arguments(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    """Parse JSON function arguments from a Chat Completions tool call."""
    raw_arguments = tool_call.get("function", {}).get("arguments") or "{}"
    if isinstance(raw_arguments, dict):
        return raw_arguments
    return json.loads(raw_arguments)


def _source_filter_from_context(context: Dict[str, Any]) -> List[str]:
    """Return the validated source filter selected outside the model."""
    raw_sources = context.get("source_filter")
    if not isinstance(raw_sources, list):
        return []

    selected: List[str] = []
    for source in raw_sources:
        normalized = str(source or "").strip()
        if normalized in RESEARCH_SOURCE_IDS and normalized not in selected:
            selected.append(normalized)
    return selected


def _apply_source_filter(arguments: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Force run_research to honor a user-selected source filter."""
    selected_sources = _source_filter_from_context(context)
    if not selected_sources:
        return arguments
    return {**arguments, "sources": selected_sources}


def _missing_scope(arguments: Dict[str, Any]) -> List[str]:
    """Return missing scope fields that would make research unsafe to execute."""
    missing: List[str] = []
    if not str(arguments.get("question", "")).strip():
        missing.append("research question")

    combinations = arguments.get("combinations") or []
    if not combinations:
        return missing + ["bank-period combinations"]

    for index, combo in enumerate(combinations, start=1):
        label = f"combination {index}"
        if not any(combo.get(field) for field in ("bank_id", "bank_symbol", "bank_name")):
            missing.append(f"{label} bank")
        if not combo.get("fiscal_year"):
            missing.append(f"{label} fiscal year")
        if not combo.get("quarter"):
            missing.append(f"{label} quarter")

    return missing


def is_research_scope_complete(arguments: Dict[str, Any]) -> bool:
    """Return whether run_research has enough scope to safely query data."""
    if _missing_scope(arguments):
        return False
    try:
        ResearchRequest.model_validate(arguments)
    except ValidationError:
        return False
    return all(
        isinstance(combo, BankPeriodCombination)
        and bool(combo.bank_id or combo.bank_symbol or combo.bank_name)
        for combo in ResearchRequest.model_validate(arguments).combinations
    )


async def dispatch_tool_call(
    tool_call: Dict[str, Any],
    context: Dict[str, Any],
    output_queue: Optional[asyncio.Queue] = None,
) -> Dict[str, Any]:
    """Dispatch one Chat Completions tool call."""
    name = tool_call.get("function", {}).get("name")
    arguments = _parse_tool_arguments(tool_call)

    if name == "present_choice_card":
        event = build_choice_card_event(
            question=arguments["question"],
            options=arguments["options"],
            allow_free_text=arguments.get("allow_free_text", True),
            metadata=arguments.get("metadata") or {},
        )
        await emit_event(output_queue, event)
        return {
            "status": "awaiting_user",
            "card_id": event["content"]["card_id"],
            "question": event["content"]["question"],
        }

    if name == "run_research":
        arguments = _apply_source_filter(arguments, context)
        missing = _missing_scope(arguments)
        if missing:
            return {
                "status": "needs_clarification",
                "missing_scope": missing,
                "message": (
                    "Research was not started because the request is missing: "
                    f"{', '.join(missing)}."
                ),
            }
        try:
            ResearchRequest.model_validate(arguments)
        except ValidationError as exc:
            return {
                "status": "needs_clarification",
                "missing_scope": ["valid research scope"],
                "message": f"Research was not started because the scope was invalid: {exc}",
            }
        if context.get("chart_backfill_pending") and not context.get("chart_backfill_used"):
            context["chart_backfill_used"] = True
            context["chart_backfill_pending"] = False
        progress_store = ResearchProgressStore(output_queue)
        return await run_research_tool(arguments, context, output_queue, progress_store)

    if name == "audit_chart_slots":
        return audit_chart_slots(arguments, context)

    if name == "start_final_response":
        try:
            shell = FinalResponseShell.model_validate(arguments)
        except ValidationError as exc:
            return {
                "status": "error",
                "message": f"Final response shell was invalid: {exc}",
            }

        await emit_event(
            output_queue,
            {
                "type": "final_response_start",
                "name": "aegis",
                "content": shell.model_dump(mode="json"),
            },
        )
        return {
            "status": "final_response_started",
            "render_mode": shell.render_mode,
            "body_style": shell.body_style,
        }

    return {"status": "error", "message": f"Unknown tool: {name}"}
