"""PostgreSQL sync helpers for finalized retrieval artifacts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from psycopg2 import Binary, sql
from psycopg2.extensions import connection as PsycopgConnection

from connections.postgres_connector import connection_scope
from utils.config_setup import get_input_source_config
from utils.source_context import get_source_context

PUBLIC_SCHEMA = "public"
SOURCE_DOCUMENTS_TABLE = "aegis_source_documents"
MIME_TYPES_BY_EXTENSION = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xml": "application/xml",
}


@dataclass(frozen=True)
class PostgresSyncResult:
    """Summary of one finalized source sync to PostgreSQL."""

    deleted_data_rows: int
    inserted_data_rows: int
    deleted_embedding_rows: int
    inserted_embedding_rows: int
    deleted_document_rows: int
    upserted_document_rows: int
    row_counts: dict[str, int]
    embedding_row_counts: dict[str, int]

    @property
    def master_row_count(self) -> int:
        """Return total chunk rows currently present for the manifest files."""
        return sum(self.row_counts.values())

    @property
    def master_embedding_row_count(self) -> int:
        """Return total embedding rows currently present for the manifest files."""
        return sum(self.embedding_row_counts.values())


def sync_retrieval_source(
    *,
    data_table_name: str,
    embeddings_table_name: str,
    records: Sequence[Any],
    replace_file_ids: set[str],
    processed_rows: Sequence[Mapping[str, Any]],
    processed_embedding_rows: Sequence[Mapping[str, Any]],
    data_fields: Sequence[str],
    data_embedding_fields: frozenset[str],
    embeddings_fields: Sequence[str],
    embeddings_vector_fields: frozenset[str],
) -> PostgresSyncResult:
    """Apply finalized source changes to PostgreSQL retrieval tables."""
    source_types = {str(record.data_source) for record in records}
    if len(source_types) > 1:
        raise ValueError(f"Expected one source_type in manifest, got {sorted(source_types)}")
    source_type = next(iter(source_types), get_source_context().data_source)
    file_ids = [str(record.file_id) for record in records]

    with connection_scope() as conn:
        deleted_data_rows = _delete_file_rows(conn, data_table_name, replace_file_ids)
        deleted_embedding_rows = _delete_file_rows(
            conn,
            embeddings_table_name,
            replace_file_ids,
        )
        inserted_data_rows = _insert_rows(
            conn,
            data_table_name,
            data_fields,
            data_embedding_fields,
            processed_rows,
        )
        inserted_embedding_rows = _insert_rows(
            conn,
            embeddings_table_name,
            embeddings_fields,
            embeddings_vector_fields,
            processed_embedding_rows,
        )
        deleted_document_rows, upserted_document_rows = _sync_source_documents(
            conn,
            source_type,
            records,
        )
        row_counts = _count_rows_by_file_id(conn, data_table_name, file_ids)
        embedding_row_counts = _count_rows_by_file_id(
            conn,
            embeddings_table_name,
            file_ids,
        )
        conn.commit()

    return PostgresSyncResult(
        deleted_data_rows=deleted_data_rows,
        inserted_data_rows=inserted_data_rows,
        deleted_embedding_rows=deleted_embedding_rows,
        inserted_embedding_rows=inserted_embedding_rows,
        deleted_document_rows=deleted_document_rows,
        upserted_document_rows=upserted_document_rows,
        row_counts=row_counts,
        embedding_row_counts=embedding_row_counts,
    )


def _delete_file_rows(
    conn: PsycopgConnection,
    table_name: str,
    file_ids: set[str],
) -> int:
    """Delete all rows for replaced or removed source files."""
    if not file_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE file_id = ANY(%s)").format(
                _table_ref(table_name),
            ),
            (list(file_ids),),
        )
        return int(cur.rowcount)


def _insert_rows(
    conn: PsycopgConnection,
    table_name: str,
    fieldnames: Sequence[str],
    embedding_fields: frozenset[str],
    rows: Sequence[Mapping[str, Any]],
) -> int:
    """Copy normalized rows into a target table through an in-memory CSV."""
    if not rows:
        return 0

    target = _table_ref(table_name)
    staging = sql.Identifier(f"{_identifier_fragment(table_name)}_finalize_stage")
    columns = sql.SQL(", ").join(sql.Identifier(field) for field in fieldnames)
    normalized_csv = _rows_to_csv_text(rows, fieldnames, embedding_fields)

    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP").format(
                staging,
                target,
            ),
        )
        cur.copy_expert(
            sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT CSV, HEADER TRUE, NULL '')")
            .format(staging, columns)
            .as_string(conn),
            io.StringIO(normalized_csv),
        )
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(staging))
        staged_rows = int(cur.fetchone()[0])
        cur.execute(
            sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}").format(
                target,
                columns,
                columns,
                staging,
            ),
        )
    return staged_rows


def _sync_source_documents(
    conn: PsycopgConnection,
    source_type: str,
    records: Sequence[Any],
) -> tuple[int, int]:
    """Delete stale source documents and upsert missing or changed bytes."""
    input_config = get_input_source_config()
    if input_config.source != "local":
        raise NotImplementedError(
            "Source document byte sync currently requires local INPUT_SOURCE."
        )

    input_base_path = Path(input_config.base_path).expanduser().resolve()
    current_file_ids = [str(record.file_id) for record in records]
    existing_hashes = _existing_document_hashes(conn, source_type)
    deleted_rows = _delete_stale_source_documents(conn, source_type, current_file_ids)
    upserted_rows = 0

    for record in records:
        if existing_hashes.get(str(record.file_id)) == str(record.file_hash):
            continue
        absolute_path = _resolve_document_path(input_base_path, str(record.file_path))
        upserted_rows += _upsert_source_document(conn, record, absolute_path)

    return deleted_rows, upserted_rows


def _delete_stale_source_documents(
    conn: PsycopgConnection,
    source_type: str,
    current_file_ids: list[str],
) -> int:
    """Remove document-byte rows for files no longer in the source manifest."""
    target = _table_ref(SOURCE_DOCUMENTS_TABLE)
    with conn.cursor() as cur:
        if current_file_ids:
            cur.execute(
                sql.SQL(
                    "DELETE FROM {} WHERE source_type = %s AND NOT (file_id = ANY(%s))"
                ).format(target),
                (source_type, current_file_ids),
            )
        else:
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE source_type = %s").format(target),
                (source_type,),
            )
        return int(cur.rowcount)


def _existing_document_hashes(
    conn: PsycopgConnection,
    source_type: str,
) -> dict[str, str]:
    """Return stored document hashes for one logical source."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL("SELECT file_id, file_hash FROM {} WHERE source_type = %s").format(
                _table_ref(SOURCE_DOCUMENTS_TABLE),
            ),
            (source_type,),
        )
        return {str(file_id): str(file_hash) for file_id, file_hash in cur.fetchall()}


