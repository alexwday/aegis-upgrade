"""Load finalized master CSVs into public PostgreSQL retrieval tables.

The loader treats the master CSVs as complete current snapshots produced by
the selected source's ``pipeline.finalize`` module. On ``--apply`` it stages
both CSVs in temporary tables, truncates the public targets, and inserts staged
rows in one transaction.

Examples:
    venv/bin/python scripts/load_retrieval_master_csvs.py --source investor_slides
    venv/bin/python scripts/load_retrieval_master_csvs.py --source investor_slides --apply
    venv/bin/python scripts/load_retrieval_master_csvs.py --source rts --env-file /path/.env --apply
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime
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
DATA_JSON_ARRAY_FIELDS = ("keywords", "metrics")
DATA_EMBEDDING_FIELDS = (
    "keyword_embedding",
    "metric_embedding",
    "summary_embedding",
    "chunk_embedding",
)
DATA_REQUIRED_FIELDS = (
    "source_type",
    "fiscal_year",
    "quarter",
    "bank",
    "filename",
    "file_id",
    "file_type",
    "file_path",
    "file_hash",
    "chunk_id",
)
EMBEDDINGS_REQUIRED_FIELDS = (
    "embedding_id",
    "embedding_type",
    "embedding_scope",
    "source_type",
    "fiscal_year",
    "quarter",
    "bank",
    "filename",
    "file_id",
    "file_type",
    "file_path",
    "file_hash",
    "embedding_text",
    "embedding",
    "embedding_model",
    "embedding_dimensions",
    "created_at",
)
EMBEDDING_TYPES = {
    "content",
    "keyword",
    "metric",
    "section_summary",
    "document_summary",
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
class LoadConfig:
    """Resolved inputs for loading finalized master CSVs."""

    source: RetrievalSource
    env_file: Path
    master_data_csv: Path
    master_embeddings_csv: Path
    data_table: str
    embeddings_table: str
    embedding_dimensions: int
    apply: bool
    allow_empty: bool


def main(argv: list[str] | None = None) -> int:
    """Validate and optionally load the finalized master CSVs."""
    argv_list = list(sys.argv[1:] if argv is None else argv)
    source = select_source(argv_list)
    _activate_source_modules(load_source_modules(source))
    args = _parse_args(argv_list, source)
    config = _resolve_config(args, source)
    data_rows = _read_validated_data_rows(
        config.master_data_csv,
        config.embedding_dimensions,
    )
    data_keys = {(row["file_id"], row["chunk_id"]) for row in data_rows}
    embedding_rows = _read_validated_embedding_rows(
        config.master_embeddings_csv,
        config.embedding_dimensions,
        data_keys,
    )
    if not data_rows and not config.allow_empty:
        raise ValueError(
            f"Master data CSV contains no data rows: {config.master_data_csv}. "
            "Use --allow-empty to intentionally refresh the tables to zero rows.",
        )
    if not embedding_rows and not config.allow_empty:
        raise ValueError(
            "Master embeddings CSV contains no data rows: "
            f"{config.master_embeddings_csv}. Use --allow-empty to intentionally "
            "refresh the tables to zero rows.",
        )

    if not config.apply:
        print(
            "Dry run complete: "
            f"validated {len(data_rows)} data row(s) from {config.master_data_csv} "
            f"and {len(embedding_rows)} embedding row(s) from "
            f"{config.master_embeddings_csv}.",
        )
        print("Re-run with --apply to refresh the public tables from these CSVs.")
        return 0

    db_config = get_database_config()
    with psycopg2.connect(
        **db_config,
        application_name=f"aegis-{config.source.application_slug}-load-retrieval-csvs",
    ) as conn:
        loaded_data_rows = _refresh_table(
            conn,
            table_name=config.data_table,
            staging_table_name=config.source.data_staging_table,
            fieldnames=MASTER_DATA_FIELDS,
            rows=data_rows,
        )
        loaded_embedding_rows = _refresh_table(
            conn,
            table_name=config.embeddings_table,
            staging_table_name=config.source.embeddings_staging_table,
            fieldnames=MASTER_EMBEDDINGS_FIELDS,
            rows=embedding_rows,
        )

    print(
        f'Loaded {loaded_data_rows} row(s) into {PUBLIC_SCHEMA}."{config.data_table}" '
        f"and {loaded_embedding_rows} row(s) into "
        f'{PUBLIC_SCHEMA}."{config.embeddings_table}".',
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
    """Return command-line arguments for the CSV loader."""
    parser = argparse.ArgumentParser(
        description=f"Refresh public {source.label} retrieval tables.",
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
        help="Finalized master data CSV to validate and load.",
    )
    parser.add_argument(
        "--master-embeddings-csv",
        type=Path,
        default=DEFAULT_LOCAL_OUTPUT_PATH / MASTER_EMBEDDINGS_FILE_NAME,
        help="Finalized master embeddings CSV to validate and load.",
    )
    parser.add_argument(
        "--data-table-name",
        default=source.data_table,
        help=f"Public chunk table name. Defaults to {source.data_table!r}.",
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
        "--embedding-dimensions",
        type=int,
        help="Expected embedding vector dimensions.",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow empty CSV bodies to refresh both tables to zero rows.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the table refresh. Without this flag, validation is dry-run.",
    )
    return parser.parse_args(argv)


def _resolve_config(args: argparse.Namespace, source: RetrievalSource) -> LoadConfig:
    """Resolve env, file paths, table names, and expected embedding dimensions."""
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

    data_table = str(args.data_table_name).strip()
    embeddings_table = str(args.embeddings_table_name).strip()
    if not data_table:
        raise ValueError("--data-table-name must not be blank")
    if not embeddings_table:
        raise ValueError("--embeddings-table-name must not be blank")

    return LoadConfig(
        source=source,
        env_file=env_file,
        master_data_csv=master_data_csv,
        master_embeddings_csv=master_embeddings_csv,
        data_table=data_table,
        embeddings_table=embeddings_table,
        embedding_dimensions=dimensions,
        apply=args.apply,
        allow_empty=args.allow_empty,
    )


def _read_validated_data_rows(
    master_csv: Path,
    embedding_dimensions: int,
) -> list[dict[str, str]]:
    """Read and normalize chunk-level master rows for PostgreSQL COPY."""
    rows = []
    seen_keys = set()
    with master_csv.open("r", encoding="utf-8", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        _validate_header(reader.fieldnames, MASTER_DATA_FIELDS, master_csv)
        for line_number, row in enumerate(reader, 2):
            if None in row:
                raise ValueError(f"Unexpected extra CSV fields at line {line_number}")
            normalized = _normalize_data_row(row, line_number, embedding_dimensions)
            key = (normalized["file_id"], normalized["chunk_id"])
            if key in seen_keys:
                raise ValueError(
                    f"Duplicate file_id/chunk_id at line {line_number}: {key}",
                )
            seen_keys.add(key)
            rows.append(normalized)
    return rows


def _read_validated_embedding_rows(
    master_csv: Path,
    embedding_dimensions: int,
    data_keys: set[tuple[str, str]],
) -> list[dict[str, str]]:
    """Read and normalize long-form embedding rows for PostgreSQL COPY."""
    rows = []
    seen_ids = set()
    with master_csv.open("r", encoding="utf-8", newline="") as file_obj:
        reader = csv.DictReader(file_obj)
        _validate_header(reader.fieldnames, MASTER_EMBEDDINGS_FIELDS, master_csv)
        for line_number, row in enumerate(reader, 2):
            if None in row:
                raise ValueError(f"Unexpected extra CSV fields at line {line_number}")
            normalized = _normalize_embedding_row(
                row,
                line_number,
                embedding_dimensions,
                data_keys,
            )
            embedding_id = normalized["embedding_id"]
            if embedding_id in seen_ids:
                raise ValueError(
                    f"Duplicate embedding_id at line {line_number}: {embedding_id}",
                )
            seen_ids.add(embedding_id)
            rows.append(normalized)
    return rows


def _validate_header(
    actual_fields: list[str] | None,
    expected_fields: tuple[str, ...],
    path: Path,
) -> None:
    """Validate one CSV header against an expected field tuple."""
    if actual_fields != list(expected_fields):
        raise ValueError(
            f"CSV header does not match expected fields: {path}\n"
            f"Expected: {list(expected_fields)}\n"
            f"Actual:   {actual_fields}",
        )


def _normalize_data_row(
    row: dict[str, str],
    line_number: int,
    embedding_dimensions: int,
) -> dict[str, str]:
    """Normalize one chunk-level CSV record."""
    normalized = {field: row.get(field, "") for field in MASTER_DATA_FIELDS}
    for field in DATA_REQUIRED_FIELDS:
        if not normalized[field].strip():
            raise ValueError(f"Required field {field!r} is blank at line {line_number}")

    if normalized["page_number"].strip():
        normalized["page_number"] = str(
            _parse_int(normalized["page_number"], "page_number", line_number),
        )
    for field in DATA_JSON_ARRAY_FIELDS:
        normalized[field] = _normalize_json_array(normalized[field], field, line_number)
    for field in DATA_EMBEDDING_FIELDS:
        normalized[field] = _normalize_embedding(
            normalized[field],
            field,
            line_number,
            embedding_dimensions,
            allow_blank=True,
        )
    if normalized["created_at"].strip():
        _parse_datetime(normalized["created_at"], "created_at", line_number)
    return normalized


def _normalize_embedding_row(
    row: dict[str, str],
    line_number: int,
    embedding_dimensions: int,
    data_keys: set[tuple[str, str]],
) -> dict[str, str]:
    """Normalize one long-form embedding CSV record."""
    normalized = {field: row.get(field, "") for field in MASTER_EMBEDDINGS_FIELDS}
    for field in EMBEDDINGS_REQUIRED_FIELDS:
        if not normalized[field].strip():
            raise ValueError(f"Required field {field!r} is blank at line {line_number}")

    embedding_type = normalized["embedding_type"]
    if embedding_type not in EMBEDDING_TYPES:
        raise ValueError(
            f"embedding_type {embedding_type!r} is not supported at line {line_number}",
        )
    normalized["content_unit_ids"] = _normalize_json_array(
        normalized["content_unit_ids"],
        "content_unit_ids",
        line_number,
    )
    normalized["embedding_dimensions"] = str(
        _parse_int(
            normalized["embedding_dimensions"],
            "embedding_dimensions",
            line_number,
        ),
    )
    if int(normalized["embedding_dimensions"]) != embedding_dimensions:
        raise ValueError(
            "embedding_dimensions mismatch at line "
            f"{line_number}: expected {embedding_dimensions}",
        )
    normalized["embedding"] = _normalize_embedding(
        normalized["embedding"],
        "embedding",
        line_number,
        embedding_dimensions,
        allow_blank=False,
    )
    _parse_datetime(normalized["created_at"], "created_at", line_number)
    _validate_embedding_references(normalized, line_number, data_keys)
    return normalized


def _validate_embedding_references(
    row: dict[str, str],
    line_number: int,
    data_keys: set[tuple[str, str]],
) -> None:
    """Ensure embedding rows point back to known chunk rows where applicable."""
    file_id = row["file_id"]
    chunk_id = row["chunk_id"].strip()
    if chunk_id and (file_id, chunk_id) not in data_keys:
        raise ValueError(
            f"Embedding row references missing chunk at line {line_number}: "
            f"{file_id}/{chunk_id}",
        )
    content_unit_ids = json.loads(row["content_unit_ids"])
    for content_unit_id in content_unit_ids:
        if not isinstance(content_unit_id, str):
            raise ValueError(f"content_unit_ids must be strings at line {line_number}")
        if (file_id, content_unit_id) not in data_keys:
            raise ValueError(
                f"Embedding row references missing content unit at line {line_number}: "
                f"{file_id}/{content_unit_id}",
            )


def _normalize_json_array(value: str, field: str, line_number: int) -> str:
    """Return compact JSON for a list field, defaulting blanks to ``[]``."""
    stripped = value.strip()
    if not stripped:
        return "[]"
    parsed = _loads_json(stripped, field, line_number)
    if not isinstance(parsed, list):
        raise ValueError(f"{field} must be a JSON array at line {line_number}")
    return json.dumps(parsed, separators=(",", ":"))


def _normalize_embedding(
    value: str,
    field: str,
    line_number: int,
    embedding_dimensions: int,
    *,
    allow_blank: bool,
) -> str:
    """Return compact vector input text or blank for a NULL embedding."""
    stripped = value.strip()
    if not stripped or stripped == "[]":
        if allow_blank:
            return ""
        raise ValueError(f"{field} is blank at line {line_number}")
    parsed = _loads_json(stripped, field, line_number)
    if not isinstance(parsed, list):
        raise ValueError(f"{field} must be a JSON array at line {line_number}")
    if len(parsed) != embedding_dimensions:
        raise ValueError(
            f"{field} has {len(parsed)} dimensions at line {line_number}; "
            f"expected {embedding_dimensions}",
        )

    vector = []
    for index, item in enumerate(parsed):
        if isinstance(item, bool):
            raise ValueError(
                f"{field}[{index}] must be numeric at line {line_number}",
            )
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{field}[{index}] must be numeric at line {line_number}",
            ) from exc
        if not math.isfinite(number):
            raise ValueError(
                f"{field}[{index}] must be finite at line {line_number}",
            )
        vector.append(number)
    return json.dumps(vector, separators=(",", ":"))


def _loads_json(value: str, field: str, line_number: int) -> Any:
    """Parse JSON and attach CSV field context to failures."""
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} is invalid JSON at line {line_number}") from exc


def _parse_int(value: str, field: str, line_number: int) -> int:
    """Parse an integer field with CSV line context."""
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer at line {line_number}") from exc


def _parse_datetime(value: str, field: str, line_number: int) -> datetime:
    """Parse an ISO datetime field with CSV line context."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field} must be an ISO timestamp at line {line_number}"
        ) from exc


