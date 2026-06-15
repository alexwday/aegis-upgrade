"""Tests for OpenAI Chat Completions parameter compatibility."""

from __future__ import annotations

from aegis_agent.connections.llm_connector import _apply_generation_limits


def test_gpt_5_models_use_max_completion_tokens() -> None:
    """GPT-5-class models should not receive legacy max_tokens or temperature."""
    api_params = {}

    _apply_generation_limits(api_params, "gpt-5.4-mini", 0.7, 1234)

    assert api_params == {"max_completion_tokens": 1234}


def test_legacy_chat_models_use_max_tokens() -> None:
    """Legacy chat models should keep the existing max_tokens behavior."""
    api_params = {}

    _apply_generation_limits(api_params, "gpt-4.1-mini-2025-04-14", 0.5, 2000)

    assert api_params == {"temperature": 0.5, "max_tokens": 2000}
