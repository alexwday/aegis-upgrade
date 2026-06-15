"""Configuration helpers for the database module."""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

MODULE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = MODULE_ROOT.parent
ENV_PATH = MODULE_ROOT / ".env"
DEFAULT_LOCAL_INPUT_PATH = Path("/Users/alexwday/Projects/aegis-upgrade/aegis-documents/supplementary_financials")
DEFAULT_LOCAL_OUTPUT_PATH = REPO_ROOT / "data-output" / "master"
DEFAULT_DATA_SOURCE = "financial-supp"
TOKENIZER_CACHE_PATH = REPO_ROOT / "tokenizer-cache"

ENDPOINT_MODES = {"default", "internal"}
AUTH_MODES = {"default", "oauth"}
SOURCE_OPTIONS = {"local", "nas"}
REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh", "minimal"}
MODEL_SIZES = {"small", "large"}
POSTGRES_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

STAGE_MODEL_ALIASES = {
    "content_extraction": "large",
    "default": "small",
    "doc_metadata": "large",
    "enrichment": "large",
    "section_summary": "large",
    "startup": "small",
    "small": "small",
    "large": "large",
}


@dataclass(frozen=True)
class LLMModelConfig:
    """Configuration for one chat model size."""

    model: str
    max_tokens: int
    temperature: float | None
    reasoning_effort: str | None
    timeout: float
    cost_input: float | None
    cost_output: float | None


@dataclass(frozen=True)
class OAuthConfig:
    """OAuth client-credentials configuration."""

    token_endpoint: str
    client_id: str
    client_secret: str
    grant_type: str


@dataclass(frozen=True)
class InputSourceConfig:
    """Local or NAS input source configuration."""

    source: str
    base_path: Path | str
    nas: dict[str, Any]


@dataclass(frozen=True)
class OutputSourceConfig:
    """Local or NAS output source configuration."""

    source: str
    base_path: Path | str
    nas: dict[str, Any]


@dataclass(frozen=True)
class StageRetryConfig:
    """Retry behavior for one LLM enrichment operation."""

    max_retries: int
    retry_delay_seconds: float


@dataclass(frozen=True)
class EnrichmentBudgetConfig:
    """Token budgets used by the XLSX enrichment stage."""

    doc_metadata_context: int
    content_extraction_batch: int
    section_summary_batch: int


@dataclass(frozen=True)
class EnrichmentRetryConfig:
    """Retry settings grouped by enrichment operation."""

    doc_metadata: StageRetryConfig
    content_extraction: StageRetryConfig
    section_summary: StageRetryConfig


@dataclass(frozen=True)
class EnrichmentConfig:
    """Runtime controls for LLM enrichment.

    ``max_workbook_workers`` bounds concurrent workbook pipelines,
    ``max_sheet_workers`` bounds in-workbook batch fan-out, and
    ``max_parallel_enrichment_calls`` bounds live LLM calls across all
    workbooks.
    """

    budgets: EnrichmentBudgetConfig
    retries: EnrichmentRetryConfig
    max_workbook_workers: int = 3
    max_sheet_workers: int = 3
    max_parallel_enrichment_calls: int = 3
    progress_log_interval_seconds: float = 30.0


@dataclass(frozen=True)
class EmbeddingConfig:
    """Runtime controls for final embedding generation.

    ``model`` and ``dimensions`` are passed to the embedding endpoint. The
    ``batch_size`` setting bounds how many texts are submitted in one live
    embedding request. ``progress_log_interval_seconds`` controls aggregate
    progress snapshots while API batches complete.
    """

    model: str = "text-embedding-3-large"
    dimensions: int = 3072
    batch_size: int = 100
    progress_log_interval_seconds: float = 30.0


def load_config(env_path: Path | None = None, override: bool = False) -> None:
    """Load this module's .env file into process environment.

    Existing process environment values win by default. This lets a global
    OPENAI_API_KEY remain active even when database/.env contains a blank value.
    """
    path = env_path or ENV_PATH
    if path.exists():
        for key, value in _read_env_file(path).items():
            if override or key not in os.environ:
                os.environ[key] = value
    ensure_tokenizer_cache_env()


