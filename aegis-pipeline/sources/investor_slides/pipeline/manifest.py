"""Build a manifest diff for persisted source document outputs.

This stage compares the configured source input folder with matching records in
the persisted master manifest. It writes progress JSON files that tell later
stages which source documents need processing and which prior master data
entries need removal.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from utils.config_setup import (
    DEFAULT_DATA_SOURCE,
    get_data_source,
    get_input_source_config,
    get_output_source_config,
    load_config,
)
from utils.logging_setup import get_stage_logger

DATA_SOURCE = DEFAULT_DATA_SOURCE
MASTER_DATA_FILE_NAME = "master-data.csv"
MASTER_MANIFEST_FILE_NAME = "master-manifest.json"
FILES_TO_PROCESS_FILE_NAME = "files_to_process.json"
FILES_TO_REMOVE_FILE_NAME = "files_to_remove.json"
ARTIFACTS_DIR_NAME = "artifacts"
ARTIFACT_MANIFEST_FILE_NAME = "manifest.json"
PIPELINE_LOCK_FILE_NAME = "pipeline.lock"
PIPELINE_LOCK_MAX_AGE = timedelta(hours=24)
PROGRESS_DIR = Path(__file__).resolve().parent / "progress"

FILE_TYPE = "pdf"
FISCAL_PERIOD_PATTERN = re.compile(r"^(?P<fiscal_year>\d{4})_(?P<quarter>Q[1-4])$")
MANIFEST_FIELDS = (
    "file_id",
    "data_source",
    "fiscal_year",
    "quarter",
    "bank",
    "file_path",
    "file_name",
    "file_type",
    "file_size",
    "file_hash",
    "date_last_modified",
)


class ManifestStateError(RuntimeError):
    """Raised when the manifest stage finds incomplete or invalid state."""


@dataclass(frozen=True)
class ManifestRecord:
    """One source document record tracked by the master manifest."""

    file_id: str
    data_source: str
    fiscal_year: str
    quarter: str
    bank: str
    file_path: str
    file_name: str
    file_type: str
    file_size: int
    file_hash: str
    date_last_modified: str


@dataclass(frozen=True)
class MasterFileStatus:
    """Status of the persisted master output files."""

    output_base_path: Path
    output_base_path_exists: bool
    master_data_path: Path
    master_manifest_path: Path
    master_files_exist: bool
    missing_master_files: tuple[str, ...]


@dataclass(frozen=True)
class PendingProcessFile:
    """Current input file that a later stage should process."""

    record: ManifestRecord
    previous_record: ManifestRecord | None


@dataclass(frozen=True)
class PendingRemovalFile:
    """Existing master record that a later stage should remove."""

    reason: str
    record: ManifestRecord
    replacement_record: ManifestRecord | None


@dataclass(frozen=True)
class ManifestDiff:
    """Comparison between current input files and the persisted manifest."""

    new_files: tuple[ManifestRecord, ...]
    modified_files: tuple[PendingProcessFile, ...]
    removed_files: tuple[ManifestRecord, ...]
    unchanged_files: tuple[ManifestRecord, ...]
    files_to_process: tuple[PendingProcessFile, ...]
    files_to_remove: tuple[PendingRemovalFile, ...]


@dataclass(frozen=True)
class ProgressFiles:
    """Paths written by the manifest stage for downstream pipeline stages."""

    files_to_process_path: Path
    files_to_remove_path: Path
    artifacts_dir: Path
    artifact_manifest_paths: tuple[Path, ...]


@dataclass(frozen=True)
class PipelineLock:
    """Filesystem lock held while the manifest stage is running."""

    path: Path
    acquired_at: str


@dataclass(frozen=True)
class ManifestStageResult:
    """Complete result from running the manifest stage."""

    master_status: MasterFileStatus
    input_records: tuple[ManifestRecord, ...]
    master_manifest_records: tuple[ManifestRecord, ...]
    diff: ManifestDiff
    progress_files: ProgressFiles


def run_manifest_stage(
    input_base_path: Path | None = None,
    output_base_path: Path | None = None,
    progress_dir: Path = PROGRESS_DIR,
    data_source: str | None = None,
) -> ManifestStageResult:
    """Run the manifest stage and write progress JSON files.

    Args:
        input_base_path: Optional local source input folder override.
            When omitted, the path comes from source input configuration.
        output_base_path: Optional local output folder override. When omitted,
            the path comes from source output configuration.
        progress_dir: Folder where this stage writes files_to_process.json and
            files_to_remove.json for later pipeline stages. The pipeline lock
            file also lives in this folder.
        data_source: Optional logical source label override. When omitted, the
            value comes from DATA_SOURCE with investor-slides as the default.

    Returns:
        ManifestStageResult with master status, current inputs, manifest records,
        computed diff, and progress file paths.

    Raises:
        ManifestStateError: If the master folder has only one required master
            file, the input folder shape is invalid, or the manifest is invalid.
        NotImplementedError: If local paths are not configured.
    """
    pipeline_lock = acquire_pipeline_lock(progress_dir)
    logger = get_stage_logger(__name__, "MANIFEST")
    logger.info("Running manifest stage")
    resolved_data_source = _resolve_data_source(data_source)
    master_status = check_master_files(output_base_path)
    input_records = build_input_manifest(
        input_base_path,
        data_source=resolved_data_source,
    )
    master_manifest_records = (
        load_master_manifest(master_status.master_manifest_path)
        if master_status.master_files_exist
        else ()
    )
    diff = compare_manifest_records(
        input_records,
        master_manifest_records,
        data_source=resolved_data_source,
    )
    progress_files = write_progress_files(diff, progress_dir)
    logger.info(
        "Manifest complete: inputs=%s, manifest=%s, master_exists=%s, "
        "process=%s, remove=%s",
        len(input_records),
        len(master_manifest_records),
        master_status.master_files_exist,
        len(diff.files_to_process),
        len(diff.files_to_remove),
    )
    release_pipeline_lock(pipeline_lock)

    return ManifestStageResult(
        master_status=master_status,
        input_records=input_records,
        master_manifest_records=master_manifest_records,
        diff=diff,
        progress_files=progress_files,
    )


def acquire_pipeline_lock(
    progress_dir: Path = PROGRESS_DIR,
    *,
    max_age: timedelta = PIPELINE_LOCK_MAX_AGE,
) -> PipelineLock:
    """Create the manifest pipeline lock or raise if an active lock exists.

    The lock is written before progress JSON files are generated. If a run fails,
    the lock remains in place so the next run stops instead of overwriting
    progress artifacts from an unresolved failure. Locks older than max_age are
    treated as stale and removed before a new lock is acquired.
    """
    logger = get_stage_logger(__name__, "MANIFEST")
    progress_dir.mkdir(parents=True, exist_ok=True)
    lock_path = progress_dir / PIPELINE_LOCK_FILE_NAME

    while True:
        now = datetime.now(tz=UTC)
        try:
            with lock_path.open("x", encoding="utf-8") as lock_file:
                lock_file.write(
                    json.dumps(
                        {
                            "created_at": now.isoformat(),
                            "expires_after_hours": max_age.total_seconds() / 3600,
                            "pid": os.getpid(),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n"
                )
            logger.info("Pipeline lock acquired")
            return PipelineLock(path=lock_path, acquired_at=now.isoformat())
        except FileExistsError as exc:
            if _remove_stale_lock(lock_path, now, max_age):
                logger.warning("Stale pipeline lock expired; replacing")
                continue
            raise ManifestStateError(
                "Pipeline lock exists from a prior failed or running manifest "
                f"stage: {lock_path}. Delete this file after reviewing the "
                "failure, or wait for it to expire after 24 hours."
            ) from exc


def release_pipeline_lock(pipeline_lock: PipelineLock) -> None:
    """Remove a held pipeline lock after a successful manifest stage."""
    pipeline_lock.path.unlink(missing_ok=True)
    get_stage_logger(__name__, "MANIFEST").info("Pipeline lock released")


def check_master_files(output_base_path: Path | None = None) -> MasterFileStatus:
    """Check whether the persisted master output files are complete.

    The expected layout is output_base_path/master-data.csv and
    output_base_path/master-manifest.json, where output_base_path already points
    to the configured master folder. Both files must exist together. If exactly
    one exists, this function raises because the persisted state is incomplete
    and should not be loaded by downstream stages.
    """
    base_path = _resolve_output_base_path(output_base_path)
    master_data_path = base_path / MASTER_DATA_FILE_NAME
    master_manifest_path = base_path / MASTER_MANIFEST_FILE_NAME

    if base_path.exists() and not base_path.is_dir():
        raise ManifestStateError(f"Output base path is not a folder: {base_path}")

    for path in (master_data_path, master_manifest_path):
        if path.exists() and not path.is_file():
            raise ManifestStateError(f"Master artifact is not a file: {path}")

    master_data_exists = master_data_path.is_file()
    master_manifest_exists = master_manifest_path.is_file()
    if master_data_exists != master_manifest_exists:
        found = (
            MASTER_DATA_FILE_NAME if master_data_exists else MASTER_MANIFEST_FILE_NAME
        )
        missing = (
            MASTER_MANIFEST_FILE_NAME if master_data_exists else MASTER_DATA_FILE_NAME
        )
        raise ManifestStateError(
            "Incomplete persisted master state: expected both "
            f"{MASTER_DATA_FILE_NAME} and {MASTER_MANIFEST_FILE_NAME}; found only "
            f"{found}. Missing {missing}."
        )

    missing_master_files = tuple(
        name
        for name, exists in (
            (MASTER_DATA_FILE_NAME, master_data_exists),
            (MASTER_MANIFEST_FILE_NAME, master_manifest_exists),
        )
        if not exists
    )

    return MasterFileStatus(
        output_base_path=base_path,
        output_base_path_exists=base_path.is_dir(),
        master_data_path=master_data_path,
        master_manifest_path=master_manifest_path,
        master_files_exist=master_data_exists and master_manifest_exists,
        missing_master_files=missing_master_files,
    )


def build_input_manifest(
    input_base_path: Path | None = None,
    data_source: str | None = None,
) -> tuple[ManifestRecord, ...]:
    """List source document records from the input folder.

    The configured input base path is expected to point directly at the
    source folder. Its children must be fiscal period folders named YYYY_QX,
    with bank ticker subfolders beneath each period. Each populated bank folder
    must contain exactly one visible source file. Empty bank folders are
    treated as absent inputs so the diff can remove prior master records.
    """
    base_path = _resolve_input_base_path(input_base_path)
    resolved_data_source = _resolve_data_source(data_source)
    records: list[ManifestRecord] = []

    for period_dir in _visible_children(base_path):
        if not period_dir.is_dir():
            raise ManifestStateError(
                f"Unexpected file in source input folder: {period_dir}"
            )

        match = FISCAL_PERIOD_PATTERN.fullmatch(period_dir.name)
        if match is None:
            raise ManifestStateError(
                "Source period folders must be named YYYY_QX; "
                f"got {period_dir.name!r}"
            )

        fiscal_year = match.group("fiscal_year")
        quarter = match.group("quarter")
        for bank_dir in _visible_children(period_dir):
            if not bank_dir.is_dir():
                raise ManifestStateError(
                    f"Unexpected file in fiscal period folder: {bank_dir}"
                )
            source_file_path = _find_single_source_file(bank_dir)
            if source_file_path is None:
                continue
            records.append(
                _record_from_source_file(
                    input_base_path=base_path,
                    source_file_path=source_file_path,
                    data_source=resolved_data_source,
                    fiscal_year=fiscal_year,
                    quarter=quarter,
                    bank=bank_dir.name,
                )
            )

    return tuple(sorted(records, key=_manifest_key))


def load_master_manifest(manifest_path: Path) -> tuple[ManifestRecord, ...]:
    """Load persisted manifest records from the master manifest JSON file.

    The current on-disk format is JSON. The file may either contain a list of
    manifest records or an object with a files list. An empty file is treated as
    an empty manifest so initial blank master files can be introduced later.
    """
    if not manifest_path.exists():
        return ()

    raw_manifest = manifest_path.read_text(encoding="utf-8").strip()
    if not raw_manifest:
        return ()

    try:
        parsed = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        raise ManifestStateError(
            f"Invalid JSON in master manifest: {manifest_path}"
        ) from exc

    if isinstance(parsed, list):
        rows = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("files"), list):
        rows = parsed["files"]
    else:
        raise ManifestStateError(
            "master-manifest.json must be a JSON list or an object with a files list"
        )

    return tuple(
        sorted(
            (_record_from_mapping(row, manifest_path) for row in rows),
            key=_manifest_key,
        )
    )


def compare_manifest_records(
    input_records: Sequence[ManifestRecord],
    master_manifest_records: Sequence[ManifestRecord],
    data_source: str | None = None,
) -> ManifestDiff:
    """Compare current input records against matching master manifest records.

    When data_source is supplied, records from other sources are preserved by
    excluding them from removed-file detection.
    """
    scoped_master_records = _scope_master_manifest_records(
        master_manifest_records,
        data_source,
    )
    _validate_input_data_source(input_records, data_source)
    input_index = _index_records(input_records, "current input manifest")
    master_manifest_index = _index_records(
        scoped_master_records,
        "master manifest",
    )

    new_files: list[ManifestRecord] = []
    modified_files: list[PendingProcessFile] = []
    unchanged_files: list[ManifestRecord] = []
    removed_files: list[ManifestRecord] = []
    files_to_process: list[PendingProcessFile] = []
    files_to_remove: list[PendingRemovalFile] = []

    for key in sorted(input_index):
        current_record = input_index[key]
        master_manifest_record = master_manifest_index.get(key)
        if master_manifest_record is None:
            new_files.append(current_record)
            files_to_process.append(
                PendingProcessFile(
                    record=current_record,
                    previous_record=None,
                )
            )
            continue

        if master_manifest_record.file_hash != current_record.file_hash:
            modified = PendingProcessFile(
                record=current_record,
                previous_record=master_manifest_record,
            )
            modified_files.append(modified)
            files_to_process.append(modified)
            files_to_remove.append(
                PendingRemovalFile(
                    reason="modified",
                    record=master_manifest_record,
                    replacement_record=current_record,
                )
            )
            continue

        unchanged_files.append(current_record)

    for key in sorted(master_manifest_index.keys() - input_index.keys()):
        removed_record = master_manifest_index[key]
        removed_files.append(removed_record)
        files_to_remove.append(
            PendingRemovalFile(
                reason="removed",
                record=removed_record,
                replacement_record=None,
            )
        )

    return ManifestDiff(
        new_files=tuple(new_files),
        modified_files=tuple(modified_files),
        removed_files=tuple(removed_files),
        unchanged_files=tuple(unchanged_files),
        files_to_process=tuple(files_to_process),
        files_to_remove=tuple(files_to_remove),
    )


def write_progress_files(
    diff: ManifestDiff,
    progress_dir: Path = PROGRESS_DIR,
) -> ProgressFiles:
    """Write JSON progress files and per-file artifact manifests."""
    progress_dir.mkdir(parents=True, exist_ok=True)
    generated_at = _utc_now()
    files_to_process_path = progress_dir / FILES_TO_PROCESS_FILE_NAME
    files_to_remove_path = progress_dir / FILES_TO_REMOVE_FILE_NAME
    artifacts_dir = progress_dir / ARTIFACTS_DIR_NAME

    _write_json(
        files_to_process_path,
        {
            "generated_at": generated_at,
            "record_count": len(diff.files_to_process),
            "files_to_process": [
                _process_action_to_dict(item) for item in diff.files_to_process
            ],
        },
    )
    _write_json(
        files_to_remove_path,
        {
            "generated_at": generated_at,
            "record_count": len(diff.files_to_remove),
            "files_to_remove": [
                _removal_action_to_dict(item) for item in diff.files_to_remove
            ],
        },
    )
    artifact_manifest_paths = write_artifact_manifests(
        diff.files_to_process,
        artifacts_dir,
    )

    return ProgressFiles(
        files_to_process_path=files_to_process_path,
        files_to_remove_path=files_to_remove_path,
        artifacts_dir=artifacts_dir,
        artifact_manifest_paths=artifact_manifest_paths,
    )


def write_artifact_manifests(
    files_to_process: Sequence[PendingProcessFile],
    artifacts_dir: Path,
) -> tuple[Path, ...]:
    """Create one artifact folder and manifest.json for each file to process."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    manifest_paths: list[Path] = []

    for item in files_to_process:
        file_artifact_dir = artifacts_dir / item.record.file_id
        file_artifact_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = file_artifact_dir / ARTIFACT_MANIFEST_FILE_NAME
        _write_json(manifest_path, _record_to_dict(item.record))
        manifest_paths.append(manifest_path)

    return tuple(manifest_paths)


