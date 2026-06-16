"""Create public PostgreSQL retrieval tables for an Aegis pipeline source.

The script reads database connection settings from the selected source's
``.env`` by default, validates the local master CSV headers against the source
pipeline schemas, and creates two public tables.

Examples:
    venv/bin/python scripts/create_retrieval_tables.py --source investor_slides
    venv/bin/python scripts/create_retrieval_tables.py --source investor_slides --apply
    venv/bin/python scripts/create_retrieval_tables.py --source rts --env-file /path/.env --apply

Run ``--help`` for the supported source names.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import connection as PsycopgConnection

from retrieval_source_config import (  # noqa: E402
    RetrievalSource,
    SourceModules,
    load_source_modules,
    select_source,
    source_keys,
)

PUBLIC_SCHEMA = "public"
DATA_EMBEDDING_COLUMNS = (
    "keyword_embedding",
    "metric_embedding",
    "summary_embedding",
    "chunk_embedding",
)
DATA_SCALAR_COLUMNS = {
    "source_type": "text NOT NULL",
    "fiscal_year": "text NOT NULL",
    "quarter": "text NOT NULL",
    "bank": "text NOT NULL",
    "filename": "text NOT NULL",
    "file_id": "text NOT NULL",
    "file_type": "text NOT NULL",
    "file_path": "text NOT NULL",
    "file_hash": "text NOT NULL",
    "page_number": "integer",
    "name": "text",
    "summary": "text",
    "chunk_id": "text NOT NULL",
    "chunk_content": "text",
    "keywords": "jsonb NOT NULL DEFAULT '[]'::jsonb",
    "metrics": "jsonb NOT NULL DEFAULT '[]'::jsonb",
    "created_at": "timestamptz",
}
EMBEDDINGS_SCALAR_COLUMNS = {
    "embedding_id": "text NOT NULL",
    "embedding_type": "text NOT NULL",
    "embedding_scope": "text NOT NULL",
    "source_type": "text NOT NULL",
    "fiscal_year": "text NOT NULL",
    "quarter": "text NOT NULL",
    "bank": "text NOT NULL",
    "filename": "text NOT NULL",
    "file_id": "text NOT NULL",
    "file_type": "text NOT NULL",
    "file_path": "text NOT NULL",
    "file_hash": "text NOT NULL",
    "content_unit_id": "text",
    "content_unit_ids": "jsonb NOT NULL DEFAULT '[]'::jsonb",
    "chunk_id": "text",
    "section_id": "text",
    "embedding_text": "text NOT NULL",
    "text_hash": "text",
    "embedding_model": "text NOT NULL",
    "embedding_dimensions": "integer NOT NULL",
    "created_at": "timestamptz NOT NULL",
}

MASTER_DATA_FIELDS: tuple[str, ...]
MASTER_EMBEDDINGS_FIELDS: tuple[str, ...]
MASTER_DATA_FILE_NAME: str
MASTER_EMBEDDINGS_FILE_NAME: str
DEFAULT_LOCAL_OUTPUT_PATH: Path
ENV_PATH: Path
get_database_config: Any
get_embedding_config: Any
load_config: Any


@dataclass(frozen=True)
class ScriptConfig:
    """Resolved inputs required to create the retrieval tables."""

    source: RetrievalSource
    env_file: Path
    master_data_csv: Path
    master_embeddings_csv: Path
    data_table: str
    embeddings_table: str
    embedding_storage: str
    embedding_dimensions: int
    apply: bool
    create_vector_extension: bool


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and create or display the table DDL."""
    argv_list = list(sys.argv[1:] if argv is None else argv)
    source = select_source(argv_list)
    _activate_source_modules(load_source_modules(source))
    args = _parse_args(argv_list, source)
    config = _resolve_config(args, source)
    _validate_csv_header(config.master_data_csv, MASTER_DATA_FIELDS, "master data")
    _validate_csv_header(
        config.master_embeddings_csv,
        MASTER_EMBEDDINGS_FIELDS,
        "master embeddings",
    )

    statements = _build_setup_statements(config)
    if not config.apply:
        print(_render_statements(statements))
        print(
            "\nDry run complete. Re-run with --apply to execute against PostgreSQL.",
        )
        return 0

    db_config = get_database_config()
    with psycopg2.connect(
        **db_config,
        application_name=f"aegis-{config.source.application_slug}-create-retrieval-tables",
    ) as conn:
        _execute_statements(conn, statements)

    print(
        "Master tables created: "
        f'{PUBLIC_SCHEMA}."{config.data_table}", '
        f'{PUBLIC_SCHEMA}."{config.embeddings_table}"',
    )
    return 0


