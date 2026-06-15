"""
Connections module exports.
"""

# OAuth exports
from .oauth_connector import setup_authentication, get_oauth_token

# Postgres exports
from .postgres_connector import (
    get_connection,
    close_all_connections,
    insert_record,
    insert_many,
    update_record,
    delete_record,
    fetch_one,
    fetch_all,
    execute_query,
    table_exists,
    get_table_schema,
)

# LLM exports
from .llm_connector import (
    complete,
    stream,
    complete_with_tools,
    embed,
    embed_batch,
    check_connection,
)

__all__ = [
    # OAuth
    "setup_authentication",
    "get_oauth_token",
    # Postgres
    "get_connection",
    "close_all_connections",
    "insert_record",
    "insert_many",
    "update_record",
    "delete_record",
    "fetch_one",
    "fetch_all",
    "execute_query",
    "table_exists",
    "get_table_schema",
    # LLM
    "complete",
    "stream",
    "complete_with_tools",
    "embed",
    "embed_batch",
    "check_connection",
]
