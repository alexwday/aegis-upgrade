"""PostgreSQL prompt loading helpers for LLM pipeline stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from psycopg2.extras import RealDictCursor

from connections.postgres_connector import connection_scope

from .source_context import get_source_context


SOURCE_LAYER = get_source_context().prompt_layer


def load_prompt(name: str, prompts_dir: Path | None = None) -> dict[str, Any]:
    """Load and validate one prompt definition from PostgreSQL.

    The ``prompts_dir`` argument is accepted for old call-site compatibility but
    is intentionally ignored. Runtime prompts must exist in ``public.prompts``.
    """
    del prompts_dir
    with connection_scope() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    model,
                    layer,
                    name,
                    description,
                    system_prompt,
                    user_prompt,
                    tool_definition,
                    version,
                    updated_at
                FROM prompts
                WHERE model = %s
                  AND layer = %s
                  AND name = %s
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                ("aegis", SOURCE_LAYER, name),
            )
            data = cur.fetchone()
    if data is None:
        raise LookupError(f"Prompt aegis/{SOURCE_LAYER}/{name} was not found in Postgres")
    return _validate_prompt(name, dict(data))


def _validate_prompt(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the prompt contract used by LLM stages."""
    tool_definition = data.get("tool_definition")
    if isinstance(tool_definition, dict):
        tools = [tool_definition]
    elif isinstance(tool_definition, list):
        tools = tool_definition
    else:
        tools = []

    tool_choice = "required" if tools else "none"
    prompt = {
        "stage": name,
        "version": _required_str(name, data, "version"),
        "description": _required_str(name, data, "description"),
        "system_prompt": _required_str(name, data, "system_prompt"),
        "user_prompt": _required_str(name, data, "user_prompt"),
        "tool_choice": tool_choice,
        "tools": tools,
    }
    if prompt["tool_choice"] not in {"required", "auto", "none"}:
        raise ValueError(f"Prompt {name!r} has invalid tool_choice")
    if not isinstance(prompt["tools"], list):
        raise ValueError(f"Prompt {name!r} tools must be a list")
    if prompt["tool_choice"] != "none" and not prompt["tools"]:
        raise ValueError(f"Prompt {name!r} requires non-empty tools")
    return prompt


def _required_str(name: str, data: dict[str, Any], field_name: str) -> str:
    """Return a required string prompt field."""
    value = data.get(field_name)
    if value is None:
        raise ValueError(f"Prompt {name!r} requires field {field_name!r}")
    value = str(value)
    if not value.strip():
        raise ValueError(f"Prompt {name!r} requires field {field_name!r}")
    return value.strip()