def main() -> int:
    """Run the manifest stage."""
    run_manifest_stage()
    return 0


def _resolve_input_base_path(input_base_path: Path | None) -> Path:
    """Resolve the local source input folder without NAS side effects."""
    if input_base_path is not None:
        path = input_base_path.expanduser().resolve()
        if not path.is_dir():
            raise ManifestStateError(f"Input base path is not a directory: {path}")
        return path

    load_config()
    input_config = get_input_source_config()
    if input_config.source != "local":
        raise NotImplementedError(
            "Manifest input scanning currently supports local paths only. "
            "NAS scanning should be wired through the connector once the exact "
            "listing and hashing side effects are approved."
        )

    configured_path = input_config.base_path
    if not isinstance(configured_path, Path):
        configured_path = Path(configured_path)
    return configured_path.expanduser().resolve()


def _resolve_output_base_path(output_base_path: Path | None) -> Path:
    """Resolve the local output folder without NAS side effects."""
    if output_base_path is not None:
        return output_base_path.expanduser().resolve()

    load_config()
    output_config = get_output_source_config()
    if output_config.source != "local":
        raise NotImplementedError(
            "Manifest output scanning currently supports local paths only. "
            "NAS output should be wired through the connector once the exact "
            "master artifact side effects are approved."
        )

    configured_path = output_config.base_path
    if not isinstance(configured_path, Path):
        configured_path = Path(configured_path)
    return configured_path.expanduser().resolve()


