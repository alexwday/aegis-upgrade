"""PostgreSQL connection helpers for source pipeline modules."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
from psycopg2.extensions import connection as PsycopgConnection

from utils.config_setup import get_database_config, get_database_schema
from utils.source_context import get_source_context

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 5


def get_connection() -> PsycopgConnection:
    """Open a live PostgreSQL connection using source .env settings.

    The caller owns closing the returned connection. Configuration and psycopg2
    exceptions are allowed to propagate so startup can fail visibly.
    """
    config = get_database_config()
    return psycopg2.connect(
        **config,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        application_name=f"aegis-{get_source_context().application_slug}-database",
    )


@contextmanager
def connection_scope() -> Iterator[PsycopgConnection]:
    """Yield a PostgreSQL connection and close it when the block exits."""
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def check_connection() -> dict[str, Any]:
    """Run a live PostgreSQL health check and return database metadata.

    The check verifies connectivity, reads basic server identity fields, and
    reports whether the configured schema exists. It raises on connection or
    query failures.
    """
    schema = get_database_schema()
    with connection_scope() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    current_user,
                    current_database(),
                    host(inet_server_addr()),
                    inet_server_port(),
                    current_setting('server_version')
                """)
            user, database, host, port, server_version = cur.fetchone()

            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.schemata
                    WHERE schema_name = %s
                )
                """,
                (schema,),
            )
            schema_exists = bool(cur.fetchone()[0])

    return {
        "user": user,
        "database": database,
        "host": host,
        "port": port,
        "server_version": server_version,
        "schema": schema,
        "schema_exists": schema_exists,
    }


def verify_connection() -> bool:
    """Return True when the PostgreSQL health check completes without error."""
    check_connection()
    logger.debug("PostgreSQL connection health check passed")
    return True
