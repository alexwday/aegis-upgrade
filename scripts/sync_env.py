#!/usr/bin/env python3
"""Sync the root .env into the agent and source-specific pipeline env files."""

from __future__ import annotations

import argparse
from collections import OrderedDict
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]

PIPELINE_SOURCES = OrderedDict(
    {
        "investor_slides": {
            "data_source": "investor-slides",
            "input_dir": "aegis-documents/investor_slides",
            "master_table": "investor_slides_master_data",
        },
        "supplementary_financials": {
            "data_source": "financial-supp",
            "input_dir": "aegis-documents/supplementary_financials",
            "master_table": "financial_supp_master_data",
        },
        "rts": {
            "data_source": "rts",
            "input_dir": "aegis-documents/rts",
            "master_table": "rts_master_data",
        },
        "pillar3": {
            "data_source": "pillar3",
            "input_dir": "aegis-documents/pillar3",
            "master_table": "pillar3_master_data",
        },
        "transcripts": {
            "data_source": "earnings-transcripts",
            "input_dir": "aegis-documents/transcripts",
            "master_table": "earnings_transcripts_master_data",
        },
        "event_transcripts": {
            "data_source": "event-transcripts",
            "input_dir": "aegis-documents/event_transcripts",
            "master_table": "event_transcripts_master_data",
        },
    }
)


def main() -> int:
    args = parse_args()
    env_path = args.env_file.expanduser().resolve()
    try:
        values = read_env(env_path)
    except FileNotFoundError:
        if not args.check:
            raise
        values = {}

    writes = {
        PROJECT_ROOT / "aegis-agent" / ".env": agent_env(values),
    }
    for source_name, metadata in PIPELINE_SOURCES.items():
        writes[
            PROJECT_ROOT
            / "aegis-pipeline"
            / "sources"
            / source_name
            / ".env"
        ] = pipeline_env(values, metadata)

    for path, env_values in writes.items():
        if args.check:
            print(f"would write {path}")
            continue
        write_env(path, env_values)
        print(f"wrote {path}")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="Root dotenv file to sync. Defaults to .env.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print target files without writing them.",
    )
    return parser.parse_args()


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"Env file not found: {path}")

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
        end_index = find_closing_quote(value, quote)
        if end_index is not None:
            return value[1:end_index]

    return strip_inline_comment(value).strip()


def find_closing_quote(value: str, quote: str) -> int | None:
    escaped = False
    for index, char in enumerate(value[1:], start=1):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == quote:
            return index
    return None


def strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False

    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == "#" and not in_single and not in_double:
            previous = value[index - 1] if index else ""
            if not previous or previous.isspace():
                return value[:index]
    return value


