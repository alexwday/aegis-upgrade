"""Helpers for resolving model tiers across V1 and V2 agent paths."""

from __future__ import annotations

from typing import Any

from .settings import config


def resolve_research_model(context: dict[str, Any] | None, default_tier: str) -> str:
    """Return the model name for a research LLM call.

    V1 source pipelines historically hard-code a source-specific tier. V2 passes
    a model plan in context so the same source pipeline can honor the UI model
    selection without changing the public V1 path.
    """
    context = context or {}
    model_plan = context.get("v2_model_plan")
    research_model = None
    if isinstance(model_plan, dict):
        research_model = model_plan.get("research_model")
    elif model_plan is not None:
        research_model = getattr(model_plan, "research_model", None)
    if research_model:
        return str(research_model)
    return getattr(config.llm, default_tier).model
