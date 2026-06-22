"""Tests for shared model tier resolution."""

from __future__ import annotations

from dataclasses import dataclass

from aegis_agent.utils.model_tiers import resolve_research_model
from aegis_agent.utils.settings import config


@dataclass(frozen=True)
class _Plan:
    research_model: str


def test_resolve_research_model_uses_v2_dataclass_plan() -> None:
    """V2 deep research should override V1 source defaults with the planned model."""
    assert (
        resolve_research_model({"v2_model_plan": _Plan("research-model")}, "small")
        == "research-model"
    )


def test_resolve_research_model_uses_v2_dict_plan() -> None:
    """Serialized V2 model plans should also work."""
    assert (
        resolve_research_model(
            {"v2_model_plan": {"research_model": "dict-research-model"}}, "small"
        )
        == "dict-research-model"
    )


def test_resolve_research_model_falls_back_to_v1_default_tier() -> None:
    """V1 paths without a V2 model plan should keep their existing default tier."""
    assert resolve_research_model({}, "small") == config.llm.small.model
