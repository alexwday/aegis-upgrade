"""Enrich chunking outputs with metadata, keywords, metrics, and summaries.

This stage mirrors Aegis Retriever Stage 5 for both XLSX sheets and PDF slide
pages. It loads the chunking artifacts, treats every source item as one content
unit and one section, then performs the enrichment call sequence: document
metadata, content keyword/metric extraction, and section summaries.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, Semaphore
from typing import Any

from connections.llm_connector import LLMClient
from utils.config_setup import (
    EnrichmentConfig,
    StageRetryConfig,
    get_enrichment_config,
    get_ssl_verify,
    load_config,
)
from utils.logging_setup import get_stage_logger
from utils.prompt_loader import load_prompt
from .chunking import (
    CHUNKING_ARTIFACT_FILE_NAME,
    CHUNKING_DIR_NAME,
    CHUNKS_JSONL_FILE_NAME,
)
from .manifest import (
    ARTIFACTS_DIR_NAME,
    FILES_TO_PROCESS_FILE_NAME,
    PROGRESS_DIR,
    ManifestRecord,
)

ENRICHMENT_DIR_NAME = "enrichment"
ENRICHMENT_MANIFEST_FILE_NAME = "enrichment_manifest.json"
DOCUMENT_ARTIFACT_FILE_NAME = "document.json"
CONTENT_UNITS_JSONL_FILE_NAME = "content_units.jsonl"
SECTIONS_JSONL_FILE_NAME = "sections.jsonl"
DOCUMENT_METADATA_FILE_NAME = "document_metadata.json"
DEPRECATED_SUMMARY_MARKDOWN_FILE_NAME = "summary.md"
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
VALID_STRUCTURE_TYPES = frozenset(
    {
        "sheet_based",
        "slide_deck",
        "page_based",
        "chapters",
        "sections",
        "topic_based",
        "semantic",
    }
)


class EnrichmentStageError(RuntimeError):
    """Raised when enrichment cannot read or write required artifacts."""


@dataclass
class EnrichedContentUnit:
    """One page or sheet content unit before embeddings."""

    source: dict[str, Any]
    source_run_id: str
    source_artifact_root: Path
    filetype: str
    item_type: str
    item_number: int
    item_title: str
    content_unit_id: str
    chunk_id: str
    chunk_index: int
    raw_content: str
    embedding_content: str
    chunk_context: str = ""
    chunk_header: str = ""
    sheet_passthrough_content: str = ""
    section_passthrough_content: str = ""
    raw_token_count: int = 0
    embedding_token_count: int = 0
    token_tier: str = ""
    section_id: str = ""
    keywords: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-serializable content unit record."""
        return {
            "source": self.source,
            "source_run_id": self.source_run_id,
            "source_artifact_root": str(self.source_artifact_root),
            "filetype": self.filetype,
            "item_type": self.item_type,
            "item_number": self.item_number,
            "item_title": self.item_title,
            "content_unit_id": self.content_unit_id,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "section_id": self.section_id,
            "raw_content": self.raw_content,
            "embedding_content": self.embedding_content,
            "chunk_context": self.chunk_context,
            "chunk_header": self.chunk_header,
            "sheet_passthrough_content": self.sheet_passthrough_content,
            "section_passthrough_content": self.section_passthrough_content,
            "keywords": list(self.keywords),
            "metrics": list(self.metrics),
            "raw_token_count": self.raw_token_count,
            "embedding_token_count": self.embedding_token_count,
            "token_tier": self.token_tier,
        }


@dataclass
class EnrichmentSection:
    """One source-item section."""

    section_id: str
    parent_section_id: str
    level: str
    title: str
    sequence: int
    page_start: int
    page_end: int
    chunk_ids: list[str]
    token_count: int = 0
    summary: str = ""
    keywords: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-serializable section record."""
        return {
            "section_id": self.section_id,
            "parent_section_id": self.parent_section_id,
            "level": self.level,
            "title": self.title,
            "sequence": self.sequence,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "chunk_ids": list(self.chunk_ids),
            "token_count": self.token_count,
            "summary": self.summary,
            "keywords": list(self.keywords),
            "metrics": list(self.metrics),
        }


@dataclass
class EnrichmentDocument:
    """One source document flowing through enrichment."""

    source: dict[str, Any]
    source_stage_run_id: str
    source_artifact_root: Path
    chunking_artifact_root: Path
    chunks_jsonl_path: Path
    content_units: list[EnrichedContentUnit]
    document_metadata: dict[str, Any] = field(default_factory=dict)
    sections: list[EnrichmentSection] = field(default_factory=list)

    @property
    def filetype(self) -> str:
        """Return source file type."""
        return str(self.source.get("filetype", ""))

    @property
    def filename(self) -> str:
        """Return source filename."""
        return str(self.source.get("filename", ""))

    def to_record(self) -> dict[str, Any]:
        """Return a complete JSON-serializable document record."""
        return {
            "source": self.source,
            "source_stage_run_id": self.source_stage_run_id,
            "source_artifact_root": str(self.source_artifact_root),
            "chunking_artifact_root": str(self.chunking_artifact_root),
            "chunks_jsonl_path": str(self.chunks_jsonl_path),
            "document_metadata": self.document_metadata,
            "sections": [section.to_record() for section in self.sections],
            "content_units": [unit.to_record() for unit in self.content_units],
        }


@dataclass(frozen=True)
class EnrichmentStageResult:
    """Summary returned after enrichment artifacts are written."""

    processed_file_count: int
    content_unit_count: int
    enrichment_artifact_paths: tuple[Path, ...]


@dataclass(frozen=True)
class _ProcessedWorkbook:
    """Result returned after one source document has completed enrichment."""

    index: int
    artifact_path: Path
    content_unit_count: int


@dataclass
class _WorkbookProgress:
    """Thread-safe tracker state for one source document's enrichment progress."""

    index: int
    total_files: int
    file_id: str
    file_name: str
    phase: str = "queued"
    sheet_total: int = 0
    metadata_done: int = 0
    content_done: int = 0
    sections_done: int = 0
    completed: bool = False
    failed: bool = False
    started_monotonic: float = field(default_factory=time.perf_counter)


