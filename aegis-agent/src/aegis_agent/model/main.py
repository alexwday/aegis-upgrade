"""
Entry point for the multi-source Aegis Agent workflow.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Union

from ..connections.oauth_connector import setup_authentication
from ..utils.conversation import process_conversation
from ..utils.logging import get_logger, setup_logging
from ..utils.monitor import add_monitor_entry, initialize_monitor, post_monitor_entries_async
from ..utils.settings import config
from ..utils.ssl import setup_ssl
from ..utils.sql_prompt import postgresql_prompts
from .agents import run_aegis_agent
from .agents.schemas import DEFAULT_DOCUMENT_SOURCES


def _postgres_setup_message() -> str:
    """Return a concise setup message for missing PostgreSQL connectivity."""
    return (
        "Aegis is not ready because PostgreSQL is not reachable. "
        f"Expected {config.postgres_host}:{config.postgres_port}/"
        f"{config.postgres_database or '<database>'}. Start PostgreSQL or update "
        "`POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DATABASE`, and credentials in `.env`, "
        "then restart the server."
    )


def _auth_setup_message(auth_error: str | None = None) -> str:
    """Return a concise setup message for missing LLM authentication."""
    detail = f" Current auth error: {auth_error}." if auth_error else ""
    return (
        "Aegis is not ready because LLM authentication is not configured. "
        "Set `API_KEY` or configure OAuth settings in `.env`, then restart the server."
        f"{detail}"
    )


def _user_facing_error(exc: Exception) -> str:
    """Map backend dependency exceptions to chat-safe messages."""
    message = str(exc)
    lowered = message.lower()
    if (
        "connection refused" in lowered
        and ("postgres" in lowered or "5432" in lowered or "sqlalchemy" in lowered)
    ):
        return _postgres_setup_message()
    if "api_key not configured" in lowered:
        return _auth_setup_message("API_KEY not configured")
    return message


async def model(
    conversation: Optional[Union[Dict[str, Any], List[Dict[str, str]]]] = None,
    db_names: Optional[List[str]] = None,
    source_filter: Optional[List[str]] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Stream one Aegis Agent turn as websocket-compatible events.

    The public contract remains an async generator yielding dictionaries with
    event types such as agent, agent_status, subagent_start, subagent, ui_card,
    status, and error.
    """
    setup_logging()
    logger = get_logger()
    execution_id = str(uuid.uuid4())
    initialize_monitor(execution_id, "aegis_agent")
    run_start = datetime.now(timezone.utc)

    logger.info(
        "model.started",
        execution_id=execution_id,
        db_names=db_names,
        source_filter=source_filter,
    )

    try:
        try:
            postgresql_prompts()
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "model.prompt_cache_unavailable",
                execution_id=execution_id,
                error=str(exc),
            )

        ssl_start = datetime.now(timezone.utc)
        ssl_config = setup_ssl()
        add_monitor_entry(
            stage_name="SSL_Setup",
            stage_start_time=ssl_start,
            stage_end_time=datetime.now(timezone.utc),
            status=ssl_config.get("status", "Unknown"),
            decision_details=ssl_config.get("decision_details", "SSL setup completed"),
            error_message=ssl_config.get("error"),
        )

        auth_start = datetime.now(timezone.utc)
        auth_config = await setup_authentication(execution_id, ssl_config)
        add_monitor_entry(
            stage_name="Authentication",
            stage_start_time=auth_start,
            stage_end_time=datetime.now(timezone.utc),
            status=auth_config.get("status", "Unknown"),
            decision_details=auth_config.get("decision_details", "Authentication completed"),
            error_message=auth_config.get("error"),
        )
        if not auth_config.get("success"):
            yield {
                "type": "error",
                "name": "aegis",
                "content": _auth_setup_message(auth_config.get("error")),
            }
            return

        conv_start = datetime.now(timezone.utc)
        processed = process_conversation(conversation, execution_id)
        add_monitor_entry(
            stage_name="Conversation_Processing",
            stage_start_time=conv_start,
            stage_end_time=datetime.now(timezone.utc),
            status=processed.get("status", "Unknown"),
            decision_details=processed.get("decision_details"),
            error_message=processed.get("error"),
        )

        if not processed.get("success"):
            yield {
                "type": "error",
                "name": "aegis",
                "content": processed.get("error", "Conversation processing failed."),
            }
            return

        context = {
            "execution_id": execution_id,
            "auth_config": auth_config,
            "ssl_config": ssl_config,
            "db_names": db_names or list(DEFAULT_DOCUMENT_SOURCES),
            "source_filter": source_filter,
        }

        async for event in run_aegis_agent(processed["messages"], context):
            yield event

        add_monitor_entry(
            stage_name="Model_Run",
            stage_start_time=run_start,
            stage_end_time=datetime.now(timezone.utc),
            status="Success",
            decision_details="Aegis Agent turn completed",
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("model.failed", execution_id=execution_id, error=str(exc))
        safe_error = _user_facing_error(exc)
        add_monitor_entry(
            stage_name="Model_Run",
            stage_start_time=run_start,
            stage_end_time=datetime.now(timezone.utc),
            status="Failure",
            error_message=safe_error,
        )
        yield {"type": "error", "name": "aegis", "content": safe_error}
    finally:
        try:
            await post_monitor_entries_async(execution_id)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "model.monitor_post_failed",
                execution_id=execution_id,
                error=str(exc),
            )