def first(values: Mapping[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = values.get(key, "")
        if value:
            return value
    return default


def normalize_agent_auth_mode(values: Mapping[str, str]) -> str:
    mode = first(values, "AUTH_METHOD", "LLM_AUTH_MODE", default="api_key").lower()
    return "api_key" if mode == "default" else mode


def normalize_pipeline_auth_mode(values: Mapping[str, str]) -> str:
    mode = first(values, "LLM_AUTH_MODE", "AUTH_METHOD", default="default").lower()
    return "default" if mode == "api_key" else mode


def put(
    out: OrderedDict[str, str],
    values: Mapping[str, str],
    key: str,
    default: str = "",
) -> None:
    out[key] = values.get(key, default)


def agent_env(values: Mapping[str, str]) -> OrderedDict[str, str]:
    out: OrderedDict[str, str] = OrderedDict()
    out["LOG_LEVEL"] = values.get("LOG_LEVEL", "INFO")
    out["ENVIRONMENT"] = values.get("ENVIRONMENT", "local")
    out["AUTH_METHOD"] = normalize_agent_auth_mode(values)
    out["API_KEY"] = first(values, "API_KEY", "OPENAI_API_KEY")
    out["SSL_VERIFY"] = values.get("SSL_VERIFY", "false")
    out["SSL_CERT_PATH"] = values.get("SSL_CERT_PATH", "")
    for key in (
        "OAUTH_ENDPOINT",
        "OAUTH_CLIENT_ID",
        "OAUTH_CLIENT_SECRET",
        "OAUTH_GRANT_TYPE",
        "OAUTH_MAX_RETRIES",
        "OAUTH_RETRY_DELAY",
    ):
        put(out, values, key)

    out["POSTGRES_HOST"] = first(values, "POSTGRES_HOST", "DB_HOST", default="127.0.0.1")
    out["POSTGRES_PORT"] = first(values, "POSTGRES_PORT", "DB_PORT", default="5432")
    out["POSTGRES_DATABASE"] = first(values, "POSTGRES_DATABASE", "DB_NAME", default="postgres")
    out["POSTGRES_USER"] = first(values, "POSTGRES_USER", "DB_USER", default="postgres")
    out["POSTGRES_PASSWORD"] = first(values, "POSTGRES_PASSWORD", "DB_PASSWORD")
    out["S3_REPORTS_BASE_URL"] = values.get("S3_REPORTS_BASE_URL", "")

    out["LLM_BASE_URL"] = first(values, "LLM_BASE_URL", "LLM_DEFAULT_URL", default="https://api.openai.com/v1")
    agent_overrides = {
        "LLM_TEMPERATURE_SMALL": ("AGENT_LLM_TEMPERATURE_SMALL", "0.3"),
        "LLM_TEMPERATURE_MEDIUM": ("AGENT_LLM_TEMPERATURE_MEDIUM", "0.5"),
        "LLM_TEMPERATURE_LARGE": ("AGENT_LLM_TEMPERATURE_LARGE", "0.7"),
    }
    for key in (
        "LLM_MODEL_SMALL",
        "LLM_TEMPERATURE_SMALL",
        "LLM_MAX_TOKENS_SMALL",
        "LLM_TIMEOUT_SMALL",
        "LLM_MAX_RETRIES_SMALL",
        "LLM_COST_INPUT_SMALL",
        "LLM_COST_OUTPUT_SMALL",
        "LLM_MODEL_MEDIUM",
        "LLM_TEMPERATURE_MEDIUM",
        "LLM_MAX_TOKENS_MEDIUM",
        "LLM_TIMEOUT_MEDIUM",
        "LLM_MAX_RETRIES_MEDIUM",
        "LLM_COST_INPUT_MEDIUM",
        "LLM_COST_OUTPUT_MEDIUM",
        "LLM_MODEL_LARGE",
        "LLM_TEMPERATURE_LARGE",
        "LLM_MAX_TOKENS_LARGE",
        "LLM_TIMEOUT_LARGE",
        "LLM_MAX_RETRIES_LARGE",
        "LLM_COST_INPUT_LARGE",
        "LLM_COST_OUTPUT_LARGE",
        "LLM_EMBEDDING_MODEL",
        "LLM_EMBEDDING_DIMENSIONS",
        "LLM_EMBEDDING_TIMEOUT",
        "LLM_EMBEDDING_MAX_RETRIES",
        "LLM_EMBEDDING_COST_INPUT",
    ):
        if key in agent_overrides:
            override_key, default = agent_overrides[key]
            out[key] = first(values, override_key, key, default=default)
        else:
            put(out, values, key)

    return out


def pipeline_env(
    values: Mapping[str, str],
    metadata: Mapping[str, str],
) -> OrderedDict[str, str]:
    out: OrderedDict[str, str] = OrderedDict()

    for key in (
        "LOG_LEVEL",
        "ENVIRONMENT",
        "OUTPUT_LOGS",
        "LLM_ENDPOINT_MODE",
        "LLM_DEFAULT_URL",
        "LLM_BASE_URL",
        "LLM_MODEL_SMALL",
        "LLM_TEMPERATURE_SMALL",
        "LLM_REASONING_EFFORT_SMALL",
        "LLM_MAX_TOKENS_SMALL",
        "LLM_TIMEOUT_SMALL",
        "LLM_COST_INPUT_SMALL",
        "LLM_COST_OUTPUT_SMALL",
        "LLM_MODEL_LARGE",
        "LLM_TEMPERATURE_LARGE",
        "LLM_REASONING_EFFORT_LARGE",
        "LLM_MAX_TOKENS_LARGE",
        "LLM_TIMEOUT_LARGE",
        "LLM_COST_INPUT_LARGE",
        "LLM_COST_OUTPUT_LARGE",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_BATCH_SIZE",
        "EMBEDDING_PROGRESS_LOG_INTERVAL_SECONDS",
        "LLM_EMBEDDING_MODEL",
        "LLM_EMBEDDING_DIMENSIONS",
        "LLM_EMBEDDING_TIMEOUT",
        "LLM_EMBEDDING_COST_INPUT",
    ):
        put(out, values, key)

    out["LLM_AUTH_MODE"] = normalize_pipeline_auth_mode(values)
    out["OPENAI_API_KEY"] = first(values, "OPENAI_API_KEY", "API_KEY")
    for key in ("OAUTH_ENDPOINT", "OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "OAUTH_GRANT_TYPE"):
        put(out, values, key)
    out["SSL_VERIFY"] = values.get("SSL_VERIFY", "false")

    out["DATA_SOURCE"] = metadata["data_source"]
    out["INPUT_SOURCE"] = values.get("INPUT_SOURCE", "local")
    out["INPUT_BASE_PATH"] = str((PROJECT_ROOT / metadata["input_dir"]).resolve())
    out["OUTPUT_SOURCE"] = values.get("OUTPUT_SOURCE", "local")
    out["OUTPUT_BASE_PATH"] = values.get("OUTPUT_BASE_PATH", "")

    for key in ("NAS_USERNAME", "NAS_PASSWORD", "NAS_SERVER_IP", "NAS_SERVER_NAME", "NAS_SHARE_NAME", "NAS_PORT"):
        put(out, values, key)

    out["DB_HOST"] = first(values, "DB_HOST", "POSTGRES_HOST", default="127.0.0.1")
    out["DB_PORT"] = first(values, "DB_PORT", "POSTGRES_PORT", default="5432")
    out["DB_NAME"] = first(values, "DB_NAME", "POSTGRES_DATABASE", default="postgres")
    out["DB_USER"] = first(values, "DB_USER", "POSTGRES_USER", default="postgres")
    out["DB_PASSWORD"] = first(values, "DB_PASSWORD", "POSTGRES_PASSWORD")
    out["DB_SCHEMA"] = values.get("DB_SCHEMA", "public")
    out["MASTER_DATA_TABLE_NAME"] = metadata["master_table"]

    for key in (
        "VISION_DPI_SCALE",
        "EXTRACTION_PAGE_WORKERS",
        "PDF_RAW_OCR_MODEL",
        "PDF_RAW_OCR_DETAIL",
        "PDF_RAW_OCR_REASONING_EFFORT",
        "PDF_RAW_OCR_MAX_OUTPUT_TOKENS",
        "EXTRACTION_REGION_WORKERS",
        "PDF_VISION_MAX_RETRIES",
        "PDF_VISION_RETRY_DELAY_SECONDS",
        "PDF_CROP_PADDING",
        "PDF_BOUNDARY_RETRY_ENABLED",
        "PDF_BOUNDARY_RETRY_PADDING",
        "PDF_BOUNDARY_RETRY_EXTERNAL_LABEL_PADDING",
        "PDF_BOUNDARY_RETRY_MAX_ATTEMPTS",
        "EXTRACTION_PROGRESS_LOG_INTERVAL_SECONDS",
        "DOC_METADATA_CONTEXT_BUDGET",
        "CONTENT_EXTRACTION_BATCH_BUDGET",
        "SECTION_SUMMARY_BATCH_BUDGET",
        "DOC_METADATA_MAX_RETRIES",
        "DOC_METADATA_RETRY_DELAY_SECONDS",
        "CONTENT_EXTRACTION_MAX_RETRIES",
        "CONTENT_EXTRACTION_RETRY_DELAY_SECONDS",
        "SECTION_SUMMARY_MAX_RETRIES",
        "SECTION_SUMMARY_RETRY_DELAY_SECONDS",
        "ENRICHMENT_WORKBOOK_WORKERS",
        "ENRICHMENT_SHEET_WORKERS",
        "ENRICHMENT_LLM_WORKERS",
        "ENRICHMENT_PROGRESS_LOG_INTERVAL_SECONDS",
    ):
        put(out, values, key)

    return out


def write_env(path: Path, values: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated by scripts/sync_env.py from the root .env.",
        "# Do not commit this file.",
        "",
    ]
    lines.extend(f"{key}={format_env_value(value)}" for key, value in values.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_env_value(value: str) -> str:
    if value == "":
        return ""
    if any(char.isspace() for char in value) or "#" in value:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


if __name__ == "__main__":
    raise SystemExit(main())