class _EnrichmentProgressTracker:
    """Emit compact aggregate enrichment progress for concurrent documents."""

    def __init__(
        self,
        logger: Any,
        total_files: int,
        interval_seconds: float,
    ) -> None:
        """Create a progress tracker with timer-based aggregate logging."""
        self._logger = logger
        self._total_files = total_files
        self._interval_seconds = interval_seconds
        self._states: dict[str, _WorkbookProgress] = {}
        self._lock = Lock()
        self._last_logged = 0.0

    def start(self, index: int, record: ManifestRecord) -> None:
        """Track a source document as active and log the start once."""
        state = _WorkbookProgress(
            index=index,
            total_files=self._total_files,
            file_id=record.file_id,
            file_name=record.file_name,
            phase="loading",
        )
        with self._lock:
            self._states[record.file_id] = state
        self._logger.info(
            "Started enrichment document %d/%d: %s",
            index,
            self._total_files,
            record.file_name,
        )

    def update(
        self,
        file_id: str,
        *,
        phase: str | None = None,
        sheet_total: int | None = None,
        metadata_done: int | None = None,
        content_done: int | None = None,
        sections_done: int | None = None,
        force: bool = False,
    ) -> None:
        """Update one document and maybe log an aggregate progress snapshot."""
        with self._lock:
            state = self._states.get(file_id)
            if state is None:
                return
            if phase is not None:
                state.phase = phase
            if sheet_total is not None:
                state.sheet_total = sheet_total
            if metadata_done is not None:
                state.metadata_done = metadata_done
            if content_done is not None:
                state.content_done = content_done
            if sections_done is not None:
                state.sections_done = sections_done
            self._log_snapshot_locked(force=force)

    def complete(self, file_id: str, duration_seconds: float) -> None:
        """Mark a source document completed and log one final per-file line."""
        with self._lock:
            state = self._states.get(file_id)
            if state is None:
                return
            state.phase = "complete"
            state.completed = True
            state.metadata_done = 1
            state.content_done = state.sheet_total
            state.sections_done = state.sheet_total
            completed = self._completed_count_locked()
        self._logger.info(
            "Finished enrichment document %d/%d: %s items=%d duration=%.1fs",
            completed,
            self._total_files,
            state.file_name,
            state.sheet_total,
            duration_seconds,
        )
        self.update(file_id, force=True)

    def fail(self, file_id: str) -> None:
        """Mark a document failed so snapshots do not show it as active."""
        with self._lock:
            state = self._states.get(file_id)
            if state is None:
                return
            state.phase = "failed"
            state.failed = True
            self._log_snapshot_locked(force=True)

    def _log_snapshot_locked(self, force: bool = False) -> None:
        """Log one aggregate snapshot when forced or the interval has elapsed."""
        if self._total_files == 0:
            return
        now = time.perf_counter()
        if not force:
            if self._interval_seconds <= 0:
                return
            if now - self._last_logged < self._interval_seconds:
                return
        self._last_logged = now
        self._logger.info("Enrichment progress: %s", self._render_locked())

    def _render_locked(self) -> str:
        """Render compact active-document progress for one log line."""
        completed = self._completed_count_locked()
        active_states = [
            state
            for state in self._states.values()
            if not state.completed and not state.failed
        ]
        active_states.sort(key=lambda state: state.index)
        if not active_states:
            return f"files {completed}/{self._total_files} complete"
        active = " | ".join(_render_progress_state(state) for state in active_states)
        return f"files {completed}/{self._total_files} complete | {active}"

    def _completed_count_locked(self) -> int:
        """Return completed document count while the caller holds the lock."""
        return sum(1 for state in self._states.values() if state.completed)


def _render_progress_state(state: _WorkbookProgress) -> str:
    """Render per-document item counts in one compact progress segment."""
    total_steps = 1 + (state.sheet_total * 2)
    completed_steps = (
        state.metadata_done
        + state.content_done
        + state.sections_done
    )
    return f"{state.file_id}: {state.phase} steps {completed_steps}/{total_steps}"


