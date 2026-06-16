#!/usr/bin/env python3
"""Upsert canonical Aegis prompt YAML files into PostgreSQL."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable, Mapping

import psycopg2
import yaml
from psycopg2.extras import Json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "aegis-prompts"
DEFAULT_ENV_CANDIDATES = (
    PROJECT_ROOT / ".env",
    PROJECT_ROOT.parent / "aegis" / ".env",
    PROJECT_ROOT / "aegis-agent" / ".env",
)


def main() -> int:
    args = parse_args()
    values = load_env_values(args.env_file)
    prompts = discover_prompt_files(args.prompts_dir)

    if args.dry_run:
        for prompt in prompts:
            print(f"would upsert aegis/{prompt['layer']}/{prompt['name']} v{prompt['version']}")
        print(f"prompt files checked: {len(prompts)}")
        return 0

    with psycopg2.connect(**database_config(values), application_name="aegis-prompt-sync") as conn:
        upserted = upsert_prompts(conn, prompts)
        conn.commit()

    print(f"prompt rows upserted: {upserted}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prompts-dir",
        type=Path,
        default=DEFAULT_PROMPTS_DIR,
        help="Directory containing canonical prompt YAML files.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        action="append",
        help=(
            "Dotenv file to load. May be passed multiple times. Defaults to root .env, "
            "sibling aegis/.env, and aegis-agent/.env when present."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate files without DB writes.")
    return parser.parse_args()


def load_env_values(env_files: list[Path] | None) -> dict[str, str]:
    values: dict[str, str] = {}
    candidates = env_files if env_files is not None else list(DEFAULT_ENV_CANDIDATES)
    for path in candidates:
        expanded = path.expanduser().resolve()
        if expanded.exists():
            values.update(read_env(expanded))
    return values


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = clean_env_value(raw_value)
    return values


def clean_env_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    quote = value[0] if value[0] in {"'", '"'} else ""
    if quote:
        end = value.rfind(quote)
        return value[1:end] if end > 0 else value[1:]
    if " #" in value:
        value = value.split(" #", 1)[0].rstrip()
    return value


def first(values: Mapping[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = values.get(key)
        if value:
            return value
    return default


def database_config(values: Mapping[str, str]) -> dict[str, str]:
    config = {
        "host": first(values, "DB_HOST", "POSTGRES_HOST", default="127.0.0.1"),
        "port": first(values, "DB_PORT", "POSTGRES_PORT", default="5432"),
        "dbname": first(values, "DB_NAME", "POSTGRES_DATABASE", default="postgres"),
        "user": first(values, "DB_USER", "POSTGRES_USER", default="postgres"),
        "password": first(values, "DB_PASSWORD", "POSTGRES_PASSWORD"),
    }
    return {key: value for key, value in config.items() if value}


def discover_prompt_files(prompts_dir: Path) -> list[dict[str, Any]]:
    root = prompts_dir.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Prompt directory not found: {root}")

    prompts = []
    for path in sorted(root.rglob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not {"model", "layer", "name"}.issubset(data):
            continue
        prompts.append(normalize_prompt(data, path))
    if not prompts:
        raise RuntimeError(f"No prompt YAML files found under {root}")
    return prompts


def normalize_prompt(data: dict[str, Any], path: Path) -> dict[str, Any]:
    prompt = {
        "model": required_str(data, "model", path),
        "layer": required_str(data, "layer", path),
        "name": required_str(data, "name", path),
        "version": str(data.get("version") or "1.0.0"),
        "description": optional_str(data.get("description")),
        "comments": optional_str(data.get("comments")),
        "system_prompt": optional_str(data.get("system_prompt")),
        "user_prompt": optional_str(data.get("user_prompt")),
        "tool_definition": normalize_tool_definition(data, path),
        "uses_global": data.get("uses_global"),
    }
    if not prompt["system_prompt"] and not prompt["user_prompt"]:
        raise ValueError(f"{path} must contain system_prompt or user_prompt")
    return prompt


def required_str(data: Mapping[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} requires non-empty {key}")
    return value.strip()


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def normalize_tool_definition(data: Mapping[str, Any], path: Path) -> Any:
    tool_definition = data.get("tool_definition")
    if tool_definition:
        if not isinstance(tool_definition, (dict, list)):
            raise ValueError(f"{path} tool_definition must be a mapping or list")
        return tool_definition

    tools = data.get("tools") or []
    if not tools:
        return None
    if not isinstance(tools, list) or len(tools) != 1 or not isinstance(tools[0], dict):
        raise ValueError(f"{path} must define exactly one tool when using tools")
    return tools[0]


def upsert_prompts(conn: psycopg2.extensions.connection, prompts: Iterable[dict[str, Any]]) -> int:
    count = 0
    with conn.cursor() as cur:
        for prompt in prompts:
            params = prompt_params(prompt)
            cur.execute(
                """
                UPDATE public.prompts
                SET
                    description = %(description)s,
                    comments = %(comments)s,
                    system_prompt = %(system_prompt)s,
                    user_prompt = %(user_prompt)s,
                    tool_definition = %(tool_definition)s,
                    uses_global = %(uses_global)s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE model = %(model)s
                  AND layer = %(layer)s
                  AND name = %(name)s
                  AND version = %(version)s
                """,
                params,
            )
            if cur.rowcount == 0:
                cur.execute(
                    """
                    INSERT INTO public.prompts (
                        model,
                        layer,
                        name,
                        description,
                        comments,
                        system_prompt,
                        user_prompt,
                        tool_definition,
                        uses_global,
                        version,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        %(model)s,
                        %(layer)s,
                        %(name)s,
                        %(description)s,
                        %(comments)s,
                        %(system_prompt)s,
                        %(user_prompt)s,
                        %(tool_definition)s,
                        %(uses_global)s,
                        %(version)s,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """,
                    params,
                )
            count += 1
    return count


def prompt_params(prompt: dict[str, Any]) -> dict[str, Any]:
    return {
        **prompt,
        "tool_definition": Json(prompt["tool_definition"])
        if prompt["tool_definition"] is not None
        else None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
