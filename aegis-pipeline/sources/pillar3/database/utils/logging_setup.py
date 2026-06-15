"""Logging helpers for the database module."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = MODULE_ROOT.parent
DEFAULT_LOGS_DIR = MODULE_ROOT / "logs"
DEFAULT_LOGGER_NAME = ""
HANDLER_MARKER = "_aegis_pillar3_handler"

STAGE_WIDTH = 16
SOURCE_WIDTH = 36

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
YELLOW = "\033[33m"
WHITE = "\033[37m"
BRIGHT_RED = "\033[91m"
BRIGHT_YELLOW = "\033[93m"
BRIGHT_CYAN = "\033[96m"

NOISY_LOGGERS = (
    "httpcore",
    "httpx",
    "openai",
    "urllib3",
)

SECRET_PATTERNS = (
    re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|client[_-]?secret|password)"
        r"([\"']?\s*[:=]\s*[\"']?)([^\"'\s,;]+)"
    ),
)

_configured = False
_current_log_file: Path | None = None


class RedactingFilter(logging.Filter):
    """Redact common secret-bearing key/value pairs from log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact supported secret patterns and keep the record enabled."""
        message = record.getMessage()
        redacted = _redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class ConsoleFormatter(logging.Formatter):
    """Compact colored formatter for local CLI runs."""

    LEVEL_COLORS = {
        logging.DEBUG: DIM,
        logging.INFO: WHITE,
        logging.WARNING: BRIGHT_YELLOW,
        logging.ERROR: BRIGHT_RED,
        logging.CRITICAL: BOLD + BRIGHT_RED,
    }

    def __init__(self, use_color: bool = True):
        """Create a console formatter with optional ANSI color output."""
        super().__init__()
        self.use_color = use_color

    def formatTime(self, record, datefmt=None):
        """Format record timestamps in local time for console output."""
        created = datetime.fromtimestamp(record.created)
        if datefmt:
            return created.strftime(datefmt)
        return created.strftime("%Y-%m-%d %H:%M:%S")

    def format(self, record):
        """Return one compact console log line, including exceptions."""
        timestamp = self.formatTime(record)
        stage = getattr(record, "stage", "SYSTEM")
        source = _short_source_path(record.pathname)
        message = record.getMessage()

        if not self.use_color:
            line = (
                f"{timestamp} | {stage:<{STAGE_WIDTH}} | "
                f"{source:<{SOURCE_WIDTH}} | {message}"
            )
        else:
            color = self.LEVEL_COLORS.get(record.levelno, WHITE)
            line = (
                f"{DIM}{timestamp}{RESET} | "
                f"{BOLD}{BRIGHT_CYAN}{stage:<{STAGE_WIDTH}}{RESET} | "
                f"{YELLOW}{source:<{SOURCE_WIDTH}}{RESET} | "
                f"{color}{message}{RESET}"
            )

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class FileFormatter(logging.Formatter):
    """Plain formatter with full file and line detail."""

    def formatTime(self, record, datefmt=None):
        """Format record timestamps with millisecond precision."""
        created = datetime.fromtimestamp(record.created)
        if datefmt:
            return created.strftime(datefmt)
        base = created.strftime("%Y-%m-%d %H:%M:%S")
        return f"{base}.{int(record.msecs):03d}"

    def format(self, record):
        """Return one file log line with level, stage, source, and message."""
        timestamp = self.formatTime(record)
        stage = getattr(record, "stage", "SYSTEM")
        source = f"{_short_source_path(record.pathname)}:{record.lineno}"
        line = (
            f"{timestamp} | {record.levelname:<8} | "
            f"{stage:<{STAGE_WIDTH}} | {source:<42} | "
            f"{record.getMessage()}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(
    console_level: int | str = logging.INFO,
    file_level: int | str = logging.DEBUG,
    log_dir: Path | str | None = None,
    logger_name: str = DEFAULT_LOGGER_NAME,
    enable_file: bool = True,
    force: bool = False,
) -> Path | None:
    """Configure application logging and return the active log file path.

    By default this configures the root logger so database package loggers and
    third-party SDK loggers are both captured. Repeated calls are idempotent
    unless ``force=True``.
    """
    global _configured, _current_log_file

    if _configured and not force:
        return _current_log_file

    console_level_value = _coerce_level(console_level)
    file_level_value = _coerce_level(file_level)
    logger_level = min(console_level_value, file_level_value)
    app_logger = logging.getLogger(logger_name)
    app_logger.setLevel(logger_level)

    _remove_owned_handlers(app_logger)

    redacting_filter = RedactingFilter()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level_value)
    console.setFormatter(ConsoleFormatter(use_color=sys.stdout.isatty()))
    console.addFilter(redacting_filter)
    _mark_handler(console)
    app_logger.addHandler(console)

    log_file = None
    if enable_file:
        logs_dir = Path(log_dir) if log_dir is not None else DEFAULT_LOGS_DIR
        logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_file = logs_dir / f"database_{timestamp}.log"

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(file_level_value)
        file_handler.setFormatter(FileFormatter())
        file_handler.addFilter(redacting_filter)
        _mark_handler(file_handler)
        app_logger.addHandler(file_handler)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    _configured = True
    _current_log_file = log_file
    return log_file


def get_stage_logger(name: str, stage: str) -> logging.LoggerAdapter:
    """Return a logger adapter with a stage label attached."""
    return logging.LoggerAdapter(logging.getLogger(name), {"stage": stage})


def _short_source_path(pathname: str) -> str:
    """Return a repo-relative source path when possible."""
    path = Path(pathname)
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return path.name


def _coerce_level(level: int | str) -> int:
    """Convert a logging level name or integer into a logging level value."""
    if isinstance(level, int):
        return level
    value = logging.getLevelName(level.upper())
    if isinstance(value, int):
        return value
    raise ValueError(f"Unknown logging level: {level!r}")


def _remove_owned_handlers(logger: logging.Logger) -> None:
    """Close handlers previously installed by this module."""
    for handler in logger.handlers[:]:
        if getattr(handler, HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()


def _mark_handler(handler: logging.Handler) -> None:
    """Mark a handler so later setup calls can replace it safely."""
    setattr(handler, HANDLER_MARKER, True)


def _redact(message: str) -> str:
    """Return a log message with configured secret values redacted."""
    redacted = message
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\1\2<redacted>", redacted)
    return redacted