class _BoundedLLMClient:
    """Bound live LLM calls through a shared stage-wide semaphore."""

    def __init__(self, client: Any, semaphore: Semaphore) -> None:
        """Wrap a client while preserving its OpenAI-compatible call contract."""
        self._client = client
        self._semaphore = semaphore

    def call(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Call the wrapped client after acquiring one enrichment worker slot."""
        with self._semaphore:
            return self._client.call(*args, **kwargs)


def run_enrichment_stage(
    progress_dir: Path = PROGRESS_DIR,
    config: EnrichmentConfig | None = None,
    client_factory: Callable[[], Any] | None = None,
    max_files: int | None = None,
    max_content_units_per_file: int | None = None,
    max_workbook_workers: int | None = None,
) -> EnrichmentStageResult:
    """Run enrichment over existing chunking artifacts.

    Args:
        progress_dir: Folder containing progress manifests and artifacts.
        config: Optional enrichment runtime config for tests or overrides,
            including document concurrency, LLM call concurrency, retry policy,
            token budgets, and progress-log cadence.
        client_factory: Optional factory returning an LLM client with ``call``.
            When omitted, a configured ``LLMClient`` is created and live LLM
            calls are made.
        max_files: Optional file limit for smoke checks.
        max_content_units_per_file: Optional item/content-unit limit per file.
        max_workbook_workers: Optional document concurrency override. When not
            provided, ``config.max_workbook_workers`` is used.

    Returns:
        EnrichmentStageResult with artifact paths and content-unit count.

    External side effects:
        Writes artifacts under ``progress_dir/artifacts/<file_id>/enrichment``.
        Performs live LLM calls when ``client_factory`` is omitted.
    """
    logger = get_stage_logger(__name__, "ENRICHMENT")
    load_config()
    enrichment_config = config or get_enrichment_config()
    records = list(_load_process_records(progress_dir))
    if max_files is not None:
        records = records[:max_files]
    workbook_workers = _resolve_workbook_workers(
        max_workbook_workers
        if max_workbook_workers is not None
        else enrichment_config.max_workbook_workers,
        len(records),
    )
    client_factory = client_factory or _default_client_factory
    processed_at = _utc_now()
    progress = _EnrichmentProgressTracker(
        logger,
        total_files=len(records),
        interval_seconds=enrichment_config.progress_log_interval_seconds,
    )
    llm_semaphore = Semaphore(enrichment_config.max_parallel_enrichment_calls)

    logger.info(
        "Enrichment queued: files=%d document_workers=%d "
        "item_workers=%d llm_workers=%d progress_interval=%.1fs",
        len(records),
        workbook_workers,
        enrichment_config.max_sheet_workers,
        enrichment_config.max_parallel_enrichment_calls,
        enrichment_config.progress_log_interval_seconds,
    )

    if not records:
        logger.info("Enrichment complete: files=0 content_units=0")
        return EnrichmentStageResult(
            processed_file_count=0,
            content_unit_count=0,
            enrichment_artifact_paths=(),
        )

    if workbook_workers == 1:
        processed = [
            _process_enrichment_workbook(
                index=index,
                record=record,
                progress_dir=progress_dir,
                config=enrichment_config,
                client_factory=client_factory,
                llm_semaphore=llm_semaphore,
                processed_at=processed_at,
                max_content_units_per_file=max_content_units_per_file,
                progress=progress,
            )
            for index, record in enumerate(records, start=1)
        ]
    else:
        processed_results: list[_ProcessedWorkbook | None] = [None] * len(records)
        with ThreadPoolExecutor(max_workers=workbook_workers) as pool:
            futures = {
                pool.submit(
                    _process_enrichment_workbook,
                    index=index,
                    record=record,
                    progress_dir=progress_dir,
                    config=enrichment_config,
                    client_factory=client_factory,
                    llm_semaphore=llm_semaphore,
                    processed_at=processed_at,
                    max_content_units_per_file=max_content_units_per_file,
                    progress=progress,
                ): index
                for index, record in enumerate(records, start=1)
            }
            for future in as_completed(futures):
                result = future.result()
                processed_results[result.index - 1] = result
        processed = [result for result in processed_results if result is not None]

    artifact_paths = [result.artifact_path for result in processed]
    content_unit_count = sum(result.content_unit_count for result in processed)

    logger.info(
        "Enrichment complete: files=%d content_units=%d",
        len(records),
        content_unit_count,
    )
    return EnrichmentStageResult(
        processed_file_count=len(records),
        content_unit_count=content_unit_count,
        enrichment_artifact_paths=tuple(artifact_paths),
    )


def _process_enrichment_workbook(
    index: int,
    record: ManifestRecord,
    progress_dir: Path,
    config: EnrichmentConfig,
    client_factory: Callable[[], Any],
    llm_semaphore: Semaphore,
    processed_at: str,
    max_content_units_per_file: int | None,
    progress: _EnrichmentProgressTracker,
) -> _ProcessedWorkbook:
    """Run all enrichment phases for one source document and write artifacts."""
    progress.start(index, record)
    start = time.perf_counter()
    started_at = _utc_now()
    try:
        client = _BoundedLLMClient(client_factory(), llm_semaphore)
        document = _load_enrichment_document(
            progress_dir,
            record=record,
            max_content_units=max_content_units_per_file,
        )
        progress.update(
            record.file_id,
            phase="document_metadata",
            sheet_total=_sheet_count(document),
            force=True,
        )
        enrich_document_metadata(document, client, config)
        progress.update(record.file_id, metadata_done=1)
        detect_sheet_sections(document)
        progress.update(
            record.file_id,
            phase="content_extraction",
            sheet_total=_section_count(document),
        )
        extract_content_metadata(
            document,
            client,
            config,
            progress_callback=lambda done, total: progress.update(
                record.file_id,
                phase="content_extraction",
                sheet_total=total,
                content_done=done,
            ),
        )
        progress.update(
            record.file_id,
            phase="section_summary",
            content_done=_section_count(document),
        )
        summarize_sections(
            document,
            client,
            config,
            progress_callback=lambda done, total: progress.update(
                record.file_id,
                phase="section_summary",
                sheet_total=total,
                sections_done=done,
            ),
        )
        completed_at = _utc_now()
        duration_seconds = time.perf_counter() - start
        artifact_path = write_enrichment_artifacts(
            record=record,
            document=document,
            enrichment_root=_artifact_root(progress_dir, record.file_id)
            / ENRICHMENT_DIR_NAME,
            processed_at=processed_at,
            file_started_at=started_at,
            file_completed_at=completed_at,
            duration_seconds=duration_seconds,
        )
        progress.complete(record.file_id, duration_seconds)
        return _ProcessedWorkbook(
            index=index,
            artifact_path=artifact_path,
            content_unit_count=len(document.content_units),
        )
    except Exception:
        progress.fail(record.file_id)
        raise


def enrich_document_metadata(
    document: EnrichmentDocument,
    client: Any,
    config: EnrichmentConfig,
) -> None:
    """Populate document metadata with the Aegis metadata prompt."""
    prompt = load_prompt("doc_metadata")
    user_message = _format_doc_metadata_input(document, prompt, config)
    metadata = _call_with_retry(
        client=client,
        messages=[
            {"role": "system", "content": prompt["system_prompt"]},
            {"role": "user", "content": user_message},
        ],
        prompt=prompt,
        parser=_parse_metadata_response,
        retry_config=config.retries.doc_metadata,
        context=f"doc_metadata:{document.filename}",
    )
    metadata = _normalize_source_metadata(document, metadata)
    source_context = _source_context(document)
    document.document_metadata = {
        "title": metadata["title"],
        "authors": metadata["authors"],
        "publication_date": metadata["publication_date"],
        "language": metadata["language"],
        "structure_type": metadata["structure_type"],
        "executive_summary": metadata["executive_summary"],
        "data_source": source_context["data_source"],
        "filter_1": source_context["filter_1"],
        "filter_2": source_context["filter_2"],
        "filter_3": source_context["filter_3"],
        "has_toc": metadata["has_toc"],
        "toc_entries": metadata["toc_entries"],
        "source_toc_entries": metadata["toc_entries"],
        "generated_toc_entries": [],
        "rationale": metadata["rationale"],
    }


def detect_sheet_sections(document: EnrichmentDocument) -> None:
    """Create one section per source page or sheet and assign units."""
    sections = []
    grouped = _group_units_by_item(document.content_units)
    for sequence, item_number in enumerate(sorted(grouped), start=1):
        units = grouped[item_number]
        title = units[0].item_title or f"{_unit_item_label(units[0])} {item_number}"
        section = EnrichmentSection(
            section_id=str(sequence),
            parent_section_id="",
            level="section",
            title=title,
            sequence=sequence,
            page_start=item_number,
            page_end=item_number,
            chunk_ids=[unit.content_unit_id for unit in units],
            token_count=sum(unit.raw_token_count for unit in units),
        )
        sections.append(section)
        for unit in units:
            unit.section_id = section.section_id
    document.sections = sections


def extract_content_metadata(
    document: EnrichmentDocument,
    client: Any,
    config: EnrichmentConfig,
    progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    """Populate keyword and metric extraction for every content unit.

    Independent content batches fan out up to ``config.max_sheet_workers``.
    The optional callback receives completed item count and total item count
    after each batch finishes.
    """
    prompt = load_prompt("content_extraction")
    batches = _batch_units(
        document.content_units,
        config.budgets.content_extraction_batch,
    )
    all_results: dict[str, dict[str, list[str]]] = {}
    completed_unit_ids: set[str] = set()
    unit_ids_by_sheet = _unit_ids_by_sheet(document.content_units)
    total_sheets = len(unit_ids_by_sheet)
    if not batches:
        if progress_callback is not None:
            progress_callback(0, total_sheets)
    elif _parallel_worker_count(config, len(batches)) == 1:
        for batch_index, batch in enumerate(batches, start=1):
            batch_results = _extract_content_batch(
                batch=batch,
                document=document,
                client=client,
                config=config,
                prompt=prompt,
                batch_label=f"batch_{batch_index}",
            )
            all_results.update(batch_results)
            completed_unit_ids.update(unit.content_unit_id for unit in batch)
            if progress_callback is not None:
                progress_callback(
                    _completed_sheet_count(unit_ids_by_sheet, completed_unit_ids),
                    total_sheets,
                )
    else:
        worker_count = _parallel_worker_count(config, len(batches))
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(
                    _extract_content_batch,
                    batch=batch,
                    document=document,
                    client=client,
                    config=config,
                    prompt=prompt,
                    batch_label=f"batch_{batch_index}",
                ): batch
                for batch_index, batch in enumerate(batches, start=1)
            }
            for future in as_completed(futures):
                batch = futures[future]
                batch_results = future.result()
                all_results.update(batch_results)
                completed_unit_ids.update(unit.content_unit_id for unit in batch)
                if progress_callback is not None:
                    progress_callback(
                        _completed_sheet_count(
                            unit_ids_by_sheet,
                            completed_unit_ids,
                        ),
                        total_sheets,
                    )
    for unit in document.content_units:
        extracted = all_results.get(unit.content_unit_id, {})
        unit.keywords = list(extracted.get("keywords", []))
        unit.metrics = list(extracted.get("metrics", []))


def summarize_sections(
    document: EnrichmentDocument,
    client: Any,
    config: EnrichmentConfig,
    progress_callback: Callable[[int, int], None] | None = None,
) -> None:
    """Populate section summaries, keywords, and metrics.

    Section batches fan out up to ``config.max_sheet_workers``. The optional
    callback receives completed section count and total section
    count after each batch finishes.
    """
    prompt = load_prompt("section_summary")
    sections = [section for section in document.sections if section.chunk_ids]
    batches = _batch_sections(sections, config.budgets.section_summary_batch)
    all_results: dict[str, dict[str, Any]] = {}
    completed: list[EnrichmentSection] = []
    if not batches:
        if progress_callback is not None:
            progress_callback(0, len(sections))
    elif _parallel_worker_count(config, len(batches)) == 1:
        for batch_index, batch in enumerate(batches, start=1):
            batch_results = _summarize_section_batch(
                batch=batch,
                document=document,
                client=client,
                prompt=prompt,
                retry_config=config.retries.section_summary,
                context=f"section_summary:{document.filename}:batch_{batch_index}",
                toc_context=_build_progressive_toc(completed),
            )
            all_results.update(batch_results)
            completed.extend(batch)
            if progress_callback is not None:
                progress_callback(len(completed), len(sections))
    else:
        worker_count = _parallel_worker_count(config, len(batches))
        # Parallel summaries cannot depend on earlier batch summaries, so each
        # batch gets the same stable section outline instead of a progressive TOC.
        toc_context = _build_section_outline(sections)
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(
                    _summarize_section_batch,
                    batch=batch,
                    document=document,
                    client=client,
                    prompt=prompt,
                    retry_config=config.retries.section_summary,
                    context=f"section_summary:{document.filename}:batch_{batch_index}",
                    toc_context=toc_context,
                ): batch
                for batch_index, batch in enumerate(batches, start=1)
            }
            for future in as_completed(futures):
                batch = futures[future]
                batch_results = future.result()
                all_results.update(batch_results)
                completed.extend(batch)
                if progress_callback is not None:
                    progress_callback(len(completed), len(sections))
    for section in document.sections:
        data = all_results.get(section.section_id)
        if data:
            section.summary = data["summary"]
            section.keywords = list(data["keywords"])
            section.metrics = list(data["metrics"])
    document.document_metadata["generated_toc_entries"] = [
        {
            "section_id": section.section_id,
            "title": section.title,
            "page_start": section.page_start,
            "summary": section.summary,
        }
        for section in document.sections
        if section.level == "section"
    ]


def write_enrichment_artifacts(
    record: ManifestRecord,
    document: EnrichmentDocument,
    enrichment_root: Path,
    processed_at: str,
    file_started_at: str,
    file_completed_at: str,
    duration_seconds: float,
) -> Path:
    """Write inspectable enrichment artifacts for one source document."""
    enrichment_root.mkdir(parents=True, exist_ok=True)
    content_units_path = enrichment_root / CONTENT_UNITS_JSONL_FILE_NAME
    sections_path = enrichment_root / SECTIONS_JSONL_FILE_NAME
    metadata_path = enrichment_root / DOCUMENT_METADATA_FILE_NAME
    document_path = enrichment_root / DOCUMENT_ARTIFACT_FILE_NAME
    deprecated_summary_path = enrichment_root / DEPRECATED_SUMMARY_MARKDOWN_FILE_NAME
    if deprecated_summary_path.exists():
        deprecated_summary_path.unlink()
    _write_jsonl(
        content_units_path,
        [unit.to_record() for unit in document.content_units],
    )
    _write_jsonl(sections_path, [section.to_record() for section in document.sections])
    _write_json(metadata_path, document.document_metadata)
    document_payload = {
        **document.to_record(),
        "stage": "enrichment",
        "processed_at": processed_at,
        "file_started_at": file_started_at,
        "file_completed_at": file_completed_at,
        "duration_seconds": round(duration_seconds, 6),
        "artifact_root": str(enrichment_root),
        "content_units_jsonl_path": str(content_units_path),
        "sections_jsonl_path": str(sections_path),
        "document_metadata_json_path": str(metadata_path),
    }
    _write_json(document_path, document_payload)
    _write_json(
        enrichment_root / ENRICHMENT_MANIFEST_FILE_NAME,
        _enrichment_manifest_record(
            record=record,
            document=document,
            document_path=document_path,
            content_units_path=content_units_path,
            sections_path=sections_path,
            metadata_path=metadata_path,
            processed_at=processed_at,
            duration_seconds=duration_seconds,
        ),
    )
    return document_path


def _extract_content_batch(
    batch: list[EnrichedContentUnit],
    document: EnrichmentDocument,
    client: Any,
    config: EnrichmentConfig,
    prompt: dict[str, Any],
    batch_label: str,
) -> dict[str, dict[str, list[str]]]:
    """Extract keywords and metrics for one batch, splitting on omissions."""
    user_message = _format_content_batch(batch, document, prompt)
    try:
        return _call_with_retry(
            client=client,
            messages=[
                {"role": "system", "content": prompt["system_prompt"]},
                {"role": "user", "content": user_message},
            ],
            prompt=prompt,
            parser=_parse_content_extraction_response,
            retry_config=config.retries.content_extraction,
            context=f"content_extraction:{document.filename}:{batch_label}",
            validator=lambda parsed, expected=batch: _validate_unit_results(
                expected,
                parsed,
            ),
        )
    except ValueError as exc:
        if len(batch) == 1 or "content extraction ids mismatch" not in str(exc):
            raise
        midpoint = len(batch) // 2
        first_half = _extract_content_batch(
            batch[:midpoint],
            document,
            client,
            config,
            prompt,
            f"{batch_label}_split_1",
        )
        second_half = _extract_content_batch(
            batch[midpoint:],
            document,
            client,
            config,
            prompt,
            f"{batch_label}_split_2",
        )
        return {**first_half, **second_half}


def _summarize_section_batch(
    batch: list[EnrichmentSection],
    document: EnrichmentDocument,
    client: Any,
    prompt: dict[str, Any],
    retry_config: StageRetryConfig,
    context: str,
    toc_context: str,
) -> dict[str, dict[str, Any]]:
    """Summarize one section batch using the supplied TOC context."""
    user_message = _format_section_summary_batch(
        batch,
        document,
        toc_context,
        prompt,
    )
    return _call_with_retry(
        client=client,
        messages=[
            {"role": "system", "content": prompt["system_prompt"]},
            {"role": "user", "content": user_message},
        ],
        prompt=prompt,
        parser=_parse_section_summary_response,
        retry_config=retry_config,
        context=context,
        validator=lambda parsed, expected=batch: _validate_section_results(
            expected,
            parsed,
        ),
    )


def _format_doc_metadata_input(
    document: EnrichmentDocument,
    prompt: dict[str, Any],
    config: EnrichmentConfig,
) -> str:
    """Build document metadata prompt input."""
    user_input = "\n\n".join(
        [
            f"<file_metadata>\n{_build_file_metadata(document)}\n</file_metadata>",
            f"<page_names>\n{_build_page_names(document)}\n</page_names>",
            f"<layout_summary>\n{_build_layout_summary(document)}\n</layout_summary>",
            "<content>\n"
            f"{_build_metadata_content(document, config.budgets.doc_metadata_context)}"
            "\n</content>",
        ]
    )
    return prompt["user_prompt"].format(user_input=user_input)


def _format_content_batch(
    batch: list[EnrichedContentUnit],
    document: EnrichmentDocument,
    prompt: dict[str, Any],
) -> str:
    """Build keyword/metric extraction prompt input using source content."""
    source_context = _source_context(document)
    context_lines = []
    title = document.document_metadata.get("title", "")
    if title:
        context_lines.append(f'title: "{title}"')
    for key in ("data_source", "filter_1", "filter_2", "filter_3"):
        if source_context[key]:
            context_lines.append(f'{key}: "{source_context[key]}"')
    parts = []
    if context_lines:
        parts.append(
            "<document_context>\n"
            f"{'\n'.join(context_lines)}\n"
            "</document_context>"
        )
    unit_blocks = []
    for unit in batch:
        attrs = [
            f'id="{unit.content_unit_id}"',
            f'item_type="{unit.item_type or _unit_item_label(unit).lower()}"',
            f'item_number="{unit.item_number}"',
        ]
        if unit.item_type == "page" or unit.filetype == "pdf":
            attrs.append(f'page="{unit.item_number}"')
        elif unit.item_type == "sheet" or unit.filetype == "xlsx":
            attrs.append(f'sheet="{unit.item_number}"')
        if unit.item_title:
            attrs.append(f'name="{unit.item_title}"')
        if unit.chunk_context:
            attrs.append(f'context="{unit.chunk_context}"')
        unit_blocks.append(
            f"<unit {' '.join(attrs)}>\n"
            f"{unit.raw_content}\n"
            "</unit>"
        )
    parts.append(f"<content_units>\n{'\n\n'.join(unit_blocks)}\n</content_units>")
    return prompt["user_prompt"].format(user_input="\n\n".join(parts))


def _format_section_summary_batch(
    batch: list[EnrichmentSection],
    document: EnrichmentDocument,
    toc_so_far: str,
    prompt: dict[str, Any],
) -> str:
    """Build section-summary prompt input using full source-item content."""
    parts = [_build_doc_context(document)]
    if toc_so_far:
        parts.append(toc_so_far)
    section_blocks = [
        _gather_section_content(section, document.content_units) for section in batch
    ]
    parts.append(f"<sections>\n{'\n\n'.join(section_blocks)}\n</sections>")
    return prompt["user_prompt"].format(
        user_input="\n\n".join(part for part in parts if part)
    )


def _build_file_metadata(document: EnrichmentDocument) -> str:
    """Build file metadata prompt block."""
    source_context = _source_context(document)
    item_numbers = {unit.item_number for unit in document.content_units}
    item_types = sorted(
        {unit.item_type for unit in document.content_units if unit.item_type}
    )
    primary_item_label = _document_item_label(document)
    lines = [
        f"filename: {document.filename}",
        f"filetype: {document.filetype}",
        f"primary_item_type: {primary_item_label.lower()}",
        f"item_types: {', '.join(item_types) if item_types else 'unknown'}",
        f"total_items: {len(item_numbers)}",
    ]
    if primary_item_label == "Page":
        lines.append(f"total_pages: {len(item_numbers)}")
    elif primary_item_label == "Sheet":
        lines.append(f"total_sheets: {len(item_numbers)}")
    if source_context["data_source"]:
        lines.append(f"data_source: {source_context['data_source']}")
    for index, filter_key in enumerate(("filter_1", "filter_2", "filter_3"), start=1):
        if source_context[filter_key]:
            lines.append(f"filter_{index}: {source_context[filter_key]}")
    return "\n".join(lines)


def _build_page_names(document: EnrichmentDocument) -> str:
    """Build source item names prompt block."""
    seen_items = set()
    lines = []
    for unit in sorted(document.content_units, key=lambda item: item.item_number):
        if unit.item_number in seen_items:
            continue
        seen_items.add(unit.item_number)
        item_label = _unit_item_label(unit)
        lines.append(f"{item_label} {unit.item_number}: {unit.item_title}")
    return "\n".join(lines)


def _document_item_label(document: EnrichmentDocument) -> str:
    """Return the dominant source item label for a document."""
    if document.filetype == "pdf":
        return "Page"
    if document.filetype == "xlsx":
        return "Sheet"
    for unit in document.content_units:
        label = _unit_item_label(unit)
        if label != "Item":
            return label
    return "Item"


def _unit_item_label(unit: EnrichedContentUnit) -> str:
    """Return a display label for one unit's source item type."""
    item_type = unit.item_type.strip().lower()
    if item_type == "page" or unit.filetype == "pdf":
        return "Page"
    if item_type == "sheet" or unit.filetype == "xlsx":
        return "Sheet"
    if item_type:
        return item_type.replace("_", " ").title()
    return "Item"


def _build_layout_summary(document: EnrichmentDocument) -> str:
    """Build a content-shape summary by token tier."""
    counts: dict[str, int] = {}
    for unit in document.content_units:
        tier = unit.token_tier or "unknown"
        counts[tier] = counts.get(tier, 0) + 1
    return "\n".join(f"{key}: {value}" for key, value in sorted(counts.items()))


def _build_metadata_content(document: EnrichmentDocument, budget: int) -> str:
    """Collect raw item content up to the configured metadata budget."""
    parts = []
    used = 0
    for unit in sorted(
        document.content_units,
        key=lambda item: (item.item_number, item.chunk_index),
    ):
        label = f"[{unit.item_type.title()} {unit.item_number} Chunk {unit.chunk_id}]"
        unit_text = f"{label}\n{unit.raw_content}"
        token_cost = unit.raw_token_count
        if parts and used + token_cost > budget:
            break
        parts.append(unit_text)
        used += token_cost
    return "\n\n".join(parts)


def _gather_section_content(
    section: EnrichmentSection,
    units: list[EnrichedContentUnit],
) -> str:
    """Build one section content block for summarization."""
    header = (
        f'<section id="{section.section_id}" title="{section.title}" '
        f'item_start="{section.page_start}" item_end="{section.page_end}">'
    )
    unit_parts = []
    unit_lookup = {unit.content_unit_id: unit for unit in units}
    for unit_id in section.chunk_ids:
        unit = unit_lookup.get(unit_id)
        if unit is None:
            continue
        metadata_lines = []
        if unit.keywords:
            metadata_lines.append(f"keywords: {json.dumps(unit.keywords)}")
        if unit.metrics:
            metadata_lines.append(f"metrics: {json.dumps(unit.metrics)}")
        metadata = "\n".join(metadata_lines)
        unit_parts.append(
            f"{metadata}\n{unit.raw_content}" if metadata else unit.raw_content
        )
    return f"{header}\n{'\n\n'.join(unit_parts)}\n</section>"


def _build_doc_context(document: EnrichmentDocument) -> str:
    """Build document context block for summarization."""
    metadata = document.document_metadata
    lines = []
    if metadata.get("title"):
        lines.append(f'title: "{metadata["title"]}"')
    for key in ("data_source", "filter_1", "filter_2", "filter_3"):
        if metadata.get(key):
            lines.append(f'{key}: "{metadata[key]}"')
    if metadata.get("structure_type"):
        lines.append(f'structure_type: "{metadata["structure_type"]}"')
    if not lines:
        return ""
    return f"<document_context>\n{'\n'.join(lines)}\n</document_context>"


def _build_progressive_toc(completed: list[EnrichmentSection]) -> str:
    """Build progressive table-of-contents context."""
    if not completed:
        return ""
    lines = []
    for section in completed:
        snippet = section.summary[:80] if section.summary else ""
        lines.append(
            f"  [{section.section_id}] {section.title} "
            f'(p.{section.page_start}) -- "{snippet}"'
        )
    return (
        "<table_of_contents_so_far>\n"
        f"{'\n'.join(lines)}\n"
        "</table_of_contents_so_far>"
    )


def _build_section_outline(sections: list[EnrichmentSection]) -> str:
    """Build a stable section-title outline for parallel section summaries."""
    if not sections:
        return ""
    lines = [
        f"  [{section.section_id}] {section.title} (p.{section.page_start})"
        for section in sections
    ]
    return "<table_of_contents>\n" f"{'\n'.join(lines)}\n" "</table_of_contents>"


def _parse_metadata_response(response: dict[str, Any]) -> dict[str, Any]:
    """Parse document metadata tool response."""
    arguments = parse_tool_arguments(response)
    required_fields = {
        "title",
        "authors",
        "publication_date",
        "language",
        "structure_type",
        "executive_summary",
        "has_toc",
        "toc_entries",
        "rationale",
    }
    missing = required_fields - set(arguments)
    if missing:
        raise ValueError(f"Metadata response missing fields: {sorted(missing)}")
    if arguments["structure_type"] not in VALID_STRUCTURE_TYPES:
        raise ValueError(f"Invalid structure_type: {arguments['structure_type']}")
    if not isinstance(arguments["executive_summary"], str):
        raise ValueError("executive_summary must be a string")
    if not isinstance(arguments["toc_entries"], list):
        raise ValueError("toc_entries must be a list")
    return arguments


def _parse_content_extraction_response(
    response: dict[str, Any],
) -> dict[str, dict[str, list[str]]]:
    """Parse keyword/metric extraction tool response."""
    arguments = parse_tool_arguments(response)
    items = arguments.get("items")
    if not isinstance(items, list):
        raise ValueError("content extraction response missing items list")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("content extraction item must be an object")
        unit_id = item.get("unit_id")
        keywords = item.get("keywords")
        metrics = item.get("metrics")
        if not isinstance(unit_id, str) or not unit_id:
            raise ValueError("content extraction item missing unit_id")
        if not _is_string_list(keywords):
            raise ValueError(f"content extraction item {unit_id} invalid keywords")
        if not _is_string_list(metrics):
            raise ValueError(f"content extraction item {unit_id} invalid metrics")
        if unit_id in result:
            raise ValueError(f"duplicate content unit id: {unit_id}")
        result[unit_id] = {"keywords": keywords, "metrics": metrics}
    return result


def _parse_section_summary_response(
    response: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Parse section-summary tool response."""
    arguments = parse_tool_arguments(response)
    items = arguments.get("items")
    if not isinstance(items, list):
        raise ValueError("section summary response missing items list")
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("section summary item must be an object")
        section_id = item.get("section_id")
        summary = item.get("summary")
        keywords = item.get("keywords")
        metrics = item.get("metrics")
        if not isinstance(section_id, str) or not section_id:
            raise ValueError("section summary item missing section_id")
        if not isinstance(summary, str):
            raise ValueError(f"section {section_id} invalid summary")
        if not _is_string_list(keywords):
            raise ValueError(f"section {section_id} invalid keywords")
        if not _is_string_list(metrics):
            raise ValueError(f"section {section_id} invalid metrics")
        if section_id in result:
            raise ValueError(f"duplicate section id: {section_id}")
        result[section_id] = {
            "summary": summary,
            "keywords": keywords,
            "metrics": metrics,
        }
    return result


def _validate_unit_results(
    batch: list[EnrichedContentUnit],
    results: dict[str, dict[str, list[str]]],
) -> None:
    """Ensure content extraction returned exactly the requested unit ids."""
    expected = {unit.content_unit_id for unit in batch}
    actual = set(results)
    if expected != actual:
        raise ValueError(
            "content extraction ids mismatch: "
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )


def _validate_section_results(
    batch: list[EnrichmentSection],
    results: dict[str, dict[str, Any]],
) -> None:
    """Ensure section summary returned exactly the requested section ids."""
    expected = {section.section_id for section in batch}
    actual = set(results)
    if expected != actual:
        raise ValueError(
            "section summary ids mismatch: "
            f"missing={sorted(expected - actual)} extra={sorted(actual - expected)}"
        )


def _call_with_retry[T](
    client: Any,
    messages: list[dict[str, Any]],
    prompt: dict[str, Any],
    parser: Callable[[dict[str, Any]], T],
    retry_config: StageRetryConfig,
    context: str,
    validator: Callable[[T], None] | None = None,
) -> T:
    """Call an OpenAI-compatible tool request and parse with retries."""
    errors = _retryable_errors()
    for attempt in range(1, retry_config.max_retries + 1):
        try:
            response = client.call(
                messages=messages,
                stage=prompt["stage"],
                tools=prompt.get("tools"),
                tool_choice=prompt.get("tool_choice", "required"),
                context=f"{context}:attempt_{attempt}",
            )
            parsed = parser(response)
            if validator is not None:
                validator(parsed)
            return parsed
        except errors:
            if attempt == retry_config.max_retries:
                raise
            time.sleep(retry_config.retry_delay_seconds * attempt)
    raise RuntimeError(f"{context} exited retry loop without response")


def parse_tool_arguments(response: dict[str, Any]) -> dict[str, Any]:
    """Extract JSON function arguments from a chat tool-call response."""
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LLM response missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("LLM response missing message")
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        raise ValueError(_missing_tool_call_message(choices[0], message))
    function_data = tool_calls[0].get("function")
    if not isinstance(function_data, dict):
        raise ValueError("LLM response missing function data")
    raw_arguments = function_data.get("arguments")
    if not isinstance(raw_arguments, str):
        raise ValueError("LLM response missing function arguments")
    parsed = json.loads(raw_arguments)
    if not isinstance(parsed, dict):
        raise ValueError("LLM function arguments must decode to an object")
    return parsed


def _missing_tool_call_message(
    choice: dict[str, Any],
    message: dict[str, Any],
) -> str:
    """Return a concise diagnostic for non-tool LLM responses."""
    finish_reason = choice.get("finish_reason", "")
    message_keys = ", ".join(sorted(str(key) for key in message))
    return (
        "LLM response missing tool calls "
        f"(finish_reason={finish_reason!r}, message_keys=[{message_keys}])"
    )


def _load_enrichment_document(
    progress_dir: Path,
    record: ManifestRecord,
    max_content_units: int | None,
) -> EnrichmentDocument:
    """Build one enrichment document from one file's chunking artifacts."""
    chunking_root = _artifact_root(progress_dir, record.file_id) / CHUNKING_DIR_NAME
    chunking_path = chunking_root / CHUNKING_ARTIFACT_FILE_NAME
    chunking_payload = _read_required_json(chunking_path)
    chunks_jsonl_value = (
        chunking_payload.get("chunks_jsonl_path")
        or chunking_root / CHUNKS_JSONL_FILE_NAME
    )
    chunks_jsonl_path = Path(str(chunks_jsonl_value))
    chunk_records = _read_jsonl(chunks_jsonl_path)
    if max_content_units is not None:
        chunk_records = chunk_records[:max_content_units]
    source = _source_from_record(record, chunk_records)
    content_units = [
        _content_unit_from_chunk(chunk_record, source) for chunk_record in chunk_records
    ]
    return EnrichmentDocument(
        source=source,
        source_stage_run_id=str(
            chunking_payload.get("source_stage_run_id", "")
        ),
        source_artifact_root=Path(
            str(chunking_payload.get("source_workbook_artifact_path", ""))
        ).parent,
        chunking_artifact_root=chunking_root,
        chunks_jsonl_path=chunks_jsonl_path,
        content_units=content_units,
    )


def _source_from_record(
    record: ManifestRecord,
    chunk_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build Aegis-compatible source metadata plus local source fields."""
    chunk_source = (
        dict(chunk_records[0]["source"])
        if chunk_records and isinstance(chunk_records[0].get("source"), dict)
        else {}
    )
    source = {
        "source_type": chunk_source.get("source_type") or record.data_source,
        "period": chunk_source.get("period")
        or f"{record.fiscal_year}_{record.quarter}",
        "ticker": chunk_source.get("ticker") or record.bank,
        "filename": chunk_source.get("filename") or record.file_name,
        "file_path": chunk_source.get("file_path") or record.file_path,
        "filetype": chunk_source.get("filetype") or record.file_type,
        "file_hash": chunk_source.get("file_hash") or record.file_hash,
        "file_id": record.file_id,
        "data_source": record.data_source,
        "fiscal_year": record.fiscal_year,
        "quarter": record.quarter,
        "bank": record.bank,
        "file_name": record.file_name,
        "file_type": record.file_type,
        "file_size": record.file_size,
        "date_last_modified": record.date_last_modified,
    }
    return source


def _content_unit_from_chunk(
    chunk_record: dict[str, Any],
    source: dict[str, Any],
) -> EnrichedContentUnit:
    """Convert one Stage 4 chunk record into an enrichable content unit."""
    chunk_id = str(chunk_record["chunk_id"])
    return EnrichedContentUnit(
        source=source,
        source_run_id=str(chunk_record.get("source_run_id", "")),
        source_artifact_root=Path(str(chunk_record.get("source_artifact_root", ""))),
        filetype=str(chunk_record.get("filetype", source.get("filetype", ""))),
        item_type=str(chunk_record.get("item_type", "sheet")),
        item_number=int(
            chunk_record.get("item_number") or chunk_record["sheet_number"]
        ),
        item_title=str(
            chunk_record.get("item_title") or chunk_record.get("sheet_title", "")
        ),
        content_unit_id=chunk_id,
        chunk_id=chunk_id,
        chunk_index=int(chunk_record.get("chunk_index", 0)),
        raw_content=str(chunk_record.get("content", "")),
        embedding_content=str(chunk_record.get("embedding_content", "")),
        chunk_context=str(chunk_record.get("chunk_context", "")),
        chunk_header=str(chunk_record.get("chunk_header", "")),
        sheet_passthrough_content=str(
            chunk_record.get("sheet_passthrough_content", "")
        ),
        section_passthrough_content=str(
            chunk_record.get("section_passthrough_content", "")
        ),
        raw_token_count=int(
            chunk_record.get("raw_token_count")
            or chunk_record.get("content_token_count")
            or 0
        ),
        embedding_token_count=int(chunk_record.get("embedding_token_count", 0)),
        token_tier=str(chunk_record.get("token_tier", "")),
    )


def _batch_units(
    units: list[EnrichedContentUnit],
    budget: int,
) -> list[list[EnrichedContentUnit]]:
    """Batch content units by raw token count."""
    batches = []
    current_batch = []
    current_tokens = 0
    for unit in units:
        token_cost = unit.raw_token_count or unit.embedding_token_count
        if current_batch and current_tokens + token_cost > budget:
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
        current_batch.append(unit)
        current_tokens += token_cost
    if current_batch:
        batches.append(current_batch)
    return batches


def _batch_sections(
    sections: list[EnrichmentSection],
    budget: int,
) -> list[list[EnrichmentSection]]:
    """Batch sections by token count."""
    batches = []
    current_batch = []
    current_tokens = 0
    for section in sections:
        token_cost = section.token_count
        if current_batch and current_tokens + token_cost > budget:
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
        current_batch.append(section)
        current_tokens += token_cost
    if current_batch:
        batches.append(current_batch)
    return batches


def _resolve_workbook_workers(requested_workers: int, record_count: int) -> int:
    """Return the bounded document worker count for this enrichment run."""
    if record_count <= 0:
        return 0
    return max(1, min(requested_workers, record_count))


def _parallel_worker_count(config: EnrichmentConfig, item_count: int) -> int:
    """Return worker count for independent in-document enrichment batches."""
    if item_count <= 0:
        return 0
    return max(1, min(config.max_sheet_workers, item_count))


def _sheet_count(document: EnrichmentDocument) -> int:
    """Return distinct source-item count represented by content units."""
    return len({unit.item_number for unit in document.content_units})


def _section_count(document: EnrichmentDocument) -> int:
    """Return section count represented by the enrichment document."""
    return sum(1 for section in document.sections if section.level == "section")


def _unit_ids_by_sheet(
    units: list[EnrichedContentUnit],
) -> dict[int, frozenset[str]]:
    """Return content-unit ids grouped by source item number."""
    grouped: dict[int, set[str]] = defaultdict(set)
    for unit in units:
        grouped[unit.item_number].add(unit.content_unit_id)
    return {sheet: frozenset(unit_ids) for sheet, unit_ids in grouped.items()}


def _completed_sheet_count(
    unit_ids_by_sheet: dict[int, frozenset[str]],
    completed_unit_ids: set[str],
) -> int:
    """Return source items whose content units have all finished enrichment."""
    return sum(
        1
        for unit_ids in unit_ids_by_sheet.values()
        if unit_ids <= completed_unit_ids
    )


def _group_units_by_item(
    units: list[EnrichedContentUnit],
) -> dict[int, list[EnrichedContentUnit]]:
    """Return content units grouped by source item number."""
    grouped: dict[int, list[EnrichedContentUnit]] = defaultdict(list)
    for unit in units:
        grouped[unit.item_number].append(unit)
    for group in grouped.values():
        group.sort(key=lambda unit: unit.chunk_index)
    return dict(grouped)


def _normalize_source_metadata(
    document: EnrichmentDocument,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Apply deterministic source-derived metadata where useful."""
    normalized = dict(metadata)
    source_title = _source_title(document)
    if source_title:
        normalized["title"] = source_title
    normalized["structure_type"] = _source_structure_type(
        document,
        str(normalized.get("structure_type", "")),
    )
    if not str(normalized.get("executive_summary", "")).strip():
        normalized["executive_summary"] = _fallback_executive_summary(document)
    return normalized


def _source_title(document: EnrichmentDocument) -> str:
    """Build a stable title from manifest source fields."""
    bank = str(document.source.get("ticker") or document.source.get("bank") or "")
    period = str(document.source.get("period") or "")
    if not bank or "_" not in period:
        return ""
    year, quarter = period.split("_", 1)
    data_source = _normalized_data_source(document)
    if data_source == "investor-slides":
        source_name = "Investor Slides"
    elif data_source == "financial-supp":
        source_name = "Financial Supplement"
    else:
        source_name = data_source.replace("-", " ").title() if data_source else ""
    return f"{bank.upper()} {year} {quarter.upper()} {source_name}".strip()


def _source_structure_type(document: EnrichmentDocument, proposed: str) -> str:
    """Return a source-aware structure type accepted by downstream code."""
    data_source = _normalized_data_source(document)
    if document.filetype == "xlsx":
        return "sheet_based"
    if data_source == "investor-slides":
        return "slide_deck"
    if document.filetype == "pdf":
        return "page_based"
    return proposed if proposed in VALID_STRUCTURE_TYPES else "semantic"


def _fallback_executive_summary(document: EnrichmentDocument) -> str:
    """Build a concise fallback summary when the model returns no summary."""
    title = _source_title(document) or document.filename
    item_label = _document_item_label(document).lower()
    item_count = len({unit.item_number for unit in document.content_units})
    plural_label = f"{item_label}s" if item_count != 1 else item_label
    return f"{title} contains {item_count} {plural_label} prepared for retrieval."


def _normalized_data_source(document: EnrichmentDocument) -> str:
    """Return a normalized manifest data-source label."""
    value = str(
        document.source.get("data_source")
        or document.source.get("source_type")
        or ""
    )
    return value.strip().replace("_", "-").lower()


def _source_context(document: EnrichmentDocument) -> dict[str, str]:
    """Map source fields to Aegis prompt context fields."""
    return {
        "data_source": str(document.source.get("source_type", "")),
        "filter_1": str(document.source.get("period", "")),
        "filter_2": str(document.source.get("ticker", "")),
        "filter_3": "",
    }


def _enrichment_manifest_record(
    record: ManifestRecord,
    document: EnrichmentDocument,
    document_path: Path,
    content_units_path: Path,
    sections_path: Path,
    metadata_path: Path,
    processed_at: str,
    duration_seconds: float,
) -> dict[str, Any]:
    """Build a per-document enrichment manifest record."""
    return {
        "stage": "enrichment",
        "processed_at": processed_at,
        "duration_seconds": round(duration_seconds, 6),
        "source": document.source,
        "file_id": record.file_id,
        "status": "enriched",
        "source_stage": "chunking",
        "chunking_artifact_root": str(document.chunking_artifact_root),
        "chunks_jsonl_path": str(document.chunks_jsonl_path),
        "artifact_root": str(document_path.parent),
        "content_units_jsonl_path": str(content_units_path),
        "sections_jsonl_path": str(sections_path),
        "document_metadata_json_path": str(metadata_path),
        "document_json_path": str(document_path),
        "content_unit_count": len(document.content_units),
        "section_count": sum(
            1 for section in document.sections if section.level == "section"
        ),
        "subsection_count": sum(
            1 for section in document.sections if section.level == "subsection"
        ),
        "keyword_count": sum(len(unit.keywords) for unit in document.content_units),
        "metric_count": sum(len(unit.metrics) for unit in document.content_units),
    }


def _is_string_list(value: Any) -> bool:
    """Return whether value is a list of strings."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _retryable_errors() -> tuple[type[BaseException], ...]:
    """Return retryable OpenAI exception classes plus validation errors."""
    try:
        openai_module = __import__("openai")
    except ModuleNotFoundError:
        return (ValueError,)
    errors = []
    for error_name in (
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
    ):
        error_class = getattr(openai_module, error_name, None)
        if isinstance(error_class, type) and issubclass(error_class, BaseException):
            errors.append(error_class)
    return (*errors, ValueError)


def _default_client_factory() -> LLMClient:
    """Return the configured project LLM client."""
    return LLMClient(verify_ssl=get_ssl_verify())


def _load_process_records(progress_dir: Path) -> tuple[ManifestRecord, ...]:
    """Load manifest records selected for processing."""
    path = progress_dir / FILES_TO_PROCESS_FILE_NAME
    payload = _read_required_json(path)
    rows = payload.get("files_to_process")
    if not isinstance(rows, list):
        raise EnrichmentStageError(f"{path} is missing files_to_process list")
    return tuple(_manifest_record_from_mapping(row, path) for row in rows)


def _manifest_record_from_mapping(row: Any, source_path: Path) -> ManifestRecord:
    """Convert one progress JSON row into a manifest record."""
    if not isinstance(row, dict):
        raise EnrichmentStageError(f"Progress row is not an object in {source_path}")
    missing = [field for field in MANIFEST_FIELDS if field not in row]
    if missing:
        raise EnrichmentStageError(
            f"Progress row missing fields in {source_path}: {', '.join(missing)}"
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


def _artifact_root(progress_dir: Path, file_id: str) -> Path:
    """Return the per-file artifact root created by the manifest stage."""
    return progress_dir / ARTIFACTS_DIR_NAME / file_id


def _read_required_json(path: Path) -> Any:
    """Read required UTF-8 JSON and raise on failure."""
    if not path.is_file():
        raise EnrichmentStageError(f"Required JSON artifact is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EnrichmentStageError(f"Invalid JSON artifact: {path}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSONL records from a required artifact."""
    if not path.is_file():
        raise EnrichmentStageError(f"Required JSONL artifact is missing: {path}")
    rows = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EnrichmentStageError(
                f"Invalid JSONL at {path}:{line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise EnrichmentStageError(
                f"JSONL row is not an object: {path}:{line_number}"
            )
        rows.append(row)
    return rows


def _write_json(path: Path, payload: Any) -> None:
    """Write deterministic UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write deterministic UTF-8 JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _utc_now() -> str:
    """Return the current UTC time as an ISO string."""
    return datetime.now(tz=UTC).isoformat()