def _activate_source_modules(modules: SourceModules) -> None:
    """Bind selected source module constants used by the shared implementation."""
    globals()["MASTER_DATA_FIELDS"] = modules.finalize.MASTER_DATA_FIELDS
    globals()["MASTER_EMBEDDINGS_FIELDS"] = modules.finalize.MASTER_EMBEDDINGS_FIELDS
    globals()["MASTER_EMBEDDINGS_FILE_NAME"] = (
        modules.finalize.MASTER_EMBEDDINGS_FILE_NAME
    )
    globals()["MASTER_DATA_FILE_NAME"] = modules.manifest.MASTER_DATA_FILE_NAME
    globals()["DEFAULT_LOCAL_OUTPUT_PATH"] = (
        modules.config_setup.DEFAULT_LOCAL_OUTPUT_PATH
    )
    globals()["ENV_PATH"] = modules.config_setup.ENV_PATH
    globals()["get_database_config"] = modules.config_setup.get_database_config
    globals()["get_embedding_config"] = modules.config_setup.get_embedding_config
    globals()["load_config"] = modules.config_setup.load_config


def _parse_args(argv: list[str], source: RetrievalSource) -> argparse.Namespace:
    """Return validated command-line arguments."""
    parser = argparse.ArgumentParser(
        description=f"Create public {source.label} retrieval tables.",
    )
    parser.add_argument(
        "--source",
        choices=source_keys(),
        required=True,
        help="Pipeline source whose local package supplies the schema.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=ENV_PATH,
        help="Dotenv file with DB_HOST, DB_PORT, DB_NAME, DB_USER, and DB_PASSWORD.",
    )
    parser.add_argument(
        "--master-data-csv",
        type=Path,
        default=DEFAULT_LOCAL_OUTPUT_PATH / MASTER_DATA_FILE_NAME,
        help="Master data CSV whose header should match the chunk table schema.",
    )
    parser.add_argument(
        "--master-embeddings-csv",
        type=Path,
        default=DEFAULT_LOCAL_OUTPUT_PATH / MASTER_EMBEDDINGS_FILE_NAME,
        help="Master embeddings CSV whose header should match the vector table schema.",
    )
    parser.add_argument(
        "--data-table-name",
        default=source.data_table,
        help=(f"Public chunk table name. Defaults to {source.data_table!r}."),
    )
    parser.add_argument(
        "--embeddings-table-name",
        default=source.embeddings_table,
        help=(
            f"Public embeddings table name. Defaults to "
            f"{source.embeddings_table!r}."
        ),
    )
    parser.add_argument(
        "--embedding-storage",
        choices=("vector", "jsonb", "text"),
        default="vector",
        help="Column type for embedding fields.",
    )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        help="Vector dimensions when --embedding-storage vector is used.",
    )
    parser.add_argument(
        "--skip-vector-extension",
        action="store_true",
        help="Do not run CREATE EXTENSION IF NOT EXISTS vector.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute SQL. Without this flag, the script is a dry run.",
    )
    return parser.parse_args(argv)


def _resolve_config(args: argparse.Namespace, source: RetrievalSource) -> ScriptConfig:
    """Load environment settings and resolve all filesystem and DB targets."""
    env_file = args.env_file.expanduser().resolve()
    if not env_file.is_file():
        raise FileNotFoundError(f"Env file not found: {env_file}")

    master_data_csv = args.master_data_csv.expanduser().resolve()
    if not master_data_csv.is_file():
        raise FileNotFoundError(f"Master data CSV not found: {master_data_csv}")

    master_embeddings_csv = args.master_embeddings_csv.expanduser().resolve()
    if not master_embeddings_csv.is_file():
        raise FileNotFoundError(
            f"Master embeddings CSV not found: {master_embeddings_csv}"
        )

    load_config(env_file, override=True)
    dimensions = args.embedding_dimensions or get_embedding_config().dimensions
    if dimensions < 1:
        raise ValueError("--embedding-dimensions must be a positive integer")

    return ScriptConfig(
        source=source,
        env_file=env_file,
        master_data_csv=master_data_csv,
        master_embeddings_csv=master_embeddings_csv,
        data_table=str(args.data_table_name).strip(),
        embeddings_table=str(args.embeddings_table_name).strip(),
        embedding_storage=args.embedding_storage,
        embedding_dimensions=dimensions,
        apply=args.apply,
        create_vector_extension=not args.skip_vector_extension,
    )