def ensure_tokenizer_cache_env() -> Path:
    """Set and return the local tiktoken cache directory.

    The cache directory is repo-local so token counting can run deterministically
    without fetching tokenizer assets at runtime. An existing process-level
    ``TIKTOKEN_CACHE_DIR`` value is preserved for explicit overrides.
    """
    if not os.getenv("TIKTOKEN_CACHE_DIR"):
        os.environ["TIKTOKEN_CACHE_DIR"] = str(TOKENIZER_CACHE_PATH)
    return Path(os.environ["TIKTOKEN_CACHE_DIR"]).expanduser().resolve()


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse dotenv-style key/value pairs without mutating the environment.

    Blank lines, comments, and malformed lines are ignored. Values are cleaned
    with the local quote and inline-comment rules before being returned.
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _clean_env_value(raw_value)
    return values


def _clean_env_value(raw_value: str) -> str:
    """Normalize one raw dotenv value while preserving quoted content."""
    value = raw_value.strip()
    if not value:
        return ""

    quote = value[0] if value[0] in {"'", '"'} else ""
    if quote:
        end_index = _find_closing_quote(value, quote)
        if end_index is not None:
            return value[1:end_index]

    return _strip_inline_comment(value).strip()


def _find_closing_quote(value: str, quote: str) -> int | None:
    """Return the closing quote index, ignoring escaped quote characters."""
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


def _strip_inline_comment(value: str) -> str:
    """Remove unquoted inline comments from a dotenv value."""
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


def _env(name: str, default: str = "", required: bool = False) -> str:
    """Return a stripped environment value or raise when required and blank."""
    value = os.getenv(name, default).strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    return value


def _choice(name: str, allowed: set[str], default: str = "") -> str:
    """Return a lower-cased environment choice validated against allowed."""
    value = _env(name, default).lower()
    if value not in allowed:
        allowed_values = ", ".join(sorted(allowed))
        raise ValueError(f"{name} must be one of: {allowed_values}; got {value!r}")
    return value


def _bool(name: str, default: bool = False) -> bool:
    """Parse a boolean environment value using common true/false spellings."""
    raw = _env(name, str(default).lower()).lower()
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"{name} must be true or false; got {raw!r}")


def _int(name: str, default: str | None = None, minimum: int | None = None) -> int:
    """Parse an integer environment value and enforce an optional minimum."""
    raw = _env(name, default or "", required=default is None)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}; got {value}")
    return value


def _float(
    name: str,
    default: str | None = None,
    minimum: float | None = None,
    required: bool = False,
) -> float | None:
    """Parse a float environment value, returning None for optional blanks."""
    raw = _env(name, default or "", required=required)
    if not raw and not required:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}; got {value}")
    return value


def get_llm_endpoint_mode() -> str:
    """Return endpoint mode: default or internal."""
    return _choice("LLM_ENDPOINT_MODE", ENDPOINT_MODES, "default")


def get_llm_endpoint() -> str:
    """Return the active OpenAI-compatible base URL."""
    mode = get_llm_endpoint_mode()
    if mode == "default":
        return _env("LLM_DEFAULT_URL", required=True).rstrip("/")
    return _env("LLM_BASE_URL", required=True).rstrip("/")


def get_llm_auth_mode() -> str:
    """Return auth mode from env: default or oauth."""
    return _choice("LLM_AUTH_MODE", AUTH_MODES, "default")


def get_api_key() -> str:
    """Return OPENAI_API_KEY for default auth mode."""
    return _env("OPENAI_API_KEY", required=True)


def get_oauth_config() -> dict[str, str]:
    """Return OAuth config using the env names in database/.env."""
    config = OAuthConfig(
        token_endpoint=_env("OAUTH_ENDPOINT", required=True),
        client_id=_env("OAUTH_CLIENT_ID", required=True),
        client_secret=_env("OAUTH_CLIENT_SECRET", required=True),
        grant_type=_env("OAUTH_GRANT_TYPE", "client_credentials"),
    )
    return asdict(config)


def get_ssl_verify() -> bool:
    """Return whether SSL verification/RBC cert setup should be enabled."""
    return _bool("SSL_VERIFY", default=False)


def get_log_output_enabled() -> bool:
    """Return whether startup should write logs to files."""
    return _bool("OUTPUT_LOGS", default=True)


def get_stage_model_config(stage: str) -> dict[str, Any]:
    """Return model config for small or large model usage.

    stage may be "small" or "large". The startup health check maps to the
    small model configuration.
    """
    size = _resolve_model_size(stage)
    config = get_llm_model_config(size)
    return asdict(config)


