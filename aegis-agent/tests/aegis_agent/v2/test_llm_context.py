"""Tests for shared V2 LLM runtime context setup."""

from __future__ import annotations

import pytest

import aegis_agent.v2.agent.llm_context as llm_context


@pytest.fixture(autouse=True)
def fake_ssl(monkeypatch) -> None:
    """Keep auth tests focused on mode selection."""

    monkeypatch.setattr(
        llm_context,
        "setup_ssl",
        lambda: {"success": True, "verify": False},
    )


@pytest.mark.asyncio
async def test_oauth_auth_method_ignores_ambient_api_key(monkeypatch) -> None:
    """Work-computer OAuth mode should not be bypassed by shell API keys."""
    captured: dict[str, object] = {}

    async def fake_setup_authentication(execution_id, ssl_config):
        captured["execution_id"] = execution_id
        captured["ssl_config"] = ssl_config
        return {
            "success": True,
            "method": "oauth",
            "token": "oauth-token",
            "header": {"Authorization": "Bearer oauth-token"},
        }

    monkeypatch.setenv("AUTH_METHOD", "oauth")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")
    monkeypatch.setattr(
        llm_context, "setup_authentication", fake_setup_authentication
    )

    context = await llm_context.build_llm_context("run-1", "planning")

    assert context["auth_config"]["method"] == "oauth"
    assert context["auth_config"]["token"] == "oauth-token"
    assert captured["execution_id"] == "run-1"


@pytest.mark.asyncio
async def test_api_key_auth_requires_api_key(monkeypatch) -> None:
    """API-key mode should fail explicitly when no direct token is configured."""
    monkeypatch.setenv("AUTH_METHOD", "api_key")
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="planning requires API_KEY"):
        await llm_context.build_llm_context("run-1", "planning")