def _resolve_data_source(data_source: str | None) -> str:
    """Resolve the active source label for manifest identity and scoping."""
    if data_source is not None:
        resolved_data_source = data_source.strip()
        if not resolved_data_source:
            raise ManifestStateError("data_source must not be blank")
        return resolved_data_source

    load_config()
    try:
        return get_data_source()
    except ValueError as exc:
        raise ManifestStateError(str(exc)) from exc


def _scope_master_manifest_records(
    master_manifest_records: Sequence[ManifestRecord],
    data_source: str | None,
) -> tuple[ManifestRecord, ...]:
    """Return records that should participate in this source's diff."""
    if data_source is None:
        return tuple(master_manifest_records)
    return tuple(
        record
        for record in master_manifest_records
        if record.data_source == data_source
    )


def _validate_input_data_source(
    input_records: Sequence[ManifestRecord],
    data_source: str | None,
) -> None:
    """Reject a source-scoped diff containing records from another source."""
    if data_source is None:
        return

    mismatched_records = [
        record.file_id for record in input_records if record.data_source != data_source
    ]
    if mismatched_records:
        raise ManifestStateError(
            "Input manifest contains records outside the active data source "
            f"{data_source!r}: {', '.join(mismatched_records)}"
        )


def _visible_children(path: Path) -> tuple[Path, ...]:
    """Return non-hidden children while ignoring local system artifacts."""
    return tuple(
        sorted(child for child in path.iterdir() if not _is_ignored_name(child.name))
    )


