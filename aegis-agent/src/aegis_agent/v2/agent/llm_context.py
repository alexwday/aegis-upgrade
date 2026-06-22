"""Shared LLM runtime context for V2 agent components."""

from __future__ import annotations

import os
from typing import Any

from ...connections.oauth_connector import setup_authentication
from ...utils.settings import config
from ...utils.ssl import setup_ssl


def _api_key_context(
    execution_id: str, token: str, ssl_config: dict[str, Any]
) -> dict[str, Any]:
    """Build a connector context for direct API-key auth."""
    return {
        "execution_id": execution_id,
        "auth_config": {
            "success": True,
            "method": "api_key",
            "token": token,
            "header": {"Authorization": f"Bearer {token}"},
        },
        "ssl_config": ssl_config,
    }


def _env_token() -> str:
    """Return a directly configured API token, if present."""
    return os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY") or ""


def _auth_method() -> str:
    """Return the configured agent auth method with environment taking precedence."""
    return str(os.getenv("AUTH_METHOD") or config.auth_method or "api_key").lower()


async def build_llm_context(execution_id: str, purpose: str) -> dict[str, Any]:
    """Return a connector context using the same auth modes as the V1 agent path."""
    ssl_config = setup_ssl()
    if not ssl_config.get("success"):
        error = ssl_config.get("error") or "SSL setup failed."
        raise RuntimeError(f"Aegis {purpose} SSL setup failed: {error}")

    auth_method = _auth_method()
    if auth_method == "api_key":
        token = _env_token()
        if not token:
            raise RuntimeError(
                f"Aegis {purpose} requires API_KEY or OPENAI_API_KEY for API-key auth."
            )
        return _api_key_context(execution_id, token, ssl_config)

    if auth_method != "oauth":
        raise RuntimeError(
            f"Aegis {purpose} has unsupported AUTH_METHOD: {auth_method!r}."
        )

    auth_config = await setup_authentication(execution_id, ssl_config)
    if not auth_config.get("success"):
        error = auth_config.get("error") or "LLM authentication failed."
        raise RuntimeError(
            f"Aegis {purpose} requires API_KEY or OPENAI_API_KEY or configured OAuth. {error}"
        )

    return {
        "execution_id": execution_id,
        "auth_config": auth_config,
        "ssl_config": ssl_config,
    }
