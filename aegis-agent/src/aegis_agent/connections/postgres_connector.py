"""
PostgreSQL database connector using SQLAlchemy async.

This module provides an async functional interface for PostgreSQL operations
using SQLAlchemy's async support for connection management and query execution.
"""

from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List, Optional

from sqlalchemy import MetaData, Table, delete, insert, text, update
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool, QueuePool

from ..utils.logging import get_logger
from ..utils.settings import config

logger = get_logger()

_async_engine: Optional[AsyncEngine] = None
_async_session_factory: Optional[async_sessionmaker] = None


async def _get_async_engine() -> AsyncEngine:
    """
    Get or create the async SQLAlchemy engine with connection pooling.

    Returns:
        Async SQLAlchemy Engine instance

    Raises:
        SQLAlchemyError: If unable to create engine
    """
    global _async_engine, _async_session_factory  # pylint: disable=global-statement
    # Engine must be global singleton for connection pooling across the application.

    if _async_engine is None:
        try:
            # Use postgresql+asyncpg:// for async connections
            database_url = (
                f"postgresql+asyncpg://{config.postgres_user}:{config.postgres_password}"
                f"@{config.postgres_host}:{config.postgres_port}/{config.postgres_database}"
            )

            _async_engine = create_async_engine(
                database_url,
                pool_size=20,  # Increased for async concurrency
                max_overflow=40,  # Increased for async concurrency
                pool_timeout=30,
                pool_recycle=3600,
                pool_pre_ping=True,
                echo=False,
            )

            _async_session_factory = async_sessionmaker(
                _async_engine,
                class_=AsyncSession,
                expire_on_commit=False,
            )

            logger.info(
                "Async PostgreSQL engine created",
                host=config.postgres_host,
                port=config.postgres_port,
                database=config.postgres_database,
                pool_size=20,
            )
        except SQLAlchemyError as e:
            logger.error(
                "Failed to create async PostgreSQL engine",
                error=str(e),
                host=config.postgres_host,
                port=config.postgres_port,
                database=config.postgres_database,
            )
            raise

    return _async_engine


@asynccontextmanager
async def get_connection(
    execution_id: Optional[str] = None,
) -> AsyncGenerator[AsyncConnection, None]:
    """
    Get an async database connection from the pool.

    Args:
        execution_id: Optional execution ID for logging

    Yields:
        Async SQLAlchemy connection object

    Raises:
        SQLAlchemyError: If unable to get connection
    """
    engine = await _get_async_engine()

    async with engine.begin() as conn:
        try:
            logger.debug("Got async connection from pool", execution_id=execution_id)
            yield conn
        except SQLAlchemyError as e:
            logger.error(
                "Error with async database connection",
                execution_id=execution_id,
                error=str(e),
            )
            raise
        finally:
            logger.debug("Returned async connection to pool", execution_id=execution_id)


async def execute_query(
    query: str,
    params: Optional[Dict[str, Any]] = None,
    execution_id: Optional[str] = None,
) -> Optional[int]:
    """
    Execute a query that doesn't return results (INSERT, UPDATE, DELETE).

    Args:
        query: SQL query to execute
        params: Query parameters as dictionary
        execution_id: Optional execution ID for logging

    Returns:
        Number of affected rows

    Raises:
        SQLAlchemyError: If query execution fails
    """
    async with get_connection(execution_id) as conn:
        try:
            result = await conn.execute(text(query), params or {})
            # No need to commit - using begin() context manager handles it

            logger.debug(
                "Async query executed",
                execution_id=execution_id,
                affected_rows=result.rowcount,
            )

            return result.rowcount
        except SQLAlchemyError as e:
            logger.error(
                "Async query execution failed",
                execution_id=execution_id,
                error=str(e),
                query=query[:500],
            )
            raise


