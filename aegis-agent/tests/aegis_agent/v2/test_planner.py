"""Tests for the V2 LLM planner contract."""

from __future__ import annotations

import pytest

from aegis_agent.v2.agent.models import normalize_turn
from aegis_agent.v2.agent.planner import TurnPlan, _normalize_plan, plan_turn
from aegis_agent.v2.agent.conversation import ConversationContext


def test_normalize_plan_accepts_clarification_options() -> None:
    """Planner tool arguments should normalize into typed clarification choices."""
    plan = _normalize_plan(
        {
            "action": "clarify",
            "rationale": "Need bank/period scope",
            "clarification_question": "Which period?",
            "missing_scope": ["bank", "fiscal_year", "quarter"],
            "clarification_options": [
                {
                    "id": "ry_q2_2026",
                    "label": "RY-CA Q2 2026",
                    "description": "Use Royal Bank Q2 2026",
                    "payload": {
                        "filters": {
                            "bank_symbols": ["RY-CA"],
                            "fiscal_years": [2026],
                            "quarters": ["Q2"],
                        }
                    },
                }
            ],
        }
    )

    assert plan.action == "clarify"
    assert plan.clarification_question == "Which period?"
    assert plan.missing_scope == ["bank", "fiscal_year", "quarter"]
    assert plan.clarification_options[0].label == "RY-CA Q2 2026"


@pytest.mark.asyncio
async def test_plan_turn_requires_llm_credentials(monkeypatch) -> None:
    """The planner should fail explicitly instead of using keyword fallbacks."""
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    turn = normalize_turn({"content": "hi"})

    with pytest.raises(RuntimeError, match="planning requires API_KEY"):
        await plan_turn(turn, ConversationContext())


def test_turn_plan_action_contract() -> None:
    """The dataclass keeps action values explicit for orchestrator tests."""
    assert TurnPlan(action="conversation", rationale="chat").action == "conversation"
