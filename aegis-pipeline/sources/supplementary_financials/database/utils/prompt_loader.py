"""Prompt loading helpers for LLM pipeline stages."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def load_prompt(name: str, prompts_dir: Path) -> dict[str, Any]:
    """Load and validate one YAML prompt definition by filename stem.

    Args:
        name: Prompt file stem, without ``.yaml``.
        prompts_dir: Directory containing prompt YAML files.

    Returns:
        A validated prompt mapping with system/user prompts and tool schema.

    Raises:
        FileNotFoundError: If the prompt file does not exist.
        RuntimeError: If PyYAML is not installed.
        ValueError: If the prompt is malformed.
    """
    path = prompts_dir / f"{name}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    try:
        yaml_module = __import__("yaml")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyYAML is required to load LLM prompts. Install project "
            "requirements before running LLM stages."
        ) from exc

    with path.open("r", encoding="utf-8") as prompt_file:
        data = yaml_module.safe_load(prompt_file)
    return _validate_prompt(name, data)


def _validate_prompt(name: str, data: Any) -> dict[str, Any]:
    """Validate the prompt contract used by LLM stages."""
    if not isinstance(data, dict):
        raise ValueError(f"Prompt {name!r} must contain a mapping")
    prompt = {
        "stage": _required_str(name, data, "stage"),
        "version": _required_str(name, data, "version"),
        "description": _required_str(name, data, "description"),
        "system_prompt": _required_str(name, data, "system_prompt"),
        "user_prompt": _required_str(name, data, "user_prompt"),
        "tool_choice": data.get("tool_choice", "required"),
        "tools": data.get("tools"),
    }
    if prompt["tool_choice"] not in {"required", "auto", "none"}:
        raise ValueError(f"Prompt {name!r} has invalid tool_choice")
    if not isinstance(prompt["tools"], list) or not prompt["tools"]:
        raise ValueError(f"Prompt {name!r} requires non-empty tools")
    return prompt


def _required_str(name: str, data: dict[str, Any], field_name: str) -> str:
    """Return a required string prompt field."""
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Prompt {name!r} requires field {field_name!r}")
    return value.strip()
