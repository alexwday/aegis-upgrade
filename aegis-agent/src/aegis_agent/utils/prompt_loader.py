"""PostgreSQL prompt loading helpers for Aegis agents and subagents."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import sql_prompt
from .logging import get_logger


logger = get_logger()

# Define the canonical order for global prompt composition.
GLOBAL_ORDER = ["fiscal", "project", "database", "restrictions"]


def load_prompt_from_db(
    layer: str,
    name: str,
    compose_with_globals: bool = True,
    available_databases: Optional[List[str]] = None,
    execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Load a prompt row from PostgreSQL with optional global composition.

    Raises when the row is missing or malformed. There is intentionally no local
    YAML fallback; `public.prompts` is the runtime source of truth.
    """
    _ensure_prompt_manager()
    prompt_data = _get_prompt_row(layer=layer, name=name)

    logger.info(
        "prompt_loader.loaded_from_db",
        execution_id=execution_id,
        layer=layer,
        name=name,
        source="sql_database",
        has_system_prompt=bool(prompt_data.get("system_prompt")),
        has_user_prompt=bool(prompt_data.get("user_prompt")),
        has_tool_definition=bool(prompt_data.get("tool_definition")),
        uses_global=prompt_data.get("uses_global") or [],
        version=prompt_data.get("version") or "not_set",
        created_at=(
            str(prompt_data.get("created_at")) if prompt_data.get("created_at") else "unknown"
        ),
        updated_at=(
            str(prompt_data.get("updated_at")) if prompt_data.get("updated_at") else "unknown"
        ),
    )

    if compose_with_globals and prompt_data.get("uses_global"):
        composed = _compose_with_globals(
            prompt_data,
            available_databases=available_databases,
            execution_id=execution_id,
        )
        prompt_data["composed_prompt"] = composed

    return prompt_data


def _ensure_prompt_manager() -> None:
    if sql_prompt.prompt_manager is None:
        sql_prompt.postgresql_prompts()


def _get_prompt_row(layer: str, name: str) -> Dict[str, Any]:
    prompt_data = sql_prompt.prompt_manager.get_latest_prompt(
        model="aegis", layer=layer, name=name, system_prompt=False
    )
    if not isinstance(prompt_data, dict):
        raise TypeError(f"Prompt aegis/{layer}/{name} returned non-mapping data")
    return prompt_data


def _compose_with_globals(
    prompt_data: Dict[str, Any],
    *,
    available_databases: Optional[List[str]],
    execution_id: Optional[str],
) -> str:
    uses_global = prompt_data.get("uses_global") or []
    global_prompt_parts = []
    globals_loaded = []

    for global_name in GLOBAL_ORDER:
        if global_name not in uses_global:
            continue
        global_prompt_parts.append(
            _load_global_prompt(global_name, available_databases=available_databases)
        )
        globals_loaded.append(global_name)

    main_content = None
    content_key = None
    for key in ["system_prompt", "system_prompt_template", "content"]:
        if prompt_data.get(key):
            main_content = str(prompt_data[key]).strip()
            content_key = key
            break

    if not main_content:
        raise ValueError(
            f"Prompt aegis/{prompt_data.get('layer')}/{prompt_data.get('name')} has no content"
        )

    composed = "\n\n---\n\n".join(global_prompt_parts + [main_content])
    prompt_data[f"original_{content_key}"] = main_content
    logger.info(
        "prompt_loader.globals_composed",
        execution_id=execution_id,
        layer=prompt_data.get("layer"),
        name=prompt_data.get("name"),
        globals_loaded=globals_loaded,
        total_globals=len(globals_loaded),
        composed_length=len(composed),
    )
    return composed


def _load_global_prompt(
    global_name: str, *, available_databases: Optional[List[str]]
) -> str:
    if global_name == "database" and available_databases is not None:
        from .database_filter import get_database_prompt

        return get_database_prompt(available_databases)

    prompt_data = _get_prompt_row(layer="global", name=global_name)
    system_prompt = prompt_data.get("system_prompt")
    if not system_prompt:
        raise ValueError(f"Global prompt {global_name!r} has no system_prompt")
    return str(system_prompt).strip()


def load_tools_from_db(
    layer: str,
    name: str,
    execution_id: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Load one prompt's tool definition from PostgreSQL in OpenAI format."""
    prompt_data = load_prompt_from_db(
        layer=layer,
        name=name,
        compose_with_globals=False,
        execution_id=execution_id,
    )
    tool_definition = prompt_data.get("tool_definition")
    if not tool_definition:
        return None
    if isinstance(tool_definition, dict):
        return format_tools_for_openai([tool_definition])
    if isinstance(tool_definition, list):
        return format_tools_for_openai(tool_definition)
    raise ValueError(f"Prompt aegis/{layer}/{name} has invalid tool_definition")


def format_tools_for_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate and return tool definitions in the OpenAI Chat Completions format."""
    formatted_tools = []

    for tool in tools:
        if not isinstance(tool, dict):
            logger.warning("prompt_loader.format_tools.invalid_tool", tool=tool)
            continue
        if "type" not in tool:
            logger.warning("prompt_loader.format_tools.missing_type", tool=tool)
            continue
        if tool.get("type") == "function" and "function" not in tool:
            logger.warning("prompt_loader.format_tools.missing_function", tool=tool)
            continue
        formatted_tools.append(tool)

    return formatted_tools
