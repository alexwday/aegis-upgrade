"""
Logging configuration module.

This module provides structured logging setup with colored console output
and configurable log levels from environment variables.
"""

import logging
import sys
from typing import Any, Dict, Optional

import structlog

from .settings import config


def custom_renderer(_, __, event_dict: Dict[str, Any]) -> str:
    """
    Custom renderer for clean console output.

    Formats log messages with colored level indicators, timestamps,
    and key-value context data.

    Args:
        _: Unused logger parameter (required by structlog interface).
        __: Unused method name parameter (required by structlog interface).
        event_dict: Dictionary containing log event data.

    Returns:
        Formatted log string for console output.
    """
    # Keep internal correlation ids out of console output. They remain available
    # in-process for workflow plumbing, but they add noise during local ETL runs.
    event_dict.pop("execution_id", None)

    # Extract components
    timestamp = event_dict.pop("timestamp", "")
    level = event_dict.pop("level", "").upper()
    event = event_dict.pop("event", "")

    # Level styling with icons and colors
    level_styles = {
        "DEBUG": ("🔍", "\033[36m"),  # Cyan with magnifying glass
        "INFO": ("✓", "\033[32m"),  # Green with checkmark
        "WARNING": ("⚠", "\033[33m"),  # Yellow with warning sign
        "ERROR": ("✗", "\033[31m"),  # Red with X
        "CRITICAL": ("🔥", "\033[35m"),  # Magenta with fire
    }

    # ANSI codes
    reset = "\033[0m"
    dim = "\033[2m"
    bold = "\033[1m"

    # Get style for level
    icon, color = level_styles.get(level, ("•", ""))

    # Format timestamp with dim style
    time_str = f"{dim}{timestamp}{reset}"

    # Format level with icon
    level_str = f"{color}{bold}{icon} {level}{reset}"

    # Build output with arrow separator
    output = f"{time_str} {level_str} ▸ {event}"

    # Add context if any
    if event_dict:
        context = " ".join([f"{dim}{k}={reset}{v}" for k, v in event_dict.items()])
        output += f" {dim}│{reset} " + context

    return output


def setup_logging(log_level: Optional[str] = None) -> None:
    """
    Configure structlog for console output with nice formatting.

    Sets up both Python's standard logging and structlog with:
    - Colored console output with icons for each log level
    - Timestamp formatting (YYYY-MM-DD HH:MM:SS)
    - Contextual key-value pair display
    - Log level from environment (LOG_LEVEL) or parameter

    Args:
        log_level: Log level override. If None, uses LOG_LEVEL from environment
                  (default: INFO). Valid: DEBUG, INFO, WARNING, ERROR, CRITICAL
    """
    # Use environment variable if log_level not specified
    if log_level is None:
        log_level = config.log_level

    # Configure Python's logging
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, log_level.upper())
    )

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
            custom_renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, log_level.upper())),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger() -> structlog.BoundLogger:
    """
    Get a logger instance.

    Returns a structlog logger that outputs formatted messages with:
    - Colored level indicators (🔍 DEBUG, ✓ INFO, ⚠ WARNING, ✗ ERROR, 🔥 CRITICAL)
    - Timestamps and contextual data
    - Thread-safe context variables

    Returns:
        Configured structlog.BoundLogger instance

        # Usage: logger.info("Message", key1="value1", key2="value2")
        # Output: 2024-01-01 12:00:00 ✓ INFO ▸ Message │ key1=value1 key2=value2
    """
    return structlog.get_logger()
