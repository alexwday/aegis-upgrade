"""Finalize source retrieval outputs into PostgreSQL.

This stage reads manifest progress files plus per-file embedding artifacts,
applies removals and additions directly to the public PostgreSQL retrieval
tables, refreshes source document bytes, writes a compact master manifest
checkpoint, archives the run progress folder, and resets it for the next run.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.config_setup import (
    REPO_ROOT,
    get_output_source_config,
    load_config,
)
from utils.logging_setup import get_stage_logger
from utils.retrieval_postgres import SOURCE_DOCUMENTS_TABLE, sync_retrieval_source
from .embeddings import (
    CONTENT_ROWS_JSONL_FILE_NAME,
    EMBEDDING_INDEX_JSONL_FILE_NAME,
    EMBEDDING_MANIFEST_FILE_NAME,
    EMBEDDINGS_DIR_NAME,
)
from .manifest import (
    ARTIFACTS_DIR_NAME,
    FILES_TO_PROCESS_FILE_NAME,
    FILES_TO_REMOVE_FILE_NAME,
    MANIFEST_FIELDS,
    PROGRESS_DIR,
    ManifestRecord,
    check_master_files,
    load_master_manifest,
)

UPLOAD_FILE_PREFIX = "aegis-earnings-transcripts-data"
UPLOAD_EMBEDDINGS_FILE_PREFIX = "aegis-earnings-transcripts-embeddings"
ARCHIVE_DIR_NAME = "archive"
PROGRESS_ARCHIVE_PREFIX = "progress"
MASTER_DATA_FIELDS = (
    "source_type",
    "fiscal_year",
    "quarter",
    "bank",
    "filename",
    "file_id",
    "file_type",
    "file_path",
    "file_hash",
    "page_number",
    "name",
    "summary",
    "chunk_id",
    "chunk_content",
    "keywords",
    "metrics",
    "keyword_embedding",
    "metric_embedding",
    "summary_embedding",
    "chunk_embedding",
    "created_at",
)
MASTER_EMBEDDING_FIELDS = frozenset(
    {
        "keyword_embedding",
        "metric_embedding",
        "summary_embedding",
        "chunk_embedding",
    }
)
MASTER_EMBEDDINGS_FIELDS = (
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
    "content_unit_id",
    "content_unit_ids",
    "chunk_id",
    "section_id",
    "embedding_text",
    "text_hash",
    "embedding",
    "embedding_model",
    "embedding_dimensions",
    "created_at",
)
MASTER_EMBEDDINGS_VECTOR_FIELDS = frozenset({"embedding"})
MANIFEST_SCHEMA_VERSION = "master_manifest_v2"


class FinalizeStageError(RuntimeError):
    """Raised when master output generation cannot complete safely."""


@dataclass(frozen=True)
class PendingRemovalRecord:
    """One manifest record selected for removal from the master outputs."""

    reason: str
    record: ManifestRecord


@dataclass(frozen=True)
class FinalizeStageResult:
    """Summary returned after writing master outputs and archiving progress."""

    output_base_path: Path
    master_manifest_path: Path
    progress_archive_path: Path
    table_name: str
    processed_file_count: int
    removed_file_count: int
    manifest_file_count: int
    master_row_count: int
    master_embedding_row_count: int


def run_finalize_stage(
    progress_dir: Path = PROGRESS_DIR,
    output_base_path: Path | None = None,
    table_name: str | None = None,
    generated_at: str | None = None,
    progress_archive_dir: Path | None = None,
) -> FinalizeStageResult:
    """Sync finalized retrieval rows to PostgreSQL and archive progress.

    Args:
        progress_dir: Folder containing manifest progress and embedding
            artifacts for files selected in this run.
        output_base_path: Optional local output folder override. When omitted,
            the path comes from source output configuration.
        table_name: Optional PostgreSQL table name stored in the manifest for
            the external upload process. When omitted,
            ``MASTER_DATA_TABLE_NAME`` or the derived DATA_SOURCE default is
            used.
        generated_at: Optional UTC ISO timestamp override for deterministic
            tests. Runtime callers should omit it.
        progress_archive_dir: Optional archive folder override. Runtime callers
            should omit it so progress is archived under ``archive``.

    Returns:
        FinalizeStageResult with output paths, archive path, and row/file counts.

    Raises:
        FinalizeStageError: If progress files, embedding artifacts, PostgreSQL
            sync, or progress cleanup cannot complete.
        NotImplementedError: If output is configured for NAS rather than a local
            path. NAS writes should be added through the connector once the
            side effects are explicitly approved.

    External side effects:
        Applies changed rows directly to PostgreSQL, writes a compact
        ``master-manifest.json`` checkpoint, writes a zip archive under the
        resolved archive folder, and resets ``progress_dir``.
    """
    logger = get_stage_logger(__name__, "FINALIZE")
    load_config()
    resolved_output_path = _resolve_output_base_path(output_base_path)
    resolved_table_name = _resolve_table_name(table_name)
    generated_at = generated_at or _utc_now()

    status = check_master_files(resolved_output_path)
    existing_records = (
        load_master_manifest(status.master_manifest_path)
        if status.master_files_exist
        else ()
    )
    files_to_process = _load_process_records(progress_dir)
    files_to_remove = _load_removal_records(progress_dir)

    logger.info(
        "Finalizing PostgreSQL outputs: existing_files=%d process=%d "
        "remove=%d table=%s",
        len(existing_records),
        len(files_to_process),
        len(files_to_remove),
        resolved_table_name,
    )

    master_manifest_path = status.master_manifest_path

    manifest_records, processed_rows, processed_embedding_rows, replace_file_ids = (
        _build_postgres_sync_state(
            existing_records=existing_records,
            process_records=files_to_process,
            removal_records=files_to_remove,
            progress_dir=progress_dir,
        )
    )
    sync_result = sync_retrieval_source(
        data_table_name=UPLOAD_FILE_PREFIX,
        embeddings_table_name=UPLOAD_EMBEDDINGS_FILE_PREFIX,
        records=manifest_records,
        replace_file_ids=replace_file_ids,
        processed_rows=processed_rows,
        processed_embedding_rows=processed_embedding_rows,
        data_fields=MASTER_DATA_FIELDS,
        data_embedding_fields=MASTER_EMBEDDING_FIELDS,
        embeddings_fields=MASTER_EMBEDDINGS_FIELDS,
        embeddings_vector_fields=MASTER_EMBEDDINGS_VECTOR_FIELDS,
    )
    manifest_payload = _master_manifest_payload(
        generated_at=generated_at,
        table_name=resolved_table_name,
        output_base_path=status.output_base_path,
        manifest_records=manifest_records,
        master_rows=processed_rows,
        master_embedding_rows=processed_embedding_rows,
        row_counts=sync_result.row_counts,
        embedding_row_count=sync_result.master_embedding_row_count,
    )

    status.output_base_path.mkdir(parents=True, exist_ok=True)
    _write_json(master_manifest_path, manifest_payload)
    progress_archive_path = _archive_and_reset_progress(
        progress_dir=progress_dir,
        generated_at=generated_at,
        archive_dir=progress_archive_dir,
    )

    logger.info(
        "Finalize complete: files=%d rows=%d embeddings=%d "
        "inserted_rows=%d inserted_embeddings=%d document_upserts=%d "
        "progress_archive=%s",
        len(manifest_records),
        sync_result.master_row_count,
        sync_result.master_embedding_row_count,
        sync_result.inserted_data_rows,
        sync_result.inserted_embedding_rows,
        sync_result.upserted_document_rows,
        progress_archive_path,
    )
    return FinalizeStageResult(
        output_base_path=status.output_base_path,
        master_manifest_path=master_manifest_path,
        progress_archive_path=progress_archive_path,
        table_name=resolved_table_name,
        processed_file_count=len(files_to_process),
        removed_file_count=len(files_to_remove),
        manifest_file_count=len(manifest_records),
        master_row_count=sync_result.master_row_count,
        master_embedding_row_count=sync_result.master_embedding_row_count,
    )


def _build_postgres_sync_state(
    existing_records: Sequence[ManifestRecord],
    process_records: Sequence[ManifestRecord],
    removal_records: Sequence[PendingRemovalRecord],
    progress_dir: Path,
) -> tuple[tuple[ManifestRecord, ...], list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    """Return current manifest records and changed rows for PostgreSQL sync."""
    _validate_unique_manifest_records(existing_records, "existing master manifest")
    _validate_unique_manifest_records(process_records, "files_to_process")
    _validate_unique_removals(removal_records)

    removal_file_ids = {item.record.file_id for item in removal_records}
    process_file_ids = {record.file_id for record in process_records}
    replace_file_ids = removal_file_ids | process_file_ids
    kept_records = [
        record for record in existing_records if record.file_id not in replace_file_ids
    ]

    processed_rows: list[dict[str, Any]] = []
    processed_embedding_rows: list[dict[str, Any]] = []
    for record in process_records:
        processed_rows.extend(_load_processed_content_rows(progress_dir, record))
        processed_embedding_rows.extend(
            _load_processed_embedding_rows(progress_dir, record)
        )

    _validate_master_sync(processed_rows, process_records)
    _validate_master_embeddings_sync(processed_embedding_rows, process_records)
    manifest_records = tuple(sorted([*kept_records, *process_records], key=_record_key))
    return manifest_records, processed_rows, processed_embedding_rows, replace_file_ids


def _load_processed_content_rows(
    progress_dir: Path,
    record: ManifestRecord,
) -> list[dict[str, Any]]:
    """Load and validate final content rows for one processed workbook."""
    embeddings_root = (
        progress_dir / ARTIFACTS_DIR_NAME / record.file_id / EMBEDDINGS_DIR_NAME
    )
    embedding_manifest_path = embeddings_root / EMBEDDING_MANIFEST_FILE_NAME
    content_rows_path = embeddings_root / CONTENT_ROWS_JSONL_FILE_NAME
    embedding_manifest = _read_json_object(embedding_manifest_path)
    if embedding_manifest.get("status") != "embedded":
        raise FinalizeStageError(
            f"Embedding manifest is not embedded for {record.file_id}: "
            f"{embedding_manifest_path}"
        )

    rows = [
        _normalize_master_row(row, content_rows_path)
        for row in _read_jsonl(content_rows_path)
    ]
    if not rows:
        raise FinalizeStageError(
            f"Processed embedding rows are missing for {record.file_id}: "
            f"{content_rows_path}"
        )

    expected_count = int(embedding_manifest.get("content_unit_count", -1))
    if expected_count != len(rows):
        raise FinalizeStageError(
            f"Embedding row count mismatch for {record.file_id}: manifest says "
            f"{expected_count}, content rows file has {len(rows)}"
        )

    chunk_ids = set()
    for row in rows:
        if row["file_id"] != record.file_id:
            raise FinalizeStageError(
                f"Embedding row file_id mismatch for {record.file_id}: "
                f"got {row['file_id']!r}"
            )
        if not row["chunk_id"]:
            raise FinalizeStageError(
                f"Embedding row is missing chunk identity for {record.file_id}"
            )
        if row["chunk_id"] in chunk_ids:
            raise FinalizeStageError(
                f"Duplicate chunk_id in processed rows for {record.file_id}: "
                f"{row['chunk_id']}"
            )
        chunk_ids.add(row["chunk_id"])
    return rows


def _load_processed_embedding_rows(
    progress_dir: Path,
    record: ManifestRecord,
) -> list[dict[str, Any]]:
    """Load and validate long-form embedding index rows for one workbook."""
    embeddings_root = (
        progress_dir / ARTIFACTS_DIR_NAME / record.file_id / EMBEDDINGS_DIR_NAME
    )
    embedding_manifest_path = embeddings_root / EMBEDDING_MANIFEST_FILE_NAME
    embedding_index_path = embeddings_root / EMBEDDING_INDEX_JSONL_FILE_NAME
    embedding_manifest = _read_json_object(embedding_manifest_path)
    if embedding_manifest.get("status") != "embedded":
        raise FinalizeStageError(
            f"Embedding manifest is not embedded for {record.file_id}: "
            f"{embedding_manifest_path}"
        )

    rows = [
        _normalize_master_embedding_row(row, embedding_index_path)
        for row in _read_jsonl(embedding_index_path)
    ]
    if not rows:
        raise FinalizeStageError(
            f"Processed embedding index rows are missing for {record.file_id}: "
            f"{embedding_index_path}"
        )

    expected_count = embedding_manifest.get("embedding_index_count")
    if expected_count is not None and int(expected_count) != len(rows):
        raise FinalizeStageError(
            f"Embedding index row count mismatch for {record.file_id}: "
            f"manifest says {expected_count}, index file has {len(rows)}"
        )

    embedding_ids = set()
    for row in rows:
        if row["file_id"] != record.file_id:
            raise FinalizeStageError(
                f"Embedding index file_id mismatch for {record.file_id}: "
                f"got {row['file_id']!r}"
            )
        embedding_id = row["embedding_id"]
        if embedding_id in embedding_ids:
            raise FinalizeStageError(
                f"Duplicate embedding_id in processed rows for {record.file_id}: "
                f"{embedding_id}"
            )
        embedding_ids.add(embedding_id)
    return rows


def _validate_master_sync(
    rows: Sequence[dict[str, Any]],
    records: Sequence[ManifestRecord],
) -> None:
    """Validate that master data rows and manifest records match by file_id."""
    manifest_ids = {record.file_id for record in records}
    chunk_keys = set()
    data_file_ids = set()

    for row in rows:
        file_id = str(row.get("file_id", ""))
        chunk_id = str(row.get("chunk_id", ""))
        if not file_id:
            raise FinalizeStageError("Master data row is missing file_id")
        if not chunk_id:
            raise FinalizeStageError(f"Master data row is missing chunk_id: {file_id}")
        chunk_key = (file_id, chunk_id)
        if chunk_key in chunk_keys:
            raise FinalizeStageError(
                f"Duplicate master file_id/chunk_id: {file_id}/{chunk_id}"
            )
        chunk_keys.add(chunk_key)
        data_file_ids.add(file_id)

    missing_in_data = sorted(manifest_ids - data_file_ids)
    extra_in_data = sorted(data_file_ids - manifest_ids)
    if missing_in_data or extra_in_data:
        details = []
        if missing_in_data:
            details.append(f"manifest without data={missing_in_data}")
        if extra_in_data:
            details.append(f"data without manifest={extra_in_data}")
        raise FinalizeStageError(
            "Master data and manifest are out of sync: " + "; ".join(details)
        )


def _validate_master_embeddings_sync(
    rows: Sequence[dict[str, Any]],
    records: Sequence[ManifestRecord],
) -> None:
    """Validate embedding index rows against manifest records."""
    manifest_ids = {record.file_id for record in records}
    embedding_ids = set()
    embedding_file_ids = set()

    for row in rows:
        embedding_id = str(row.get("embedding_id", ""))
        file_id = str(row.get("file_id", ""))
        if not embedding_id:
            raise FinalizeStageError("Master embedding row is missing embedding_id")
        if embedding_id in embedding_ids:
            raise FinalizeStageError(f"Duplicate master embedding_id: {embedding_id}")
        embedding_ids.add(embedding_id)
        if not file_id:
            raise FinalizeStageError(
                f"Master embedding row is missing file_id: {embedding_id}"
            )
        embedding_file_ids.add(file_id)

    missing_in_embeddings = sorted(manifest_ids - embedding_file_ids)
    extra_in_embeddings = sorted(embedding_file_ids - manifest_ids)
    if missing_in_embeddings or extra_in_embeddings:
        details = []
        if missing_in_embeddings:
            details.append(f"manifest without embeddings={missing_in_embeddings}")
        if extra_in_embeddings:
            details.append(f"embeddings without manifest={extra_in_embeddings}")
        raise FinalizeStageError(
            "Master embeddings and manifest are out of sync: " + "; ".join(details)
        )


def _master_manifest_payload(
    generated_at: str,
    table_name: str,
    output_base_path: Path,
    manifest_records: Sequence[ManifestRecord],
    master_rows: Sequence[dict[str, Any]],
    master_embedding_rows: Sequence[dict[str, Any]],
    row_counts: Mapping[str, int] | None = None,
    embedding_row_count: int | None = None,
) -> dict[str, Any]:
    """Build the persisted master manifest JSON object."""
    resolved_row_counts = row_counts or _row_counts_by_file_id(master_rows)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_at": generated_at,
        "table_name": table_name,
        "storage_target": "postgres",
        "data_table": UPLOAD_FILE_PREFIX,
        "embeddings_table": UPLOAD_EMBEDDINGS_FILE_PREFIX,
        "source_documents_table": SOURCE_DOCUMENTS_TABLE,
        "output_base_path": _manifest_output_base_path(output_base_path),
        "file_count": len(manifest_records),
        "row_count": sum(resolved_row_counts.values()),
        "embedding_row_count": (
            embedding_row_count
            if embedding_row_count is not None
            else len(master_embedding_rows)
        ),
        "files": _manifest_file_records(
            manifest_records,
            created_at=generated_at,
            row_counts=resolved_row_counts,
        ),
    }


def _manifest_file_records(
    manifest_records: Sequence[ManifestRecord],
    created_at: str,
    row_counts: Mapping[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Build file-level manifest records for the current master output."""
    resolved_row_counts = row_counts or {}

    return [
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
            "row_count": resolved_row_counts.get(record.file_id, 0),
            "created_at": created_at,
        }
        for record in manifest_records
    ]


