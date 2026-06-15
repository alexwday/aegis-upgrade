"""
Aegis utility modules.

This package contains utility functions and configurations for the Aegis system.
"""

# Import commonly used functions for convenience
from .conversation import process_conversation
from .logging import setup_logging, get_logger
from .monitor import (
    initialize_monitor,
    add_monitor_entry,
    post_monitor_entries,
    get_monitor_entries,
    clear_monitor_entries,
    format_llm_call,
)
from .settings import config
from .ssl import setup_ssl

__all__ = [
    # Conversation
    "process_conversation",
    # Logging
    "setup_logging",
    "get_logger",
    # Monitor
    "initialize_monitor",
    "add_monitor_entry",
    "post_monitor_entries",
    "get_monitor_entries",
    "clear_monitor_entries",
    "format_llm_call",
    # Settings
    "config",
    # SSL
    "setup_ssl",
]