def _upsert_source_document(
    conn: PsycopgConnection,
    record: Any,
    absolute_path: Path,
) -> int:
    """Insert or update original source bytes for one manifest record."""
    original_bytes = absolute_path.read_bytes()
    actual_size = len(original_bytes)
    expected_size = int(record.file_size)
    if actual_size != expected_size:
        raise ValueError(
            f"Source file size mismatch for {record.file_id}: "
            f"expected {expected_size}, got {actual_size} at {absolute_path}"
        )
    actual_hash = hashlib.sha256(original_bytes).hexdigest()
    if actual_hash != str(record.file_hash):
        raise ValueError(
            f"Source file hash mismatch for {record.file_id}: "
            f"expected {record.file_hash}, got {actual_hash} at {absolute_path}"
        )

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
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                WHERE target.file_hash IS DISTINCT FROM EXCLUDED.file_hash
                   OR target.file_size IS DISTINCT FROM EXCLUDED.file_size
                   OR target.filename IS DISTINCT FROM EXCLUDED.filename
                   OR target.file_path IS DISTINCT FROM EXCLUDED.file_path
                   OR target.mime_type IS DISTINCT FROM EXCLUDED.mime_type
                   OR target.date_last_modified IS DISTINCT FROM EXCLUDED.date_last_modified
                """
            ).format(_table_ref(SOURCE_DOCUMENTS_TABLE)),
            (
                str(record.data_source),
                str(record.file_id),
                str(record.fiscal_year),
                str(record.quarter),
                str(record.bank),
                str(record.file_name),
                str(record.file_type),
                str(record.file_path),
                _mime_type(str(record.file_name), str(record.file_type)),
                str(record.file_hash),
                expected_size,
                str(record.date_last_modified),
                Binary(original_bytes),
            ),
        )
        return int(cur.rowcount)


def _count_rows_by_file_id(
    conn: PsycopgConnection,
    table_name: str,
    file_ids: Sequence[str],
) -> dict[str, int]:
    """Return row counts for each current manifest file."""
    if not file_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT file_id, count(*) FROM {} WHERE file_id = ANY(%s) GROUP BY file_id"
            ).format(_table_ref(table_name)),
            (list(file_ids),),
        )
        return {str(file_id): int(count) for file_id, count in cur.fetchall()}


def _resolve_document_path(input_base_path: Path, file_path: str) -> Path:
    """Resolve one manifest path under the configured local input folder."""
    raw_path = Path(file_path)
    candidate = raw_path if raw_path.is_absolute() else input_base_path / raw_path
    resolved = candidate.resolve()
    try:
        resolved.relative_to(input_base_path)
    except ValueError as exc:
        raise ValueError(
            f"Manifest file_path points outside INPUT_BASE_PATH: {file_path}"
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


def _rows_to_csv_text(
    rows: Sequence[Mapping[str, Any]],
    fieldnames: Sequence[str],
    embedding_fields: frozenset[str],
) -> str:
    """Return normalized CSV text suitable for PostgreSQL COPY."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(fieldnames), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(_csv_row(row, fieldnames, embedding_fields))
    return output.getvalue()


def _csv_row(
    row: Mapping[str, Any],
    fieldnames: Sequence[str],
    embedding_fields: frozenset[str],
) -> dict[str, str]:
    """Convert one finalized artifact row into PostgreSQL COPY text."""
    output = {}
    for field in fieldnames:
        value = row.get(field, "")
        if value is None:
            output[field] = ""
        elif field in embedding_fields and _is_empty_vector(value):
            output[field] = ""
        elif isinstance(value, bool | list | dict):
            output[field] = _remove_nul_bytes(json.dumps(value, separators=(",", ":")))
        else:
            output[field] = _remove_nul_bytes(str(value))
    return output


def _is_empty_vector(value: Any) -> bool:
    """Return whether an embedding value represents an absent vector."""
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped == "[]":
            return True
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            return False
        return isinstance(parsed, list) and len(parsed) == 0
    return False


def _remove_nul_bytes(value: str) -> str:
    """Remove NUL bytes that PostgreSQL text fields reject."""
    return value.replace("\x00", "")


def _identifier_fragment(value: str) -> str:
    """Return a safe temp-table identifier fragment."""
    return "".join(char if char.isalnum() else "_" for char in value).strip("_") or "rows"


def _table_ref(table_name: str) -> sql.Identifier:
    """Return a public-schema table reference."""
    return sql.Identifier(PUBLIC_SCHEMA, table_name)