def _row_counts_by_file_id(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Return chunk row counts by file_id."""
    row_counts: dict[str, int] = {}
    for row in rows:
        file_id = str(row.get("file_id", ""))
        row_counts[file_id] = row_counts.get(file_id, 0) + 1
    return row_counts


def _load_process_records(progress_dir: Path) -> tuple[ManifestRecord, ...]:
    """Load records selected for processing by the manifest stage."""
    path = progress_dir / FILES_TO_PROCESS_FILE_NAME
    payload = _read_json_object(path)
    rows = payload.get("files_to_process")
    if not isinstance(rows, list):
        raise FinalizeStageError(f"{path} is missing files_to_process list")
    return tuple(_manifest_record_from_mapping(row, path) for row in rows)


def _load_removal_records(progress_dir: Path) -> tuple[PendingRemovalRecord, ...]:
    """Load records selected for removal by the manifest stage."""
    path = progress_dir / FILES_TO_REMOVE_FILE_NAME
    payload = _read_json_object(path)
    rows = payload.get("files_to_remove")
    if not isinstance(rows, list):
        raise FinalizeStageError(f"{path} is missing files_to_remove list")

    removals = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise FinalizeStageError(f"Invalid removal row in {path}: {row!r}")
        reason = str(row.get("reason", "")).strip()
        if not reason:
            raise FinalizeStageError(f"Removal row is missing reason: {path}")
        removals.append(
            PendingRemovalRecord(
                reason=reason,
                record=_manifest_record_from_mapping(row, path),
            )
        )
    return tuple(removals)


def _normalize_master_row(row: Mapping[str, Any], source_path: Path) -> dict[str, Any]:
    """Normalize one row to the stable master CSV schema."""
    normalized = _project_master_row(row)
    missing_fields = [
        field
        for field in (
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
        if not str(normalized.get(field, "")).strip()
    ]
    if missing_fields:
        raise FinalizeStageError(
            f"Master data row is missing required field(s) {missing_fields}: "
            f"{source_path}"
        )
    return normalized


def _project_master_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a wide embedding row or current master row to retrieval schema."""
    return {
        "source_type": _first_value(row, "source_type", "data_source"),
        "fiscal_year": _first_value(row, "fiscal_year"),
        "quarter": _first_value(row, "quarter"),
        "bank": _first_value(row, "bank", "ticker"),
        "filename": _first_value(row, "filename", "file_name"),
        "file_id": _first_value(row, "file_id"),
        "file_type": _first_value(row, "file_type", "filetype"),
        "file_path": _first_value(row, "file_path"),
        "file_hash": _first_value(row, "file_hash"),
        "page_number": _first_value(row, "page_number", "item_number"),
        "name": _first_value(row, "name", "sheet_name", "item_title"),
        "summary": _first_value(row, "summary", "section_summary"),
        "chunk_id": _first_value(row, "chunk_id", "content_unit_id"),
        "chunk_content": _first_value(row, "chunk_content", "raw_content"),
        "keywords": _first_value(row, "keywords", "keywords_json"),
        "metrics": _first_value(row, "metrics", "metrics_json"),
        "keyword_embedding": _first_value(
            row,
            "keyword_embedding",
            "keyword_embedding_json",
        ),
        "metric_embedding": _first_value(
            row,
            "metric_embedding",
            "metric_embedding_json",
        ),
        "summary_embedding": _first_value(
            row,
            "summary_embedding",
            "section_summary_embedding_json",
        ),
        "chunk_embedding": _first_value(
            row,
            "chunk_embedding",
            "content_embedding_json",
        ),
        "created_at": _first_value(row, "created_at"),
    }


def _normalize_master_embedding_row(
    row: Mapping[str, Any],
    source_path: Path,
) -> dict[str, Any]:
    """Normalize one long-form embedding row to the master embedding schema."""
    normalized = _project_master_embedding_row(row)
    missing_fields = [
        field
        for field in (
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
        if not str(normalized.get(field, "")).strip()
    ]
    if missing_fields:
        raise FinalizeStageError(
            f"Master embedding row is missing required field(s) {missing_fields}: "
            f"{source_path}"
        )
    return normalized


def _project_master_embedding_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project an embedding index artifact row to the stable CSV schema."""
    return {
        "embedding_id": _first_value(row, "embedding_id"),
        "embedding_type": _first_value(row, "embedding_type"),
        "embedding_scope": _first_value(row, "embedding_scope"),
        "source_type": _first_value(row, "source_type", "data_source"),
        "fiscal_year": _first_value(row, "fiscal_year"),
        "quarter": _first_value(row, "quarter"),
        "bank": _first_value(row, "bank", "ticker"),
        "filename": _first_value(row, "filename", "file_name"),
        "file_id": _first_value(row, "file_id"),
        "file_type": _first_value(row, "file_type", "filetype"),
        "file_path": _first_value(row, "file_path"),
        "file_hash": _first_value(row, "file_hash"),
        "content_unit_id": _first_value(row, "content_unit_id"),
        "content_unit_ids": _first_value(row, "content_unit_ids"),
        "chunk_id": _first_value(row, "chunk_id"),
        "section_id": _first_value(row, "section_id"),
        "embedding_text": _first_value(row, "embedding_text", "text"),
        "text_hash": _first_value(row, "text_hash"),
        "embedding": _first_value(row, "embedding", "embedding_json"),
        "embedding_model": _first_value(row, "embedding_model"),
        "embedding_dimensions": _first_value(row, "embedding_dimensions"),
        "created_at": _first_value(row, "created_at"),
    }


def _first_value(row: Mapping[str, Any], *keys: str) -> Any:
    """Return the first populated row value from a set of aliases."""
    for index, key in enumerate(keys):
        if key not in row:
            continue
        value = row[key]
        if value is None:
            continue
        if isinstance(value, str) and not value and index < len(keys) - 1:
            continue
        return value
    return ""


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file containing JSON object rows."""
    if not path.is_file():
        raise FinalizeStageError(f"Required JSONL artifact missing: {path}")
    rows = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FinalizeStageError(f"Invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(row, Mapping):
            raise FinalizeStageError(
                f"JSONL row is not an object at {path}:{line_number}"
            )
        rows.append(dict(row))
    return rows


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read a required JSON object file."""
    if not path.is_file():
        raise FinalizeStageError(f"Required JSON artifact missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FinalizeStageError(f"Invalid JSON artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise FinalizeStageError(f"JSON artifact must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write deterministic JSON with an atomic local replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def _archive_and_reset_progress(
    progress_dir: Path,
    generated_at: str,
    archive_dir: Path | None,
) -> Path:
    """Zip all progress artifacts into an archive, then empty progress_dir."""
    resolved_progress_dir = progress_dir.expanduser().resolve()
    if not resolved_progress_dir.is_dir():
        raise FinalizeStageError(
            f"Progress directory is missing: {resolved_progress_dir}"
        )

    resolved_archive_dir = _resolve_progress_archive_dir(
        resolved_progress_dir,
        archive_dir,
    )
    resolved_archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = _unique_progress_archive_path(resolved_archive_dir, generated_at)
    temp_archive_path = archive_path.with_name(f".{archive_path.name}.tmp")

    try:
        _write_progress_archive(resolved_progress_dir, temp_archive_path)
        temp_archive_path.replace(archive_path)
        _reset_progress_dir(resolved_progress_dir)
    except (OSError, RuntimeError) as exc:
        temp_archive_path.unlink(missing_ok=True)
        raise FinalizeStageError(
            f"Failed to archive and reset progress directory: {resolved_progress_dir}"
        ) from exc

    return archive_path


def _resolve_progress_archive_dir(
    progress_dir: Path,
    archive_dir: Path | None,
) -> Path:
    """Resolve the folder where completed progress archives are stored."""
    if archive_dir is not None:
        return archive_dir.expanduser().resolve()
    if progress_dir == PROGRESS_DIR.resolve():
        return Path(__file__).resolve().parent.parent / ARCHIVE_DIR_NAME
    return progress_dir.parent / ARCHIVE_DIR_NAME


def _unique_progress_archive_path(archive_dir: Path, generated_at: str) -> Path:
    """Return a timestamped archive path without overwriting an existing run."""
    archive_stem = f"{PROGRESS_ARCHIVE_PREFIX}_{_timestamp_for_path(generated_at)}"
    archive_path = archive_dir / f"{archive_stem}.zip"
    suffix = 1
    while archive_path.exists():
        archive_path = archive_dir / f"{archive_stem}_{suffix:03d}.zip"
        suffix += 1
    return archive_path


def _write_progress_archive(progress_dir: Path, archive_path: Path) -> None:
    """Write the current progress directory tree into a zip archive."""
    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for path in sorted(progress_dir.rglob("*")):
            relative_path = path.relative_to(progress_dir).as_posix()
            archive_name = f"{progress_dir.name}/{relative_path}"
            if path.is_dir():
                archive.write(path, f"{archive_name}/")
            else:
                archive.write(path, archive_name)


def _reset_progress_dir(progress_dir: Path) -> None:
    """Remove all archived progress artifacts and leave the directory empty."""
    for child_path in progress_dir.iterdir():
        if child_path.is_dir() and not child_path.is_symlink():
            shutil.rmtree(child_path)
        else:
            child_path.unlink()
    progress_dir.mkdir(parents=True, exist_ok=True)


def _manifest_record_from_mapping(row: Any, source_path: Path) -> ManifestRecord:
    """Convert one progress JSON row into a manifest record."""
    if not isinstance(row, Mapping):
        raise FinalizeStageError(f"Invalid manifest row in {source_path}: {row!r}")
    missing = [field for field in MANIFEST_FIELDS if field not in row]
    if missing:
        raise FinalizeStageError(
            f"Manifest row missing fields {missing}: {source_path}"
        )
    return ManifestRecord(
        file_id=str(row["file_id"]),
        data_source=str(row["data_source"]),
        fiscal_year=str(row["fiscal_year"]),
        quarter=str(row["quarter"]),
        bank=str(row["bank"]),
        file_path=str(row["file_path"]),
        file_name=str(row["file_name"]),
        file_type=str(row["file_type"]),
        file_size=int(row["file_size"]),
        file_hash=str(row["file_hash"]),
        date_last_modified=str(row["date_last_modified"]),
    )


def _validate_unique_manifest_records(
    records: Sequence[ManifestRecord],
    label: str,
) -> None:
    """Reject duplicate file IDs in a manifest-record sequence."""
    seen = set()
    for record in records:
        if record.file_id in seen:
            raise FinalizeStageError(f"Duplicate record in {label}: {record.file_id}")
        seen.add(record.file_id)


def _validate_unique_removals(removals: Sequence[PendingRemovalRecord]) -> None:
    """Reject duplicate removal records for the same file ID."""
    seen = set()
    for removal in removals:
        file_id = removal.record.file_id
        if file_id in seen:
            raise FinalizeStageError(f"Duplicate record in files_to_remove: {file_id}")
        seen.add(file_id)


def _resolve_output_base_path(output_base_path: Path | None) -> Path:
    """Resolve the local output folder without NAS side effects."""
    if output_base_path is not None:
        return output_base_path.expanduser().resolve()

    output_config = get_output_source_config()
    if output_config.source != "local":
        raise NotImplementedError(
            "Finalize output currently supports local paths only. NAS output "
            "should be wired through the connector once the exact master "
            "artifact side effects are approved."
        )

    configured_path = output_config.base_path
    if not isinstance(configured_path, Path):
        configured_path = Path(configured_path)
    return configured_path.expanduser().resolve()


def _resolve_table_name(table_name: str | None) -> str:
    """Resolve and validate the retrieval table name stored in the manifest."""
    if table_name is None:
        return UPLOAD_FILE_PREFIX
    value = table_name.strip()
    if not value:
        raise FinalizeStageError("table_name must not be blank")
    return value


def _timestamp_for_path(value: str) -> str:
    """Return a filesystem-safe UTC timestamp label."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y%m%d_%H%M%S")


def _manifest_output_base_path(output_base_path: Path) -> str:
    """Return a portable manifest path when the output is under the repo."""
    resolved_path = output_base_path.expanduser().resolve()
    try:
        return resolved_path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved_path)


def _record_key(record: ManifestRecord) -> tuple[str, str, str, str, str]:
    """Return the stable sort key for master manifest records."""
    return (
        record.data_source,
        record.fiscal_year,
        record.quarter,
        record.bank,
        record.file_id,
    )


def _utc_now() -> str:
    """Return the current UTC time as an ISO string."""
    return datetime.now(tz=UTC).isoformat()
