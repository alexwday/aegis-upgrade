"""Tests for chat-safe model error handling."""

from __future__ import annotations

import pytest

from aegis_agent.model import main as model_main


@pytest.fixture(autouse=True)
def stub_monitoring(monkeypatch) -> None:
    """Keep model error tests independent from monitor persistence."""
    monkeypatch.setattr(model_main, "initialize_monitor", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(model_main, "add_monitor_entry", lambda *_args, **_kwargs: None)

    async def fake_post_monitor_entries_async(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(
        model_main,
        "post_monitor_entries_async",
        fake_post_monitor_entries_async,
    )


@pytest.mark.asyncio
async def test_model_stops_with_friendly_auth_error(monkeypatch) -> None:
    """Missing auth config should not continue into prompt/database work."""
    monkeypatch.setattr(model_main, "postgresql_prompts", lambda: None)
    monkeypatch.setattr(
        model_main,
        "setup_ssl",
        lambda: {"success": True, "status": "Success", "verify": False},
    )

    async def fake_setup_authentication(*_args, **_kwargs):
        return {
            "success": False,
            "status": "Failure",
            "error": "API_KEY not configured",
            "decision_details": "Authentication failed: API_KEY not configured",
        }

    async def fail_run_agent(*_args, **_kwargs):
        raise AssertionError("agent should not run when auth setup failed")
        yield {}

    monkeypatch.setattr(model_main, "setup_authentication", fake_setup_authentication)
    monkeypatch.setattr(model_main, "run_aegis_agent", fail_run_agent)

    events = [
        event
        async for event in model_main.model(
            {"messages": [{"role": "user", "content": "hi"}]}
        )
    ]

    assert events == [
        {
            "type": "error",
            "name": "aegis",
            "content": (
                "Aegis is not ready because LLM authentication is not configured. "
                "Set `API_KEY` or configure OAuth settings in `.env`, then restart the server. "
                "Current auth error: API_KEY not configured."
            ),
        }
    ]


@pytest.mark.asyncio
async def test_model_sanitizes_postgres_connection_errors(monkeypatch) -> None:
    """Database driver exceptions should be mapped to setup guidance for the chat UI."""
    monkeypatch.setattr(model_main, "postgresql_prompts", lambda: None)
    monkeypatch.setattr(
        model_main,
        "setup_ssl",
        lambda: {"success": True, "status": "Success", "verify": False},
    )

    async def fake_setup_authentication(*_args, **_kwargs):
        return {
            "success": True,
            "status": "Success",
            "method": "api_key",
            "token": "test",
            "header": {"Authorization": "Bearer test"},
            "error": None,
            "decision_details": "Authentication method: api_key",
        }

    async def fake_run_agent(*_args, **_kwargs):
        raise RuntimeError(
            '(psycopg2.OperationalError) connection to server at "localhost" '
            "(127.0.0.1), port 5432 failed: Connection refused"
        )
        yield {}

    monkeypatch.setattr(model_main, "setup_authentication", fake_setup_authentication)
    monkeypatch.setattr(model_main, "run_aegis_agent", fake_run_agent)

    events = [
        event
        async for event in model_main.model(
            {"messages": [{"role": "user", "content": "hi"}]}
        )
    ]

    assert len(events) == 1
    assert events[0]["type"] == "error"
    assert events[0]["name"] == "aegis"
    assert "PostgreSQL is not reachable" in events[0]["content"]
    assert "connection to server" not in events[0]["content"]
