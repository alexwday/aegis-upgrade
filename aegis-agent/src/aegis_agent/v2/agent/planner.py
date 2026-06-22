"""LLM turn planning for the V2 main Aegis agent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Literal

from ...connections.llm_connector import complete_with_tools
from .conversation import ConversationContext
from .models import NormalizedTurn


PlannedAction = Literal["conversation", "clarify", "availability", "research"]


@dataclass(frozen=True)
class ClarificationOption:
    """One optional model-suggested clarification choice."""

    id: str
    label: str
    description: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnPlan:
    """Model-selected next action for one user turn."""

    action: PlannedAction
    rationale: str = ""
    clarification_question: str | None = None
    missing_scope: list[str] = field(default_factory=list)
    clarification_options: list[ClarificationOption] = field(default_factory=list)


PLANNER_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "plan_v2_turn",
        "description": "Select the next action for the single Aegis agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["conversation", "clarify", "availability", "research"],
                },
                "rationale": {
                    "type": "string",
                    "description": "Brief internal reason for the selected action.",
                },
                "clarification_question": {
                    "type": "string",
                    "description": "Question to ask when action is clarify.",
                },
                "missing_scope": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": [
                            "bank",
                            "fiscal_year",
                            "quarter",
                            "data_sources",
                            "research_question",
                        ],
                    },
                    "description": "Scope required before source-backed research can run.",
                },
                "clarification_options": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "description": {"type": "string"},
                            "payload": {"type": "object"},
                        },
                        "required": ["id", "label"],
                        "additionalProperties": False,
                    },
                    "description": "Use when there are clear choices the user can click.",
                },
            },
            "required": ["action", "rationale"],
            "additionalProperties": False,
        },
    },
}


def _llm_context(turn: NormalizedTurn) -> dict[str, Any]:
    """Build connector context for the planner call."""
    token = os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    return {
        "execution_id": turn.run_uuid or "v2-agent-planner",
        "auth_config": {
            "success": bool(token),
            "method": "api_key",
            "token": token,
        },
        "ssl_config": {"verify": False},
    }


def _turn_payload(turn: NormalizedTurn) -> dict[str, Any]:
    """Return compact structured turn state for the planner prompt."""
    return {
        "user_message": turn.content,
        "selected_sources": turn.source_ids,
        "selected_banks": turn.bank_symbols,
        "selected_bank_categories": turn.bank_categories,
        "selected_fiscal_years": turn.fiscal_years,
        "selected_quarters": turn.quarters,
        "ui_model_selection": turn.model_mode,
        "ui_search_selection": turn.search_mode,
    }


def _planner_messages(
    turn: NormalizedTurn,
    conversation_context: ConversationContext,
) -> list[dict[str, str]]:
    """Build the planning prompt."""
    prior_context = conversation_context.to_prompt_text()
    system = (
        "You are the planning layer for the single Aegis agent. "
        "Aegis can have normal back-and-forth conversation, discuss prior messages, "
        "discuss prior artifacts and widgets, check data availability, ask clarifying questions, "
        "and run source-backed research. "
        "Select exactly one next action with the plan_v2_turn tool. "
        "Choose conversation when the user is greeting, discussing workflow, following up on prior content, "
        "or asking something that does not require source retrieval. "
        "Choose availability only when the user wants to know what data/source coverage exists. "
        "Choose research only when the user is asking for source-backed analysis or retrieval. "
        "Choose clarify when source-backed research is intended but the needed scope or intent is unclear. "
        "Before research, bank, fiscal year, quarter, and a research question must be clear. "
        "When clarifying and there are clear choices, include short clickable options for the clarification widget."
    )
    user = {
        "turn": _turn_payload(turn),
        "prior_context": prior_context,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _parse_tool_arguments(response: dict[str, Any]) -> dict[str, Any]:
    """Extract planner tool-call arguments."""
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError("Planner returned no choices.")
    message = choices[0].get("message") or {}
    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        raise RuntimeError("Planner did not call plan_v2_turn.")
    raw_arguments = tool_calls[0].get("function", {}).get("arguments") or "{}"
    if isinstance(raw_arguments, dict):
        return raw_arguments
    return json.loads(raw_arguments)


def _option_from_payload(payload: Any, index: int) -> ClarificationOption | None:
    """Normalize one model-suggested clarification option."""
    if not isinstance(payload, dict):
        return None
    label = str(payload.get("label") or "").strip()
    if not label:
        return None
    option_id = str(payload.get("id") or f"option_{index}").strip()
    description = payload.get("description")
    option_payload = payload.get("payload")
    return ClarificationOption(
        id=option_id or f"option_{index}",
        label=label,
        description=str(description).strip() if description else None,
        payload=option_payload if isinstance(option_payload, dict) else {},
    )


def _normalize_plan(arguments: dict[str, Any]) -> TurnPlan:
    """Validate and normalize planner tool arguments."""
    action = str(arguments.get("action") or "").strip()
    if action not in {"conversation", "clarify", "availability", "research"}:
        raise RuntimeError(f"Planner returned unsupported action: {action!r}")
    options = [
        option
        for index, item in enumerate(
            arguments.get("clarification_options") or [], start=1
        )
        if (option := _option_from_payload(item, index)) is not None
    ]
    missing_scope = [
        str(item)
        for item in arguments.get("missing_scope") or []
        if str(item)
        in {"bank", "fiscal_year", "quarter", "data_sources", "research_question"}
    ]
    question = str(arguments.get("clarification_question") or "").strip() or None
    return TurnPlan(
        action=action,  # type: ignore[arg-type]
        rationale=str(arguments.get("rationale") or "").strip(),
        clarification_question=question,
        missing_scope=missing_scope,
        clarification_options=options,
    )


async def plan_turn(
    turn: NormalizedTurn,
    conversation_context: ConversationContext,
) -> TurnPlan:
    """Use the orchestrator model to select the next agent action."""
    if not os.getenv("API_KEY") and not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("Aegis planning requires API_KEY or OPENAI_API_KEY.")
    response = await complete_with_tools(
        _planner_messages(turn, conversation_context),
        [PLANNER_TOOL],
        _llm_context(turn),
        {
            "model": turn.model_plan.orchestrator_model if turn.model_plan else None,
            "temperature": 0,
            "max_tokens": 900,
            "tool_choice": {
                "type": "function",
                "function": {"name": "plan_v2_turn"},
            },
        },
    )
    return _normalize_plan(_parse_tool_arguments(response))