def _find_single_source_file(bank_dir: Path) -> Path | None:
    """Return the source file in a bank folder, or None when the folder is empty."""
    visible_children = _visible_children(bank_dir)
    unexpected_children = [
        child
        for child in visible_children
        if child.is_dir() or child.suffix.lower().lstrip(".") != FILE_TYPE
    ]
    source_files = [
        child
        for child in visible_children
        if child.is_file() and child.suffix.lower().lstrip(".") == FILE_TYPE
    ]

    if unexpected_children or len(source_files) > 1:
        raise ManifestStateError(
            f"Each bank input folder must contain zero or one visible .{FILE_TYPE} "
            "file "
            "and no other visible entries; "
            f"got {bank_dir}"
        )

    return source_files[0] if source_files else None


def _record_from_source_file(
    input_base_path: Path,
    source_file_path: Path,
    data_source: str,
    fiscal_year: str,
    quarter: str,
    bank: str,
) -> ManifestRecord:
    """Build a manifest record for one local source file."""
    stat = source_file_path.stat()
    file_path = source_file_path.relative_to(input_base_path).as_posix()
    return ManifestRecord(
        file_id=_build_file_id(
            data_source=data_source,
            fiscal_year=fiscal_year,
            quarter=quarter,
            bank=bank,
        ),
        data_source=data_source,
        fiscal_year=fiscal_year,
        quarter=quarter,
        bank=bank,
        file_path=file_path,
        file_name=source_file_path.name,
        file_type=source_file_path.suffix.lower().lstrip("."),
        file_size=stat.st_size,
        file_hash=_hash_file(source_file_path),
        date_last_modified=_format_mtime(stat.st_mtime),
    )