def _validate_csv_header(
    path: Path,
    expected_fields: tuple[str, ...],
    label: str,
) -> None:
    """Reject CSV files whose header does not match the expected schema."""
    with path.open("r", encoding="utf-8", newline="") as file_obj:
        reader = csv.reader(file_obj)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError(f"{label} CSV is empty: {path}") from exc

    expected = list(expected_fields)
    if header != expected:
        raise ValueError(
            f"{label} CSV header does not match expected fields.\n"
            f"Expected: {expected}\n"
            f"Actual:   {header}",
        )


def _build_setup_statements(config: ScriptConfig) -> list[sql.Composable]:
    """Return SQL statements for both public retrieval tables."""
    statements: list[sql.Composable] = []
    if config.embedding_storage == "vector" and config.create_vector_extension:
        statements.append(sql.SQL("CREATE EXTENSION IF NOT EXISTS vector"))

    statements.append(_create_data_table_statement(config))
    statements.append(_create_embeddings_table_statement(config))
    return statements


def _create_data_table_statement(config: ScriptConfig) -> sql.Composable:
    """Return CREATE TABLE DDL for the chunk-level master data table."""
    definitions = []
    for column in MASTER_DATA_FIELDS:
        definitions.append(
            sql.SQL("{} {}").format(
                sql.Identifier(column),
                sql.SQL(_data_column_type(column, config)),
            ),
        )
    definitions.append(sql.SQL("PRIMARY KEY (file_id, chunk_id)"))
    return sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
        _table_ref(config.data_table),
        sql.SQL(", ").join(definitions),
    )


def _create_embeddings_table_statement(config: ScriptConfig) -> sql.Composable:
    """Return CREATE TABLE DDL for the long-form embeddings table."""
    definitions = []
    for column in MASTER_EMBEDDINGS_FIELDS:
        definitions.append(
            sql.SQL("{} {}").format(
                sql.Identifier(column),
                sql.SQL(_embeddings_column_type(column, config)),
            ),
        )
    definitions.append(sql.SQL("PRIMARY KEY (embedding_id)"))
    return sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
        _table_ref(config.embeddings_table),
        sql.SQL(", ").join(definitions),
    )


def _data_column_type(column: str, config: ScriptConfig) -> str:
    """Return a PostgreSQL column type for one master data field."""
    if column in DATA_EMBEDDING_COLUMNS:
        return _embedding_column_type(config)
    return DATA_SCALAR_COLUMNS[column]


def _embeddings_column_type(column: str, config: ScriptConfig) -> str:
    """Return a PostgreSQL column type for one master embeddings field."""
    if column == "embedding":
        return _embedding_column_type(config)
    return EMBEDDINGS_SCALAR_COLUMNS[column]


def _embedding_column_type(config: ScriptConfig) -> str:
    """Return the configured SQL type for vector-like embedding columns."""
    if config.embedding_storage == "vector":
        return f"vector({config.embedding_dimensions})"
    if config.embedding_storage == "jsonb":
        return "jsonb"
    return "text"


def _execute_statements(
    conn: PsycopgConnection,
    statements: list[sql.Composable],
) -> None:
    """Execute DDL statements in one transaction."""
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)


def _render_statements(statements: list[sql.Composable]) -> str:
    """Render SQL for dry-run review without opening a live DB connection."""
    rendered = []
    for statement in statements:
        rendered.append(_render_composable(statement).rstrip(";") + ";")
    return "\n\n".join(rendered)


def _render_composable(value: sql.Composable) -> str:
    """Render psycopg2.sql objects without requiring a PostgreSQL connection."""
    if isinstance(value, sql.SQL):
        return value.string
    if isinstance(value, sql.Identifier):
        return ".".join(_quote_identifier(part) for part in value.strings)
    if isinstance(value, sql.Composed):
        return "".join(_render_composable(part) for part in value.seq)
    raise TypeError(f"Unsupported SQL composable: {type(value)!r}")


def _quote_identifier(value: str) -> str:
    """Return a double-quoted PostgreSQL identifier."""
    return '"' + value.replace('"', '""') + '"'


def _table_ref(table_name: str) -> sql.Identifier:
    """Return a public-schema table identifier."""
    return sql.Identifier(PUBLIC_SCHEMA, table_name)


if __name__ == "__main__":
    raise SystemExit(main())
