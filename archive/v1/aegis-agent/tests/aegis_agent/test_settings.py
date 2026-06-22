"""Tests for settings resolution."""

from __future__ import annotations

from aegis_agent.utils.settings import Config


def test_config_uses_openai_api_key_when_api_key_is_absent(monkeypatch) -> None:
    """Direct OpenAI auth should honor the standard OPENAI_API_KEY variable."""
    old_instance = Config._instance  # pylint: disable=protected-access
    old_loaded = Config._loaded  # pylint: disable=protected-access
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")

    try:
        Config._instance = None  # pylint: disable=protected-access
        Config._loaded = False  # pylint: disable=protected-access
        loaded = Config()
    finally:
        Config._instance = old_instance  # pylint: disable=protected-access
        Config._loaded = old_loaded  # pylint: disable=protected-access

    assert loaded.api_key == "test-openai-key"


def test_config_uses_agent_reasoning_effort_overrides(monkeypatch) -> None:
    """Agent-specific reasoning effort env should configure model tiers."""
    old_instance = Config._instance  # pylint: disable=protected-access
    old_loaded = Config._loaded  # pylint: disable=protected-access
    monkeypatch.setenv("AGENT_LLM_REASONING_EFFORT_SMALL", "minimal")
    monkeypatch.setenv("AGENT_LLM_REASONING_EFFORT_MEDIUM", "low")
    monkeypatch.setenv("AGENT_LLM_REASONING_EFFORT_LARGE", "medium")

    try:
        Config._instance = None  # pylint: disable=protected-access
        Config._loaded = False  # pylint: disable=protected-access
        loaded = Config()
    finally:
        Config._instance = old_instance  # pylint: disable=protected-access
        Config._loaded = old_loaded  # pylint: disable=protected-access

    assert loaded.llm.small.reasoning_effort == "minimal"
    assert loaded.llm.medium.reasoning_effort == "low"
    assert loaded.llm.large.reasoning_effort == "medium"