def _record_from_mapping(
    row: Any,
    manifest_path: Path,
) -> ManifestRecord:
    """Convert one parsed master manifest row into a manifest record."""
    if not isinstance(row, Mapping):
        raise ManifestStateError(
            f"Master manifest row is not an object in {manifest_path}"
        )

    missing_fields = _missing_manifest_fields(row)
    if missing_fields:
        raise ManifestStateError(
            "Master manifest row is missing required field(s): "
            f"{', '.join(missing_fields)}"
        )

    return ManifestRecord(
        file_id=str(row["file_id"]),
        data_source=str(_manifest_value(row, "data_source", "source_type")),
        fiscal_year=str(row["fiscal_year"]),
        quarter=str(row["quarter"]),
        bank=str(row["bank"]),
        file_path=str(row["file_path"]),
        file_name=str(_manifest_value(row, "file_name", "filename")),
        file_type=str(row["file_type"]),
        file_size=int(row["file_size"]),
        file_hash=str(row["file_hash"]),
        date_last_modified=str(row["date_last_modified"]),
    )


def _missing_manifest_fields(row: Mapping[str, Any]) -> list[str]:
    """Return missing manifest fields while accepting v2 field aliases."""
    missing_fields = [
        field
        for field in MANIFEST_FIELDS
        if field not in row
        and not (
            (field == "data_source" and "source_type" in row)
            or (field == "file_name" and "filename" in row)
        )
    ]
    return missing_fields