def get_llm_model_config(size: str) -> LLMModelConfig:
    """Return typed model configuration for small or large."""
    resolved = _resolve_model_size(size)
    suffix = resolved.upper()
    reasoning_effort = _env(f"LLM_REASONING_EFFORT_{suffix}")
    if reasoning_effort and reasoning_effort not in REASONING_EFFORTS:
        allowed = ", ".join(sorted(REASONING_EFFORTS))
        raise ValueError(
            f"LLM_REASONING_EFFORT_{suffix} must be one of: {allowed}; "
            f"got {reasoning_effort!r}"
        )

    return LLMModelConfig(
        model=_env(f"LLM_MODEL_{suffix}", required=True),
        temperature=_float(f"LLM_TEMPERATURE_{suffix}", minimum=0.0),
        reasoning_effort=reasoning_effort or None,
        max_tokens=_int(f"LLM_MAX_TOKENS_{suffix}", minimum=1),
        timeout=_float(f"LLM_TIMEOUT_{suffix}", minimum=0.0, required=True) or 0.0,
        cost_input=_float(f"LLM_COST_INPUT_{suffix}", minimum=0.0),
        cost_output=_float(f"LLM_COST_OUTPUT_{suffix}", minimum=0.0),
    )


def get_enrichment_config() -> EnrichmentConfig:
    """Return enrichment budgets and retry settings from environment values."""
    return EnrichmentConfig(
        budgets=EnrichmentBudgetConfig(
            doc_metadata_context=_int("DOC_METADATA_CONTEXT_BUDGET", "30000", 1),
            content_extraction_batch=_int(
                "CONTENT_EXTRACTION_BATCH_BUDGET",
                "30000",
                1,
            ),
            section_summary_batch=_int("SECTION_SUMMARY_BATCH_BUDGET", "30000", 1),
        ),
        retries=EnrichmentRetryConfig(
            doc_metadata=_stage_retry_config("DOC_METADATA"),
            content_extraction=_stage_retry_config("CONTENT_EXTRACTION"),
            section_summary=_stage_retry_config("SECTION_SUMMARY"),
        ),
        max_workbook_workers=_int("ENRICHMENT_WORKBOOK_WORKERS", "3", 1),
        max_sheet_workers=_int("ENRICHMENT_SHEET_WORKERS", "3", 1),
        max_parallel_enrichment_calls=_int("ENRICHMENT_LLM_WORKERS", "3", 1),
        progress_log_interval_seconds=_float(
            "ENRICHMENT_PROGRESS_LOG_INTERVAL_SECONDS",
            "30.0",
            0.0,
            required=True,
        )
        or 0.0,
    )


def get_embedding_config() -> EmbeddingConfig:
    """Return final embedding settings from environment values."""
    return EmbeddingConfig(
        model=_env("EMBEDDING_MODEL", "text-embedding-3-large"),
        dimensions=_int("EMBEDDING_DIMENSIONS", "3072", 1),
        batch_size=_int("EMBEDDING_BATCH_SIZE", "100", 1),
        progress_log_interval_seconds=_float(
            "EMBEDDING_PROGRESS_LOG_INTERVAL_SECONDS",
            "30.0",
            0.0,
            required=True,
        )
        or 0.0,
    )


def get_master_data_table_name() -> str:
    """Return the PostgreSQL table name used for the master data CSV.

    ``MASTER_DATA_TABLE_NAME`` may override the default. When it is omitted,
    the table name is derived from DATA_SOURCE and suffixed with
    ``_master_data``. The returned value is also used as the CSV base filename,
    so it is restricted to a portable PostgreSQL identifier.
    """
    configured_name = _env("MASTER_DATA_TABLE_NAME")
    table_name = (
        configured_name
        if configured_name
        else f"{_database_identifier(get_data_source())}_master_data"
    )
    if not POSTGRES_IDENTIFIER_PATTERN.fullmatch(table_name):
        raise ValueError(
            "MASTER_DATA_TABLE_NAME must be a PostgreSQL identifier containing "
            "only letters, numbers, and underscores, and it must not start "
            f"with a number; got {table_name!r}"
        )
    return table_name


def _stage_retry_config(prefix: str) -> StageRetryConfig:
    """Return retry config for one enrichment operation prefix."""
    return StageRetryConfig(
        max_retries=_int(f"{prefix}_MAX_RETRIES", "3", 1),
        retry_delay_seconds=_float(
            f"{prefix}_RETRY_DELAY_SECONDS",
            "2.0",
            0.0,
            required=True,
        )
        or 0.0,
    )


