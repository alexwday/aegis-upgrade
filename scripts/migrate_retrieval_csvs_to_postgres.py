"""Migrate old finalized retrieval CSVs into the Postgres-native format.

Current pipeline finalization syncs PostgreSQL directly. This script exists
for one-time migration/backfill from older finalized master CSVs. On
``--apply`` it stages both CSVs in temporary tables, truncates the public
targets, inserts staged rows in one transaction, upserts original source
document bytes into ``public.aegis_source_documents``, and rewrites the source
``master-manifest.json`` with the new Postgres storage metadata.

Examples:
    venv/bin/python scripts/migrate_retrieval_csvs_to_postgres.py \
        --source investor_slides
    venv/bin/python scripts/migrate_retrieval_csvs_to_postgres.py \
        --source investor_slides --apply
    venv/bin/python scripts/migrate_retrieval_csvs_to_postgres.py \
        --source rts --env-file /path/.env --apply
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import mimetypes
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import Binary, sql
from psycopg2.extensions import connection as PsycopgConnection

from retrieval_source_config import (  # noqa: E402
    RetrievalSource,
    SourceModules,
    load_source_modules,
    select_source,
    source_keys,
)

PUBLIC_SCHEMA = "public"
DEFAULT_DOCUMENTS_TABLE = "aegis_source_documents"
MASTER_DATA_FILE_NAME = "master-data.csv"
MASTER_EMBEDDINGS_FILE_NAME = "master-embeddings.csv"
MASTER_MANIFEST_FILE_NAME = "master-manifest.json"
MANIFEST_SCHEMA_VERSION = "master_manifest_v2"
MIME_TYPES_BY_EXTENSION = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xml": "application/xml",
}
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
DEFAULT_DATA_SOURCE: str
DEFAULT_LOCAL_OUTPUT_PATH: Path
ENV_PATH: Path
get_database_config: Any
get_embedding_config: Any
get_input_source_config: Any
record_from_mapping: Any
load_config: Any


@dataclass(frozen=True)
class LoadConfig:
    """Resolved inputs for migrating finalized master CSVs."""

    source: RetrievalSource
    env_file: Path
    master_data_csv: Path
    master_embeddings_csv: Path
    master_manifest_json: Path
    data_table: str
    embeddings_table: str
    documents_table: str
    embedding_dimensions: int
    apply: bool
    allow_empty: bool
    skip_document_bytes: bool


@dataclass(frozen=True)
class SourceDocument:
    """One source document whose original bytes should be persisted."""

    source_type: str
    file_id: str
    fiscal_year: str
    quarter: str
    bank: str
    filename: str
    file_type: str
    file_path: str
    file_hash: str
    file_size: int
    date_last_modified: str
    mime_type: str
    absolute_path: Path


def main(argv: list[str] | None = None) -> int:
    """Validate and optionally migrate the finalized master CSVs."""
    argv_list = list(sys.argv[1:] if argv is None else argv)
    source = select_source(argv_list)
    _activate_source_modules(load_source_modules(source))
    args = _parse_args(argv_list, source)
    config = _resolve_config(args, source)
    manifest_records = _load_migration_manifest(config.master_manifest_json)
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
    _validate_manifest_sync(manifest_records, data_rows, embedding_rows)
    documents = (
        _read_source_documents(manifest_records)
        if not config.skip_document_bytes
        else []
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
        if config.skip_document_bytes:
            print("Document byte loading skipped.")
        else:
            print(
                f"Validated {len(documents)} source document record(s) from "
                f"{config.master_manifest_json}."
            )
        print(
            "Re-run with --apply to migrate the CSV snapshot and rewrite the "
            "master manifest.",
        )
        return 0

    db_config = get_database_config()
    with psycopg2.connect(
        **db_config,
        application_name=f"aegis-{config.source.application_slug}-migrate-retrieval-csvs",
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
        loaded_document_rows = 0
        deleted_document_rows = 0
        if not config.skip_document_bytes:
            deleted_document_rows, loaded_document_rows = _refresh_document_table(
                conn,
                table_name=config.documents_table,
                source_type=DEFAULT_DATA_SOURCE,
                documents=documents,
            )

    generated_at = _utc_now()
    _write_master_manifest(
        config=config,
        records=manifest_records,
        data_rows=data_rows,
        embedding_rows=embedding_rows,
        generated_at=generated_at,
    )
    print(
        f'Migrated {loaded_data_rows} row(s) into {PUBLIC_SCHEMA}."{config.data_table}" '
        f"and {loaded_embedding_rows} row(s) into "
        f'{PUBLIC_SCHEMA}."{config.embeddings_table}".',
    )
    if not config.skip_document_bytes:
        print(
            f'Loaded or updated {loaded_document_rows} source document row(s) in '
            f'{PUBLIC_SCHEMA}."{config.documents_table}" '
            f"and removed {deleted_document_rows} stale row(s).",
        )
    print(f"Rewrote Postgres-format manifest: {config.master_manifest_json}")
    return 0


def _activate_source_modules(modules: SourceModules) -> None:
    """Bind selected source module constants used by the shared implementation."""
    globals()["MASTER_DATA_FIELDS"] = modules.finalize.MASTER_DATA_FIELDS
    globals()["MASTER_EMBEDDINGS_FIELDS"] = modules.finalize.MASTER_EMBEDDINGS_FIELDS
    globals()["DEFAULT_DATA_SOURCE"] = modules.config_setup.DEFAULT_DATA_SOURCE
    globals()["DEFAULT_LOCAL_OUTPUT_PATH"] = (
        modules.config_setup.DEFAULT_LOCAL_OUTPUT_PATH
    )
    globals()["ENV_PATH"] = modules.config_setup.ENV_PATH
    globals()["get_database_config"] = modules.config_setup.get_database_config
    globals()["get_embedding_config"] = modules.config_setup.get_embedding_config
    globals()["get_input_source_config"] = modules.config_setup.get_input_source_config
    globals()["record_from_mapping"] = modules.manifest._record_from_mapping
    globals()["load_config"] = modules.config_setup.load_config


def _parse_args(argv: list[str], source: RetrievalSource) -> argparse.Namespace:
    """Return command-line arguments for the CSV loader."""
    parser = argparse.ArgumentParser(
        description=f"Migrate public {source.label} retrieval tables.",
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
        help="Finalized master data CSV to validate and migrate.",
    )
    parser.add_argument(
        "--master-embeddings-csv",
        type=Path,
        default=DEFAULT_LOCAL_OUTPUT_PATH / MASTER_EMBEDDINGS_FILE_NAME,
        help="Finalized master embeddings CSV to validate and migrate.",
    )
    parser.add_argument(
        "--master-manifest-json",
        type=Path,
        default=DEFAULT_LOCAL_OUTPUT_PATH / MASTER_MANIFEST_FILE_NAME,
        help=(
            "Finalized master manifest used to backfill original source document "
            "bytes."
        ),
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
        "--documents-table-name",
        default=DEFAULT_DOCUMENTS_TABLE,
        help=(
            "Shared public table for original source document bytes. Defaults to "
            f"{DEFAULT_DOCUMENTS_TABLE!r}."
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
        "--skip-document-bytes",
        action="store_true",
        help="Do not backfill original document bytes from master-manifest.json.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the migration. Without this flag, validation is dry-run.",
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
    master_manifest_json = args.master_manifest_json.expanduser().resolve()
    if not master_manifest_json.is_file():
        raise FileNotFoundError(
            f"Master manifest JSON not found: {master_manifest_json}"
        )

    dimensions = args.embedding_dimensions or get_embedding_config().dimensions
    if dimensions < 1:
        raise ValueError("--embedding-dimensions must be a positive integer")

    data_table = str(args.data_table_name).strip()
    embeddings_table = str(args.embeddings_table_name).strip()
    documents_table = str(args.documents_table_name).strip()
    if not data_table:
        raise ValueError("--data-table-name must not be blank")
    if not embeddings_table:
        raise ValueError("--embeddings-table-name must not be blank")
    if not documents_table:
        raise ValueError("--documents-table-name must not be blank")

    return LoadConfig(
        source=source,
        env_file=env_file,
        master_data_csv=master_data_csv,
        master_embeddings_csv=master_embeddings_csv,
        master_manifest_json=master_manifest_json,
        data_table=data_table,
        embeddings_table=embeddings_table,
        documents_table=documents_table,
        embedding_dimensions=dimensions,
        apply=args.apply,
        allow_empty=args.allow_empty,
        skip_document_bytes=args.skip_document_bytes,
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


def _validate_manifest_sync(
    records: list[Any] | tuple[Any, ...],
    data_rows: list[dict[str, str]],
    embedding_rows: list[dict[str, str]],
) -> None:
    """Ensure CSV snapshots and manifest describe the same source files."""
    manifest_ids = {record.file_id for record in records}
    data_ids = {row["file_id"] for row in data_rows}
    embedding_ids = {row["file_id"] for row in embedding_rows}
    problems = []
    if manifest_ids != data_ids:
        problems.append(
            "manifest/data mismatch "
            f"(manifest-only={sorted(manifest_ids - data_ids)}, "
            f"data-only={sorted(data_ids - manifest_ids)})"
        )
    if manifest_ids != embedding_ids:
        problems.append(
            "manifest/embedding mismatch "
            f"(manifest-only={sorted(manifest_ids - embedding_ids)}, "
            f"embedding-only={sorted(embedding_ids - manifest_ids)})"
        )
    if problems:
        raise ValueError("Cannot migrate inconsistent CSV snapshot: " + "; ".join(problems))


def _load_migration_manifest(manifest_path: Path) -> tuple[Any, ...]:
    """Load either old raw-list or new object-shaped manifest records."""
    raw_manifest = manifest_path.read_text(encoding="utf-8").strip()
    if not raw_manifest:
        return ()

    try:
        parsed = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in master manifest: {manifest_path}") from exc

    if isinstance(parsed, list):
        rows = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("files"), list):
        rows = parsed["files"]
    else:
        raise ValueError(
            "master-manifest.json must be a JSON list or an object with a files list"
        )

    return tuple(record_from_mapping(row, manifest_path) for row in rows)


def _read_source_documents(records: list[Any] | tuple[Any, ...]) -> list[SourceDocument]:
    """Read source document records from the finalized master manifest."""
    input_config = get_input_source_config()
    if input_config.source != "local":
        raise NotImplementedError(
            "Document byte backfill currently requires local INPUT_SOURCE. "
            "Use --skip-document-bytes for NAS-backed inputs until NAS byte "
            "loading is implemented.",
        )

    input_base_path = Path(input_config.base_path).expanduser().resolve()
    documents = []
    for record in records:
        absolute_path = _resolve_document_path(input_base_path, record.file_path)
        actual_size = absolute_path.stat().st_size
        if actual_size != record.file_size:
            raise ValueError(
                "Source file size does not match master manifest for "
                f"{record.file_id}: expected {record.file_size}, got {actual_size} "
                f"at {absolute_path}",
            )
        documents.append(
            SourceDocument(
                source_type=record.data_source,
                file_id=record.file_id,
                fiscal_year=record.fiscal_year,
                quarter=record.quarter,
                bank=record.bank,
                filename=record.file_name,
                file_type=record.file_type,
                file_path=record.file_path,
                file_hash=record.file_hash,
                file_size=record.file_size,
                date_last_modified=record.date_last_modified,
                mime_type=_mime_type(record.file_name, record.file_type),
                absolute_path=absolute_path,
            ),
        )
    return documents


def _write_master_manifest(
    *,
    config: LoadConfig,
    records: list[Any] | tuple[Any, ...],
    data_rows: list[dict[str, str]],
    embedding_rows: list[dict[str, str]],
    generated_at: str,
) -> None:
    """Rewrite master-manifest.json with Postgres-native storage metadata."""
    row_counts = _row_counts_by_file_id(data_rows)
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": generated_at,
        "table_name": config.data_table,
        "storage_target": "postgres",
        "data_table": config.data_table,
        "embeddings_table": config.embeddings_table,
        "source_documents_table": config.documents_table,
        "output_base_path": _manifest_output_base_path(
            config.master_manifest_json.parent
        ),
        "file_count": len(records),
        "row_count": sum(row_counts.values()),
        "embedding_row_count": len(embedding_rows),
        "files": [
            {
                "source_type": record.data_source,
                "fiscal_year": record.fiscal_year,
                "quarter": record.quarter,
                "bank": record.bank,
                "filename": record.file_name,
                "file_id": record.file_id,
                "file_type": record.file_type,
                "file_path": record.file_path,
                "file_size": record.file_size,
                "file_hash": record.file_hash,
                "date_last_modified": record.date_last_modified,
                "row_count": row_counts.get(record.file_id, 0),
                "created_at": generated_at,
            }
            for record in records
        ],
    }
    config.master_manifest_json.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _row_counts_by_file_id(rows: list[dict[str, str]]) -> dict[str, int]:
    """Return migrated chunk row counts by source file."""
    counts: dict[str, int] = {}
    for row in rows:
        file_id = row["file_id"]
        counts[file_id] = counts.get(file_id, 0) + 1
    return counts


def _manifest_output_base_path(path: Path) -> str:
    """Return a portable manifest output path."""
    resolved = path.expanduser().resolve()
    project_root = Path(__file__).resolve().parents[1]
    try:
        return resolved.relative_to(project_root).as_posix()
    except ValueError:
        return str(resolved)


def _resolve_document_path(input_base_path: Path, file_path: str) -> Path:
    """Resolve one manifest path under the configured local input folder."""
    raw_path = Path(file_path)
    candidate = raw_path if raw_path.is_absolute() else input_base_path / raw_path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(input_base_path)
    except ValueError as exc:
        raise ValueError(
            f"Manifest file_path points outside INPUT_BASE_PATH: {file_path}",
        ) from exc
    if not resolved.is_file():
        raise FileNotFoundError(f"Source document not found: {resolved}")
    return resolved


def _mime_type(filename: str, file_type: str) -> str:
    """Return a stable MIME type for known source document extensions."""
    extension = str(file_type or Path(filename).suffix.lstrip(".")).lower()
    if extension in MIME_TYPES_BY_EXTENSION:
        return MIME_TYPES_BY_EXTENSION[extension]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _refresh_document_table(
    conn: PsycopgConnection,
    table_name: str,
    source_type: str,
    documents: list[SourceDocument],
) -> tuple[int, int]:
    """Delete stale document rows for the source and upsert current bytes."""
    source_types = {source_type} | {document.source_type for document in documents}
    deleted_rows = 0
    upserted_rows = 0
    for current_source_type in sorted(source_types):
        file_ids = [
            document.file_id
            for document in documents
            if document.source_type == current_source_type
        ]
        deleted_rows += _delete_stale_source_documents(
            conn,
            table_name=table_name,
            source_type=current_source_type,
            current_file_ids=file_ids,
        )

    for document in documents:
        upserted_rows += _upsert_source_document(conn, table_name, document)
    return deleted_rows, upserted_rows


def _delete_stale_source_documents(
    conn: PsycopgConnection,
    *,
    table_name: str,
    source_type: str,
    current_file_ids: list[str],
) -> int:
    """Delete document rows for files no longer present in the source manifest."""
    target = _table_ref(table_name)
    with conn.cursor() as cur:
        if current_file_ids:
            cur.execute(
                sql.SQL(
                    "DELETE FROM {} WHERE source_type = %s "
                    "AND NOT (file_id = ANY(%s))",
                ).format(target),
                (source_type, current_file_ids),
            )
        else:
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE source_type = %s").format(target),
                (source_type,),
            )
        return int(cur.rowcount)


def _upsert_source_document(
    conn: PsycopgConnection,
    table_name: str,
    document: SourceDocument,
) -> int:
    """Insert or update one original source document byte record."""
    original_bytes = document.absolute_path.read_bytes()
    actual_hash = hashlib.sha256(original_bytes).hexdigest()
    if actual_hash != document.file_hash:
        raise ValueError(
            "Source file hash does not match master manifest for "
            f"{document.file_id}: expected {document.file_hash}, got {actual_hash} "
            f"at {document.absolute_path}",
        )
    if len(original_bytes) != document.file_size:
        raise ValueError(
            "Source file size changed while loading document bytes for "
            f"{document.file_id}: expected {document.file_size}, got "
            f"{len(original_bytes)} at {document.absolute_path}",
        )

    target = _table_ref(table_name)
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                INSERT INTO {} AS target (
                    source_type,
                    file_id,
                    fiscal_year,
                    quarter,
                    bank,
                    filename,
                    file_type,
                    file_path,
                    mime_type,
                    file_hash,
                    file_size,
                    date_last_modified,
                    original_bytes
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (source_type, file_id) DO UPDATE SET
                    fiscal_year = EXCLUDED.fiscal_year,
                    quarter = EXCLUDED.quarter,
                    bank = EXCLUDED.bank,
                    filename = EXCLUDED.filename,
                    file_type = EXCLUDED.file_type,
                    file_path = EXCLUDED.file_path,
                    mime_type = EXCLUDED.mime_type,
                    file_hash = EXCLUDED.file_hash,
                    file_size = EXCLUDED.file_size,
                    date_last_modified = EXCLUDED.date_last_modified,
                    original_bytes = EXCLUDED.original_bytes,
                    updated_at = now()
                WHERE target.fiscal_year IS DISTINCT FROM EXCLUDED.fiscal_year
                   OR target.quarter IS DISTINCT FROM EXCLUDED.quarter
                   OR target.bank IS DISTINCT FROM EXCLUDED.bank
                   OR target.filename IS DISTINCT FROM EXCLUDED.filename
                   OR target.file_type IS DISTINCT FROM EXCLUDED.file_type
                   OR target.file_path IS DISTINCT FROM EXCLUDED.file_path
                   OR target.mime_type IS DISTINCT FROM EXCLUDED.mime_type
                   OR target.file_hash IS DISTINCT FROM EXCLUDED.file_hash
                   OR target.file_size IS DISTINCT FROM EXCLUDED.file_size
                   OR target.date_last_modified IS DISTINCT FROM EXCLUDED.date_last_modified
                """,
            ).format(target),
            (
                document.source_type,
                document.file_id,
                document.fiscal_year,
                document.quarter,
                document.bank,
                document.filename,
                document.file_type,
                document.file_path,
                document.mime_type,
                document.file_hash,
                document.file_size,
                document.date_last_modified,
                Binary(original_bytes),
            ),
        )
        return int(cur.rowcount)


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


def _utc_now() -> str:
    """Return the current UTC time as an ISO string."""
    return datetime.now(tz=UTC).isoformat()


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