def _manifest_value(row: Mapping[str, Any], field: str, alias: str) -> Any:
    """Return a manifest field value, falling back to its v2 alias."""
    if field in row:
        return row[field]
    return row[alias]


def _manifest_key(record: ManifestRecord) -> str:
    """Return the stable identity key for manifest comparison."""
    return record.file_id


def _index_records(
    records: Iterable[ManifestRecord],
    label: str,
) -> dict[str, ManifestRecord]:
    """Index manifest records and reject duplicate identities."""
    index: dict[str, ManifestRecord] = {}
    for record in records:
        key = _manifest_key(record)
        if key in index:
            raise ManifestStateError(f"Duplicate record in {label}: {key}")
        index[key] = record
    return index


def _hash_file(path: Path) -> str:
    """Return a SHA-256 hash for a local file."""
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_file_id(
    data_source: str,
    fiscal_year: str,
    quarter: str,
    bank: str,
) -> str:
    """Return a stable file ID from source identity fields."""
    return "_".join(
        (
            _slug(data_source),
            _slug(fiscal_year),
            _slug(quarter),
            _slug(bank),
        )
    )


def _slug(value: str) -> str:
    """Return a filesystem-friendly label fragment."""
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")


def _format_mtime(timestamp: float) -> str:
    """Format a file modification timestamp as an ISO UTC string."""
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat()


def _utc_now() -> str:
    """Return the current UTC time as an ISO string."""
    return datetime.now(tz=UTC).isoformat()


def _remove_stale_lock(
    lock_path: Path,
    now: datetime,
    max_age: timedelta,
) -> bool:
    """Remove an expired lock file and return whether removal occurred."""
    try:
        modified_at = datetime.fromtimestamp(lock_path.stat().st_mtime, tz=UTC)
    except FileNotFoundError:
        return True

    if now - modified_at < max_age:
        return False

    lock_path.unlink(missing_ok=True)
    return True


def _is_ignored_name(name: str) -> bool:
    """Return whether a filesystem entry should be ignored during scanning."""
    return name.startswith(".") or name.startswith("~$")


def _record_to_dict(record: ManifestRecord) -> dict[str, str | int]:
    """Convert a manifest record into its JSON shape."""
    return {
        "file_id": record.file_id,
        "data_source": record.data_source,
        "fiscal_year": record.fiscal_year,
        "quarter": record.quarter,
        "bank": record.bank,
        "file_path": record.file_path,
        "file_name": record.file_name,
        "file_type": record.file_type,
        "file_size": record.file_size,
        "file_hash": record.file_hash,
        "date_last_modified": record.date_last_modified,
    }


def _process_action_to_dict(item: PendingProcessFile) -> dict[str, str | int]:
    """Convert a pending processing action into its JSON shape."""
    return _record_to_dict(item.record)


def _removal_action_to_dict(item: PendingRemovalFile) -> dict[str, str | int]:
    """Convert a pending removal action into its JSON shape."""
    payload = {"reason": item.reason, **_record_to_dict(item.record)}
    if item.replacement_record is not None:
        payload["replacement_file_hash"] = item.replacement_record.file_hash
        payload["replacement_date_last_modified"] = (
            item.replacement_record.date_last_modified
        )
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Write deterministic UTF-8 JSON for a progress artifact."""
    path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