def _resolve_model_size(stage: str) -> str:
    """Resolve a stage alias to a supported model size."""
    key = stage.strip().lower()
    resolved = STAGE_MODEL_ALIASES.get(key, key)
    if resolved not in MODEL_SIZES:
        allowed = ", ".join(sorted(STAGE_MODEL_ALIASES))
        raise ValueError(f"Unknown model stage {stage!r}. Use one of: {allowed}")
    return resolved


def _database_identifier(value: str) -> str:
    """Return a lowercase identifier fragment from a source label."""
    identifier = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    if not identifier:
        return "data"
    if identifier[0].isdigit():
        return f"data_{identifier}"
    return identifier


def get_input_source() -> str:
    """Return input source: local or nas."""
    return _choice("INPUT_SOURCE", SOURCE_OPTIONS, "local")


def get_output_source() -> str:
    """Return output source: local or nas."""
    return _choice("OUTPUT_SOURCE", SOURCE_OPTIONS, "local")


def get_data_source() -> str:
    """Return the logical data-source label used in master manifest records."""
    data_source = _env("DATA_SOURCE", DEFAULT_DATA_SOURCE)
    if not data_source:
        raise ValueError("DATA_SOURCE is required")
    return data_source


def get_base_path() -> Path | str:
    """Return local or NAS base path.

    For local input, a blank INPUT_BASE_PATH defaults to the centralized
    aegis-documents supplementary_financials folder. For NAS input, INPUT_BASE_PATH is the optional
    path prefix inside the share.
    """
    source = get_input_source()
    raw_base_path = _env("INPUT_BASE_PATH")
    if source == "nas":
        return raw_base_path

    path = (
        Path(raw_base_path).expanduser() if raw_base_path else DEFAULT_LOCAL_INPUT_PATH
    )
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    if not path.is_dir():
        raise ValueError(f"INPUT_BASE_PATH is not a directory: {path}")
    return path


def get_output_base_path() -> Path | str:
    """Return local or NAS output base path.

    For local output, a blank OUTPUT_BASE_PATH defaults to repo-root
    data-output/master. The directory is not required to exist because callers
    may create it when writing artifacts. For NAS output, OUTPUT_BASE_PATH is
    the optional path prefix inside the configured share.
    """
    source = get_output_source()
    raw_base_path = _env("OUTPUT_BASE_PATH")
    if source == "nas":
        return raw_base_path

    path = (
        Path(raw_base_path).expanduser() if raw_base_path else DEFAULT_LOCAL_OUTPUT_PATH
    )
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    return path


def get_nas_config() -> dict[str, Any]:
    """Return NAS connection config.

    NAS credentials are required when either input or output source is NAS.
    """
    return _nas_config_for_base_path(_env("INPUT_BASE_PATH"))


def _nas_config_for_base_path(base_path: str) -> dict[str, Any]:
    """Return NAS connection config using the supplied share-relative base path."""
    required = "nas" in {get_input_source(), get_output_source()}
    return {
        "base_path": base_path,
        "username": _env("NAS_USERNAME", required=required),
        "password": _env("NAS_PASSWORD", required=required),
        "server_ip": _env("NAS_SERVER_IP", required=required),
        "server_name": _env("NAS_SERVER_NAME", required=required),
        "share_name": _env("NAS_SHARE_NAME", required=required),
        "port": _int("NAS_PORT", default="445", minimum=1),
    }


def get_input_source_config() -> InputSourceConfig:
    """Return the full input source config."""
    return InputSourceConfig(
        source=get_input_source(),
        base_path=get_base_path(),
        nas=get_nas_config(),
    )


def get_output_source_config() -> OutputSourceConfig:
    """Return the full output source config."""
    return OutputSourceConfig(
        source=get_output_source(),
        base_path=get_output_base_path(),
        nas=_nas_config_for_base_path(_env("OUTPUT_BASE_PATH")),
    )


def get_database_config() -> dict[str, str]:
    """Return PostgreSQL connection parameters when DB env is present."""
    config = {
        "host": _env("DB_HOST", required=True),
        "port": _env("DB_PORT", required=True),
        "dbname": _env("DB_NAME", required=True),
        "user": _env("DB_USER", required=True),
        "password": _env("DB_PASSWORD"),
    }
    return {key: value for key, value in config.items() if value}


def get_database_schema() -> str:
    """Return configured database schema."""
    return _env("DB_SCHEMA", required=True)
