"""
Prompt loader utility for composing agent and subagent prompts with global contexts.
"""

from pathlib import Path
from typing import Dict, Any, List, Optional

import yaml

from .logging import get_logger
from . import sql_prompt

logger = get_logger()


# Define the canonical order for global prompts
GLOBAL_ORDER = ["fiscal", "project", "database", "restrictions"]


def load_yaml(file_path: str) -> Dict[str, Any]:
    """
    Load and parse a YAML file.

    Args:
        file_path: Relative path to YAML file from prompts directory

    Returns:
        Parsed YAML content as dictionary
    """
    prompts_dir = Path(__file__).parent.parent / "model" / "prompts"
    full_path = prompts_dir / file_path

    if not full_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {full_path}")

    with open(full_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_fiscal_prompt() -> str:
    """
    Load the dynamic fiscal prompt.

    Returns:
        Fiscal statement string
    """
    # pylint: disable=import-outside-toplevel
    import importlib.util

    fiscal_path = Path(__file__).parent.parent / "model" / "prompts" / "global" / "fiscal.py"
    spec = importlib.util.spec_from_file_location("fiscal", fiscal_path)
    fiscal_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fiscal_module)
    return fiscal_module.get_fiscal_statement()


def load_prompt_from_db(
    layer: str,
    name: str,
    compose_with_globals: bool = True,
    available_databases: Optional[List[str]] = None,
    execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Load prompt from SQL database with optional global composition.

    This is the standardized way for all agents and subagents to load prompts.
    Handles global prompt composition automatically if requested.

    Args:
        layer: Prompt layer ("aegis", "transcripts", "reports", "global")
        name: Prompt name (e.g., "router", "method_selection")
        compose_with_globals: If True, auto-compose with global prompts
        available_databases: Optional list for database filtering
        execution_id: Optional execution ID for logging

    Returns:
        Dictionary with prompt data. If compose_with_globals=True, includes
        'composed_prompt' field with globals prepended to system_prompt.

    Example:
        >>> # Agent usage
        >>> prompt_data = load_prompt_from_db("aegis", "router", compose_with_globals=True)
        >>> system_prompt = prompt_data["composed_prompt"]

        >>> # Subagent usage
        >>> prompt_data = load_prompt_from_db("transcripts", "method_selection", compose_with_globals=True)
        >>> system_prompt = prompt_data["composed_prompt"]

    Raises:
        FileNotFoundError: If prompt doesn't exist in database
    """
    if sql_prompt.prompt_manager is None:
        sql_prompt.postgresql_prompts()

    # Load prompt from database
    prompt_data = sql_prompt.prompt_manager.get_latest_prompt(
        model="aegis", layer=layer, name=name, system_prompt=False
    )

    # Log what was loaded from database
    logger.info(
        "prompt_loader.loaded_from_db",
        execution_id=execution_id,
        layer=layer,
        name=name,
        source="sql_database",
        has_system_prompt=bool(prompt_data.get("system_prompt")),
        has_user_prompt=bool(prompt_data.get("user_prompt")),
        has_tool_definition=bool(prompt_data.get("tool_definition")),
        has_tool_definitions=bool(prompt_data.get("tool_definitions")),
        uses_global=prompt_data.get("uses_global") or [],
        version=prompt_data.get("version") or "not_set",
        created_at=(
            str(prompt_data.get("created_at")) if prompt_data.get("created_at") else "unknown"
        ),
        updated_at=(
            str(prompt_data.get("updated_at")) if prompt_data.get("updated_at") else "unknown"
        ),
    )

    # If composition requested, load global prompts and compose
    if compose_with_globals and prompt_data.get("uses_global"):
        global_prompt_parts = []
        globals_loaded = []

        for global_name in GLOBAL_ORDER:
            if global_name not in prompt_data["uses_global"]:
                continue

            if global_name == "fiscal":
                # Fiscal is dynamically generated
                global_prompt_parts.append(_load_fiscal_prompt())
                globals_loaded.append(f"{global_name} (dynamic)")
            elif global_name == "database":
                # Database uses filtered prompt
                from .database_filter import get_database_prompt

                database_prompt = get_database_prompt(available_databases)
                global_prompt_parts.append(database_prompt)
                globals_loaded.append(f"{global_name} (filtered)")
            else:
                # Load other global prompts from database
                try:
                    global_data = sql_prompt.prompt_manager.get_latest_prompt(
                        model="aegis", layer="global", name=global_name, system_prompt=False
                    )
                    if global_data.get("system_prompt"):
                        global_prompt_parts.append(global_data["system_prompt"].strip())
                        globals_loaded.append(f"{global_name} (db)")
                except Exception as e:
                    logger.warning(
                        "prompt_loader.global_missing",
                        execution_id=execution_id,
                        global_name=global_name,
                        error=str(e),
                    )

        # Find the main prompt content
        main_content = None
        content_key = None
        for key in ["system_prompt", "system_prompt_template", "content"]:
            if key in prompt_data and prompt_data[key]:
                main_content = prompt_data[key]
                content_key = key
                break

        if main_content and global_prompt_parts:
            # Compose: globals + main content
            composed = "\n\n---\n\n".join(global_prompt_parts + [main_content])
            prompt_data["composed_prompt"] = composed
            prompt_data[f"original_{content_key}"] = main_content  # Save original

            # Log successful composition
            logger.info(
                "prompt_loader.globals_composed",
                execution_id=execution_id,
                layer=layer,
                name=name,
                globals_loaded=globals_loaded,
                total_globals=len(globals_loaded),
                composed_length=len(composed),
            )

    return prompt_data


def _load_global_prompts(
    uses_global: list, available_databases: Optional[List[str]] = None
) -> list:
    """
    Load global prompts in canonical order.

    Args:
        uses_global: List of global prompt names to use
        available_databases: Optional list of database IDs for filtering database prompt

    Returns:
        List of prompt content strings
    """
    prompt_parts = []

    if not uses_global:
        return prompt_parts

    for global_name in GLOBAL_ORDER:
        if global_name not in uses_global:
            continue

        if global_name == "fiscal":
            prompt_parts.append(_load_fiscal_prompt())
        elif global_name == "database":
            # Use filtered database prompt from database_filter utility
            from .database_filter import get_database_prompt

            database_prompt = get_database_prompt(available_databases)
            prompt_parts.append(database_prompt)
        else:
            try:
                global_data = load_yaml(f"global/{global_name}.yaml")
                if "content" in global_data:
                    prompt_parts.append(global_data["content"].strip())
            except FileNotFoundError:
                logger.warning(f"Global prompt '{global_name}' not found, skipping...")

    return prompt_parts


def load_global_prompts_for_agent(
    uses_global: List[str], available_databases: Optional[List[str]] = None
) -> str:
    """
    Public helper to load global prompts for agents.

    This is the recommended way for agents to load their global context prompts.
    Handles special cases like fiscal (dynamic) and database (filtered).

    Args:
        uses_global: List of global prompt names to use
        available_databases: Optional list of database IDs for filtering database prompt

    Returns:
        Joined global prompts string, separated by ---

    Example:
        >>> globals_str = load_global_prompts_for_agent(
        ...     ["project", "fiscal", "database"],
        ...     available_databases=["transcripts", "rts"]
        ... )
    """
    prompt_parts = _load_global_prompts(uses_global, available_databases)
    return "\n\n---\n\n".join(prompt_parts) if prompt_parts else ""


def load_prompt(agent_type: str, name: str, available_databases: Optional[List[str]] = None) -> str:
    """
    Load and compose a complete prompt for an agent or subagent.

    Global prompts are added in fixed order: fiscal > project > database > restrictions
    Then the agent-specific content is appended.

    Args:
        agent_type: Either "agent" or "subagent"
        name: Name of the agent (e.g., "router", "benchmarking")
        available_databases: Optional list of database IDs for filtering database prompt

    Returns:
        Fully composed prompt with globals and agent content

    Example:
        >>> prompt = load_prompt("agent", "router")
        >>> prompt = load_prompt("subagent", "benchmarking")
    """
    # Validate agent_type
    if agent_type not in ["agent", "subagent"]:
        raise ValueError(f"agent_type must be 'agent' or 'subagent', got: {agent_type}")

    # Determine YAML path based on type
    if agent_type == "subagent":
        # Subagents have their prompts in individual folders
        yaml_path = f"{name}/{name}.yaml"
    else:
        # Agents are in the agents folder
        yaml_path = f"{agent_type}s/{name}.yaml"

    try:
        agent_data = load_yaml(yaml_path)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"No {agent_type} found with name: {name}") from exc

    # Build prompt parts list
    prompt_parts = []

    # Add global prompts in canonical order
    if "uses_global" in agent_data:
        prompt_parts.extend(_load_global_prompts(agent_data["uses_global"], available_databases))

    # Add agent-specific content
    if "content" in agent_data:
        prompt_parts.append(agent_data["content"].strip())
    else:
        raise ValueError(f"No content found in {name}.yaml")

    # Join all parts with clear separation
    return "\n\n---\n\n".join(prompt_parts)


def load_agent_prompt(name: str) -> str:
    """
    Convenience function to load an agent prompt.

    Args:
        name: Name of the agent

    Returns:
        Fully composed agent prompt
    """
    return load_prompt("agent", name)


def load_subagent_prompt(name: str) -> str:
    """
    Convenience function to load a subagent prompt.

    Args:
        name: Name of the subagent

    Returns:
        Fully composed subagent prompt
    """
    return load_prompt("subagent", name)


def list_available_prompts() -> Dict[str, list]:
    """
    List all available agent and subagent prompts.

    Returns:
        Dictionary with 'agents' and 'subagents' lists
    """
    prompts_dir = Path(__file__).parent.parent / "model" / "prompts"

    agents = []
    agents_dir = prompts_dir / "agents"
    if agents_dir.exists():
        agents = [f.stem for f in agents_dir.glob("*.yaml")]

    subagents = []
    subagents_dir = prompts_dir / "subagents"
    if subagents_dir.exists():
        subagents = [f.stem for f in subagents_dir.glob("*.yaml")]

    return {"agents": sorted(agents), "subagents": sorted(subagents)}


def load_tools_from_yaml(
    prompt_name: str, agent_type: str = "agent", execution_id: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Load tool definitions from a YAML file.

    Supports the new YAML format with tool_definition or tool_definitions sections.
    This is a drop-in compatible addition that doesn't break existing functionality.

    Args:
        prompt_name: Name of the prompt file (e.g., "router", "clarifier_banks")
        agent_type: Either "agent" or "subagent" (default: "agent")
        execution_id: Optional execution ID for logging

    Returns:
        List of tool definitions in OpenAI format, or None if no tools defined

    Example:
        >>> tools = load_tools_from_yaml("router")
        >>> tools = load_tools_from_yaml("transcripts", agent_type="subagent")
    """
    try:
        # Determine YAML path based on type
        # If prompt_name contains a slash, use it as-is (direct path)
        if "/" in prompt_name:
            yaml_path = f"{prompt_name}.yaml"
        elif agent_type == "subagent":
            yaml_path = f"{prompt_name}/{prompt_name}.yaml"
        else:
            yaml_path = f"{agent_type}s/{prompt_name}.yaml"

        # Load the YAML file
        try:
            agent_data = load_yaml(yaml_path)
        except FileNotFoundError:
            logger.warning(
                "prompt_loader.load_tools.file_not_found",
                prompt_name=prompt_name,
                agent_type=agent_type,
                execution_id=execution_id,
            )
            return None

        # Check for tool_definition (singular) - used by some agents
        if "tool_definition" in agent_data:
            tool_def = agent_data["tool_definition"]
            # Format as OpenAI tool
            formatted = format_tools_for_openai([tool_def])
            logger.debug(
                "prompt_loader.load_tools.loaded_singular",
                prompt_name=prompt_name,
                tool_count=1,
                execution_id=execution_id,
            )
            return formatted

        # Check for tool_definitions (plural) - used by agents with multiple tools
        if "tool_definitions" in agent_data:
            tool_defs = agent_data["tool_definitions"]
            if not isinstance(tool_defs, list):
                logger.warning(
                    "prompt_loader.load_tools.invalid_format",
                    prompt_name=prompt_name,
                    message="tool_definitions must be a list",
                    execution_id=execution_id,
                )
                return None

            formatted = format_tools_for_openai(tool_defs)
            logger.debug(
                "prompt_loader.load_tools.loaded_plural",
                prompt_name=prompt_name,
                tool_count=len(tool_defs),
                execution_id=execution_id,
            )
            return formatted

        # No tools defined in YAML
        logger.debug(
            "prompt_loader.load_tools.no_tools", prompt_name=prompt_name, execution_id=execution_id
        )
        return None

    except Exception as e:
        logger.error(
            "prompt_loader.load_tools.error",
            prompt_name=prompt_name,
            agent_type=agent_type,
            error=str(e),
            execution_id=execution_id,
        )
        return None


def format_tools_for_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Format tool definitions for OpenAI API.

    Ensures tools are in the correct format expected by the LLM connector.
    The YAML format should already be OpenAI-compatible, but this function
    validates and normalizes the structure.

    Args:
        tools: Raw tool definitions from YAML

    Returns:
        Formatted tools for OpenAI API

    Example YAML format:
        tool_definition:
          type: "function"
          function:
            name: "route"
            description: "Binary routing decision"
            parameters:
              type: "object"
              properties:
                r:
                  type: "integer"
                  enum: [0, 1]
              required: ["r"]
    """
    formatted_tools = []

    for tool in tools:
        # Tool should already be in OpenAI format from YAML
        # We just validate it has the required structure
        if not isinstance(tool, dict):
            logger.warning("prompt_loader.format_tools.invalid_tool", tool=tool)
            continue

        # Ensure 'type' field exists
        if "type" not in tool:
            logger.warning("prompt_loader.format_tools.missing_type", tool=tool)
            continue

        # Ensure 'function' field exists for function tools
        if tool.get("type") == "function" and "function" not in tool:
            logger.warning("prompt_loader.format_tools.missing_function", tool=tool)
            continue

        # Tool is valid, add to formatted list
        formatted_tools.append(tool)

    return formatted_tools


if __name__ == "__main__":  # pragma: no cover
    # Test the prompt loader
    available = list_available_prompts()
    print("Available prompts:")
    print(f"  Agents: {available['agents']}")
    print(f"  Subagents: {available['subagents']}")

    # Test loading an agent prompt if any exist
    if available["agents"]:
        TEST_AGENT = available["agents"][0]
        print(f"\nTesting load_agent_prompt('{TEST_AGENT}'):")
        print("-" * 50)
        TEST_PROMPT = load_agent_prompt(TEST_AGENT)
        print(TEST_PROMPT[:500] + "..." if len(TEST_PROMPT) > 500 else TEST_PROMPT)