async def fetch_all(
    query: str,
    params: Optional[Dict[str, Any]] = None,
    execution_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Execute a SELECT query and return all results.

    Args:
        query: SQL SELECT query
        params: Query parameters as dictionary
        execution_id: Optional execution ID for logging

    Returns:
        List of dictionaries representing rows

    Raises:
        SQLAlchemyError: If query execution fails
    """
    async with get_connection(execution_id) as conn:
        try:
            result = await conn.execute(text(query), params or {})
            rows = result.fetchall()

            # SQLAlchemy's Row._mapping is the official way to convert to dict
            # It's a public API despite the underscore prefix
            results = [dict(row._mapping) for row in rows]  # pylint: disable=protected-access

            logger.debug(
                "Async fetched all results",
                execution_id=execution_id,
                row_count=len(results),
            )

            return results
        except SQLAlchemyError as e:
            logger.error(
                "Failed to async fetch results",
                execution_id=execution_id,
                error=str(e),
                query=query[:500],
            )
            raise


async def fetch_one(
    query: str,
    params: Optional[Dict[str, Any]] = None,
    execution_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Execute a SELECT query and return the first result.

    Args:
        query: SQL SELECT query
        params: Query parameters as dictionary
        execution_id: Optional execution ID for logging

    Returns:
        Dictionary representing the first row, or None if no results

    Raises:
        SQLAlchemyError: If query execution fails
    """
    async with get_connection(execution_id) as conn:
        try:
            result = await conn.execute(text(query), params or {})
            row = result.fetchone()

            if row:
                logger.debug("Async fetched one result", execution_id=execution_id)
                # SQLAlchemy's Row._mapping is the official way to convert to dict
                # It's a public API despite the underscore prefix
                return dict(row._mapping)  # pylint: disable=protected-access

            logger.debug("No results found", execution_id=execution_id)
            return None
        except SQLAlchemyError as e:
            logger.error(
                "Failed to async fetch result",
                execution_id=execution_id,
                error=str(e),
                query=query[:500],
            )
            raise


async def insert_record(
    table: str,
    data: Dict[str, Any],
    returning: Optional[str] = None,
    execution_id: Optional[str] = None,
) -> Optional[Any]:
    """
    Insert a record into a table.

    Args:
        table: Table name
        data: Dictionary of column names and values
        returning: Optional column to return (e.g., 'log_id')
        execution_id: Optional execution ID for logging

    Returns:
        Value of the returning column if specified, otherwise None

    Raises:
        SQLAlchemyError: If insertion fails
    """
    async with get_connection(execution_id) as conn:
        try:
            metadata = MetaData()
            # In async, we need to run sync_engine.run_sync for reflection
            # or use async reflection if available
            await conn.run_sync(metadata.reflect, only=[table])
            table_obj = metadata.tables[table]

            stmt = insert(table_obj).values(**data)

            if returning:
                stmt = stmt.returning(table_obj.c[returning])
                result = await conn.execute(stmt)
                row = result.fetchone()

                returning_value = row[0] if row else None
                logger.info(
                    "Async record inserted with returning value",
                    execution_id=execution_id,
                    table=table,
                    returning_column=returning,
                    returning_value=returning_value,
                )
                return returning_value

            await conn.execute(stmt)
            logger.info("Async record inserted", execution_id=execution_id, table=table)
            return None
        except SQLAlchemyError as e:
            logger.error(
                "Failed to async insert record",
                execution_id=execution_id,
                table=table,
                error=str(e),
            )
            raise


async def insert_many(
    table: str,
    data_list: List[Dict[str, Any]],
    execution_id: Optional[str] = None,
) -> int:
    """
    Insert multiple records into a table.

    Args:
        table: Table name
        data_list: List of dictionaries with column names and values
        execution_id: Optional execution ID for logging

    Returns:
        Number of inserted records

    Raises:
        SQLAlchemyError: If insertion fails
    """
    if not data_list:
        logger.warning("No data to insert", execution_id=execution_id, table=table)
        return 0

    async with get_connection(execution_id) as conn:
        try:
            metadata = MetaData()
            await conn.run_sync(metadata.reflect, only=[table])
            table_obj = metadata.tables[table]

            stmt = insert(table_obj)
            result = await conn.execute(stmt, data_list)

            logger.info(
                "Async multiple records inserted",
                execution_id=execution_id,
                table=table,
                record_count=result.rowcount,
            )

            return result.rowcount
        except SQLAlchemyError as e:
            logger.error(
                "Failed to async insert multiple records",
                execution_id=execution_id,
                table=table,
                error=str(e),
                record_count=len(data_list),
            )
            raise


async def update_record(
    table: str,
    data: Dict[str, Any],
    where: Dict[str, Any],
    execution_id: Optional[str] = None,
) -> int:
    """
    Update records in a table.

    Args:
        table: Table name
        data: Dictionary of columns to update
        where: Dictionary of WHERE conditions
        execution_id: Optional execution ID for logging

    Returns:
        Number of updated records

    Raises:
        SQLAlchemyError: If update fails
    """
    async with get_connection(execution_id) as conn:
        try:
            metadata = MetaData()
            await conn.run_sync(metadata.reflect, only=[table])
            table_obj = metadata.tables[table]

            stmt = update(table_obj).values(**data)

            for col, val in where.items():
                stmt = stmt.where(table_obj.c[col] == val)

            result = await conn.execute(stmt)

            logger.info(
                "Async records updated",
                execution_id=execution_id,
                table=table,
                affected_rows=result.rowcount,
            )

            return result.rowcount
        except SQLAlchemyError as e:
            logger.error(
                "Failed to async update records",
                execution_id=execution_id,
                table=table,
                error=str(e),
            )
            raise


async def delete_record(
    table: str,
    where: Dict[str, Any],
    execution_id: Optional[str] = None,
) -> int:
    """
    Delete records from a table.

    Args:
        table: Table name
        where: Dictionary of WHERE conditions
        execution_id: Optional execution ID for logging

    Returns:
        Number of deleted records

    Raises:
        SQLAlchemyError: If deletion fails
    """
    async with get_connection(execution_id) as conn:
        try:
            metadata = MetaData()
            await conn.run_sync(metadata.reflect, only=[table])
            table_obj = metadata.tables[table]

            stmt = delete(table_obj)

            for col, val in where.items():
                stmt = stmt.where(table_obj.c[col] == val)

            result = await conn.execute(stmt)

            logger.info(
                "Async records deleted",
                execution_id=execution_id,
                table=table,
                affected_rows=result.rowcount,
            )

            return result.rowcount
        except SQLAlchemyError as e:
            logger.error(
                "Failed to async delete records",
                execution_id=execution_id,
                table=table,
                error=str(e),
            )
            raise


async def table_exists(table: str, execution_id: Optional[str] = None) -> bool:
    """
    Check if a table exists in the database.

    Args:
        table: Table name to check
        execution_id: Optional execution ID for logging

    Returns:
        True if table exists, False otherwise

    Raises:
        SQLAlchemyError: If query fails
    """
    query = """
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_schema = 'public'
            AND table_name = :table_name
        )
    """

    result = await fetch_one(query, {"table_name": table}, execution_id)
    exists = result["exists"] if result else False

    logger.debug(
        "Async table existence check",
        execution_id=execution_id,
        table=table,
        exists=exists,
    )

    return exists


async def get_table_schema(table: str, execution_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Get the schema information for a table.

    Args:
        table: Table name
        execution_id: Optional execution ID for logging

    Returns:
        List of column information dictionaries

    Raises:
        SQLAlchemyError: If query fails
    """
    query = """
        SELECT
            column_name,
            data_type,
            character_maximum_length,
            numeric_precision,
            numeric_scale,
            is_nullable,
            column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
        AND table_name = :table_name
        ORDER BY ordinal_position
    """

    schema = await fetch_all(query, {"table_name": table}, execution_id)

    logger.debug(
        "Retrieved async table schema",
        execution_id=execution_id,
        table=table,
        column_count=len(schema),
    )

    return schema


async def close_all_connections():
    """
    Dispose of the async connection pool and close all connections.

    This should be called when shutting down the application.
    """
    global _async_engine  # pylint: disable=global-statement
    # Need to modify global engine instance to properly dispose of connection pool.

    if _async_engine:
        await _async_engine.dispose()
        _async_engine = None
        logger.info("All async PostgreSQL connections closed")


async def insert_many_async(
    table_name: str,
    records: List[Dict[str, Any]],
    execution_id: Optional[str] = None,
) -> int:
    """
    Insert multiple records into a database table asynchronously.

    Args:
        table_name: Name of the table to insert into
        records: List of dictionaries representing records to insert
        execution_id: Optional execution ID for logging

    Returns:
        Number of rows inserted

    Raises:
        RuntimeError: If database operations fail
    """
    if not records:
        logger.warning("postgres.insert_many_empty", table=table_name, execution_id=execution_id)
        return 0

    try:
        # Get the async engine
        engine = await _get_async_engine()

        async with engine.begin() as conn:
            # Convert dict and list fields to JSON for PostgreSQL columns
            import json

            processed_records = []
            for record in records:
                processed = record.copy()
                for key, value in processed.items():
                    if isinstance(value, (dict, list)):
                        processed[key] = json.dumps(value)
                processed_records.append(processed)

            # Build insert statement using text SQL
            columns = list(processed_records[0].keys())
            placeholders = ", ".join([f":{col}" for col in columns])
            column_names = ", ".join(columns)

            query = text(
                f"""
                INSERT INTO {table_name} ({column_names})
                VALUES ({placeholders})
            """
            )

            # Execute batch insert
            result = await conn.execute(query, processed_records)
            rows_inserted = result.rowcount

            logger.info(
                "postgres.insert_many_success",
                table=table_name,
                rows_inserted=rows_inserted,
                execution_id=execution_id,
            )

            return rows_inserted

    except Exception as e:
        logger.error(
            "postgres.insert_many_error",
            table=table_name,
            record_count=len(records),
            error=str(e),
            execution_id=execution_id,
            exc_info=True,
        )
        raise RuntimeError(f"Failed to insert records into {table_name}: {str(e)}") from e