def _rows_to_csv_text(
    rows: list[dict[str, str]],
    fieldnames: tuple[str, ...],
) -> str:
    """Return normalized CSV text suitable for PostgreSQL COPY."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def _refresh_table(
    conn: PsycopgConnection,
    table_name: str,
    staging_table_name: str,
    fieldnames: tuple[str, ...],
    rows: list[dict[str, str]],
) -> int:
    """Refresh one target table from normalized rows in the active transaction."""
    target = _table_ref(table_name)
    staging = sql.Identifier(staging_table_name)
    columns = sql.SQL(", ").join(sql.Identifier(field) for field in fieldnames)
    normalized_csv = _rows_to_csv_text(rows, fieldnames)

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP"
            ).format(staging, target),
        )
        copy_sql = sql.SQL(
            "COPY {} ({}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE, NULL '')",
        ).format(staging, columns)
        cur.copy_expert(copy_sql.as_string(conn), io.StringIO(normalized_csv))
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(staging))
        staged_rows = int(cur.fetchone()[0])

        cur.execute(sql.SQL("TRUNCATE TABLE {}").format(target))
        cur.execute(
            sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}").format(
                target,
                columns,
                columns,
                staging,
            ),
        )
    return staged_rows


def _table_ref(table_name: str) -> sql.Identifier:
    """Return a public-schema table identifier."""
    return sql.Identifier(PUBLIC_SCHEMA, table_name)


if __name__ == "__main__":
    raise SystemExit(main())
