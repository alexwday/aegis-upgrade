"""Generate final retriever embeddings from enrichment artifacts.

This stage runs after enrichment. It keeps the Aegis Retriever row-level
embedding families, adapted for this RTS PDF pipeline:
content-unit text, keyword text, metric text, section summaries, and document
summary. It also writes a long-form embedding index so individual keywords and
individual metrics can be searched as focused facets and mapped back to chunks.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from connections.llm_connector import LLMClient
from utils.config_setup import (
    EmbeddingConfig,
    get_embedding_config,
    get_ssl_verify,
    load_config,
)
from utils.logging_setup import get_stage_logger
from .enrichment import (
    DOCUMENT_ARTIFACT_FILE_NAME,
    ENRICHMENT_DIR_NAME,
    EnrichedContentUnit,
    EnrichmentDocument,
    EnrichmentSection,
)
from .manifest import (
    ARTIFACTS_DIR_NAME,
    FILES_TO_PROCESS_FILE_NAME,
    PROGRESS_DIR,
    ManifestRecord,
)

EMBEDDINGS_DIR_NAME = "embeddings"
EMBEDDING_ARTIFACT_FILE_NAME = "embeddings.json"
EMBEDDING_MANIFEST_FILE_NAME = "embeddings_manifest.json"
CONTENT_ROWS_JSONL_FILE_NAME = "content_rows.jsonl"
EMBEDDING_INDEX_JSONL_FILE_NAME = "embedding_index.jsonl"
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


class EmbeddingStageError(RuntimeError):
    """Raised when final embedding artifacts cannot be generated."""


@dataclass(frozen=True)
class TermEmbedding:
    """One individual keyword or metric embedding tied to a content unit."""

    term: str
    vector: list[float]


@dataclass(frozen=True)
class DocumentEmbeddingVectors:
    """Embedding vectors generated for one enriched document."""

    content: list[list[float]]
    keyword_groups: list[list[float]]
    metric_groups: list[list[float]]
    keyword_terms: list[list[TermEmbedding]]
    metric_terms: list[list[TermEmbedding]]
    sections: dict[str, list[float]]
    document_summary: list[float]


@dataclass(frozen=True)
class EmbeddingStageResult:
    """Summary returned after writing final embedding artifacts."""

    processed_file_count: int
    content_unit_count: int
    embedding_index_count: int
    embedding_artifact_paths: tuple[Path, ...]


@dataclass(frozen=True)
class EmbeddingPlan:
    """Planned embedding API workload for one document."""

    content: int
    keyword_groups: int
    metric_groups: int
    keyword_terms: int
    metric_terms: int
    section_summaries: int
    document_summary: int
    total_texts: int
    total_batches: int


@dataclass
class _EmbeddingProgress:
    """Progress state for one workbook's embedding batches."""

    index: int
    total_files: int
    file_id: str
    file_name: str
    total_steps: int
    total_texts: int
    phase: str = "embedding"
    completed_steps: int = 0
    completed: bool = False
    failed: bool = False


class _EmbeddingProgressTracker:
    """Emit compact aggregate embedding progress for sequential workbooks."""

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
        self._states: dict[str, _EmbeddingProgress] = {}
        self._last_logged = 0.0

    def start(self, index: int, record: ManifestRecord, plan: EmbeddingPlan) -> None:
        """Track one workbook as active and log the start."""
        state = _EmbeddingProgress(
            index=index,
            total_files=self._total_files,
            file_id=record.file_id,
            file_name=record.file_name,
            total_steps=plan.total_batches,
            total_texts=plan.total_texts,
        )
        self._states[record.file_id] = state
        self._logger.info(
            "Started workbook %d/%d: %s content_units=%d texts=%d batches=%d",
            index,
            self._total_files,
            record.file_name,
            plan.content,
            plan.total_texts,
            plan.total_batches,
        )
        self.update(record.file_id, force=True)

    def advance(self, file_id: str, steps: int = 1) -> None:
        """Increment completed batch steps and maybe log a snapshot."""
        state = self._states.get(file_id)
        if state is None:
            return
        state.completed_steps = min(state.total_steps, state.completed_steps + steps)
        self._log_snapshot(force=False)

    def complete(
        self,
        record: ManifestRecord,
        duration_seconds: float,
        content_unit_count: int,
        index_row_count: int,
    ) -> None:
        """Mark a workbook complete and log final per-file progress."""
        state = self._states.get(record.file_id)
        if state is None:
            return
        state.phase = "complete"
        state.completed = True
        state.completed_steps = state.total_steps
        completed = self._completed_count()
        self._logger.info(
            "Finished workbook %d/%d: %s content_units=%d index_rows=%d "
            "duration=%.1fs",
            completed,
            self._total_files,
            record.file_name,
            content_unit_count,
            index_row_count,
            duration_seconds,
        )
        self.update(record.file_id, force=True)

    def fail(self, file_id: str) -> None:
        """Mark a workbook failed so snapshots do not show it as active."""
        state = self._states.get(file_id)
        if state is None:
            return
        state.phase = "failed"
        state.failed = True
        self._log_snapshot(force=True)

    def update(self, file_id: str, *, force: bool = False) -> None:
        """Maybe log an aggregate progress snapshot."""
        if file_id not in self._states:
            return
        self._log_snapshot(force=force)

    def _log_snapshot(self, force: bool = False) -> None:
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
        self._logger.info("Embeddings progress: %s", self._render())

    def _render(self) -> str:
        """Render compact active-workbook progress for one log line."""
        completed = self._completed_count()
        active_states = [
            state
            for state in self._states.values()
            if not state.completed and not state.failed
        ]
        active_states.sort(key=lambda state: state.index)
        if not active_states:
            return f"files {completed}/{self._total_files} complete"
        active = " | ".join(
            _render_embedding_progress_state(state) for state in active_states
        )
        return f"files {completed}/{self._total_files} complete | {active}"

    def _completed_count(self) -> int:
        """Return completed workbook count."""
        return sum(1 for state in self._states.values() if state.completed)


def _render_embedding_progress_state(state: _EmbeddingProgress) -> str:
    """Render one workbook's embedding batch progress."""
    return (
        f"{state.file_id}: {state.phase} "
        f"steps {state.completed_steps}/{state.total_steps}"
    )


def run_embedding_stage(
    progress_dir: Path = PROGRESS_DIR,
    config: EmbeddingConfig | None = None,
    client_factory: Callable[[], Any] | None = None,
    max_files: int | None = None,
    max_content_units_per_file: int | None = None,
) -> EmbeddingStageResult:
    """Generate embeddings for enriched content units.

    Args:
        progress_dir: Folder containing manifest progress and artifacts.
        config: Optional embedding model, dimension, and batch settings.
        client_factory: Optional factory returning a client with ``embed``.
            When omitted, a configured ``LLMClient`` is created and live
            embedding requests are made.
        max_files: Optional file limit for deterministic smoke checks.
        max_content_units_per_file: Optional per-file content-unit limit for
            smoke checks.

    Returns:
        EmbeddingStageResult with per-workbook artifact paths and counts.

    Raises:
        EmbeddingStageError: If enrichment artifacts are missing or malformed.

    External side effects:
        Writes artifacts under ``progress_dir/artifacts/<file_id>/embeddings``.
        Performs live embedding API calls when ``client_factory`` is omitted.
    """
    logger = get_stage_logger(__name__, "EMBEDDINGS")
    load_config()
    embedding_config = config or get_embedding_config()
    records = list(_load_process_records(progress_dir))
    if max_files is not None:
        records = records[:max_files]
    processed_at = _utc_now()
    client_factory = client_factory or _default_client_factory

    if not records:
        logger.info("Embeddings complete: files=0 content_units=0 index_rows=0")
        return EmbeddingStageResult(
            processed_file_count=0,
            content_unit_count=0,
            embedding_index_count=0,
            embedding_artifact_paths=(),
        )

    client = client_factory()
    artifact_paths: list[Path] = []
    content_unit_count = 0
    embedding_index_count = 0
    progress = _EmbeddingProgressTracker(
        logger,
        total_files=len(records),
        interval_seconds=embedding_config.progress_log_interval_seconds,
    )

    logger.info(
        "Embeddings queued: files=%d batch_size=%d progress_interval=%.1fs",
        len(records),
        embedding_config.batch_size,
        embedding_config.progress_log_interval_seconds,
    )

    for index, record in enumerate(records, start=1):
        started_at = _utc_now()
        start = time.perf_counter()
        try:
            document, enrichment_path = _load_embedding_document(
                progress_dir,
                record,
                max_content_units=max_content_units_per_file,
            )
            plan = _build_embedding_plan(document, embedding_config)
            progress.start(index, record, plan)
            vectors = _embed_document(
                document,
                client,
                embedding_config,
                progress_callback=lambda file_id=record.file_id: progress.advance(
                    file_id
                ),
            )
            content_rows = _build_document_rows(document, vectors, processed_at)
            index_rows = _build_embedding_index_rows(
                document,
                vectors,
                config=embedding_config,
                processed_at=processed_at,
            )
        except Exception:
            progress.fail(record.file_id)
            raise
        completed_at = _utc_now()
        duration_seconds = time.perf_counter() - start
        artifact_path = write_embedding_artifacts(
            record=record,
            document=document,
            enrichment_path=enrichment_path,
            embedding_root=_artifact_root(progress_dir, record.file_id)
            / EMBEDDINGS_DIR_NAME,
            content_rows=content_rows,
            index_rows=index_rows,
            config=embedding_config,
            processed_at=processed_at,
            file_started_at=started_at,
            file_completed_at=completed_at,
            duration_seconds=duration_seconds,
        )
        artifact_paths.append(artifact_path)
        content_unit_count += len(document.content_units)
        embedding_index_count += len(index_rows)
        progress.complete(
            record=record,
            duration_seconds=duration_seconds,
            content_unit_count=len(document.content_units),
            index_row_count=len(index_rows),
        )

    logger.info(
        "Embeddings complete: files=%d content_units=%d index_rows=%d",
        len(records),
        content_unit_count,
        embedding_index_count,
    )
    return EmbeddingStageResult(
        processed_file_count=len(records),
        content_unit_count=content_unit_count,
        embedding_index_count=embedding_index_count,
        embedding_artifact_paths=tuple(artifact_paths),
    )


def write_embedding_artifacts(
    record: ManifestRecord,
    document: EnrichmentDocument,
    enrichment_path: Path,
    embedding_root: Path,
    content_rows: list[dict[str, Any]],
    index_rows: list[dict[str, Any]],
    config: EmbeddingConfig,
    processed_at: str,
    file_started_at: str,
    file_completed_at: str,
    duration_seconds: float,
) -> Path:
    """Write per-workbook final embedding artifacts."""
    embedding_root.mkdir(parents=True, exist_ok=True)
    content_rows_path = embedding_root / CONTENT_ROWS_JSONL_FILE_NAME
    embedding_index_path = embedding_root / EMBEDDING_INDEX_JSONL_FILE_NAME
    artifact_path = embedding_root / EMBEDDING_ARTIFACT_FILE_NAME
    _write_jsonl(content_rows_path, content_rows)
    _write_jsonl(embedding_index_path, index_rows)

    counts = _embedding_counts(content_rows, index_rows)
    artifact = {
        "stage": "embeddings",
        "processed_at": processed_at,
        "file_started_at": file_started_at,
        "file_completed_at": file_completed_at,
        "duration_seconds": round(duration_seconds, 6),
        "source": document.source,
        "source_stage": "enrichment",
        "source_enrichment_artifact_path": str(enrichment_path),
        "embedding_model": config.model,
        "embedding_dimensions": config.dimensions,
        "embedding_batch_size": config.batch_size,
        "content_unit_count": len(document.content_units),
        "embedding_index_count": len(index_rows),
        "counts": counts,
        "content_rows_jsonl_path": str(content_rows_path),
        "embedding_index_jsonl_path": str(embedding_index_path),
    }
    _write_json(artifact_path, artifact)
    _write_json(
        embedding_root / EMBEDDING_MANIFEST_FILE_NAME,
        {
            "stage": artifact["stage"],
            "processed_at": artifact["processed_at"],
            "source": artifact["source"],
            "file_id": record.file_id,
            "status": "embedded",
            "source_stage": artifact["source_stage"],
            "source_enrichment_artifact_path": artifact[
                "source_enrichment_artifact_path"
            ],
            "artifact_root": str(embedding_root),
            "embedding_artifact_path": str(artifact_path),
            "content_rows_jsonl_path": str(content_rows_path),
            "embedding_index_jsonl_path": str(embedding_index_path),
            "duration_seconds": artifact["duration_seconds"],
            "content_unit_count": artifact["content_unit_count"],
            "embedding_index_count": artifact["embedding_index_count"],
            "counts": counts,
        },
    )
    return artifact_path


def _build_embedding_plan(
    document: EnrichmentDocument,
    config: EmbeddingConfig,
) -> EmbeddingPlan:
    """Build a compact workload plan for progress logging."""
    sections_by_unit = {
        unit.content_unit_id: _find_section(unit, document.sections)
        for unit in document.content_units
    }
    content_texts = [
        _build_content_text(unit, sections_by_unit[unit.content_unit_id])
        for unit in document.content_units
    ]
    keyword_terms = [_unique_terms(unit.keywords) for unit in document.content_units]
    metric_terms = [_unique_terms(unit.metrics) for unit in document.content_units]
    keyword_group_texts = [_join_terms(terms) for terms in keyword_terms]
    metric_group_texts = [_join_terms(terms) for terms in metric_terms]
    keyword_term_texts = [term for terms in keyword_terms for term in terms]
    metric_term_texts = [term for terms in metric_terms for term in terms]
    section_texts = list(_build_section_summary_texts(document.sections).values())
    document_summary = str(document.document_metadata.get("executive_summary", ""))
    counts = {
        "content": _non_empty_text_count(content_texts),
        "keyword_groups": _non_empty_text_count(keyword_group_texts),
        "metric_groups": _non_empty_text_count(metric_group_texts),
        "keyword_terms": _non_empty_text_count(keyword_term_texts),
        "metric_terms": _non_empty_text_count(metric_term_texts),
        "section_summaries": _non_empty_text_count(section_texts),
        "document_summary": _non_empty_text_count([document_summary]),
    }
    total_batches = sum(
        _estimated_batch_count(count, config.batch_size) for count in counts.values()
    )
    return EmbeddingPlan(
        content=counts["content"],
        keyword_groups=counts["keyword_groups"],
        metric_groups=counts["metric_groups"],
        keyword_terms=counts["keyword_terms"],
        metric_terms=counts["metric_terms"],
        section_summaries=counts["section_summaries"],
        document_summary=counts["document_summary"],
        total_texts=sum(counts.values()),
        total_batches=total_batches,
    )


def _embed_document(
    document: EnrichmentDocument,
    client: Any,
    config: EmbeddingConfig,
    progress_callback: Callable[[], None] | None = None,
) -> DocumentEmbeddingVectors:
    """Generate all embedding families for one enriched document."""
    sections_by_unit = {
        unit.content_unit_id: _find_section(unit, document.sections)
        for unit in document.content_units
    }
    content_texts = [
        _build_content_text(unit, sections_by_unit[unit.content_unit_id])
        for unit in document.content_units
    ]
    keyword_terms = [_unique_terms(unit.keywords) for unit in document.content_units]
    metric_terms = [_unique_terms(unit.metrics) for unit in document.content_units]
    keyword_group_texts = [_join_terms(terms) for terms in keyword_terms]
    metric_group_texts = [_join_terms(terms) for terms in metric_terms]
    section_texts = _build_section_summary_texts(document.sections)
    section_ids = list(section_texts)
    document_summary = str(document.document_metadata.get("executive_summary", ""))

    content_vectors = _batch_embed(
        client,
        content_texts,
        config,
        progress_callback,
    )
    keyword_group_vectors = _batch_embed(
        client,
        keyword_group_texts,
        config,
        progress_callback,
    )
    metric_group_vectors = _batch_embed(
        client,
        metric_group_texts,
        config,
        progress_callback,
    )
    keyword_term_vectors = _embed_terms(
        client,
        keyword_terms,
        config,
        progress_callback,
    )
    metric_term_vectors = _embed_terms(
        client,
        metric_terms,
        config,
        progress_callback,
    )
    section_vectors = _batch_embed(
        client,
        [section_texts[section_id] for section_id in section_ids],
        config,
        progress_callback,
    )
    summary_vectors = _batch_embed(
        client,
        [document_summary],
        config,
        progress_callback,
    )

    return DocumentEmbeddingVectors(
        content=content_vectors,
        keyword_groups=keyword_group_vectors,
        metric_groups=metric_group_vectors,
        keyword_terms=keyword_term_vectors,
        metric_terms=metric_term_vectors,
        sections=dict(zip(section_ids, section_vectors, strict=True)),
        document_summary=summary_vectors[0] if summary_vectors else [],
    )


def _batch_embed(
    client: Any,
    texts: list[str],
    config: EmbeddingConfig,
    progress_callback: Callable[[], None] | None,
) -> list[list[float]]:
    """Embed non-empty texts in batches while preserving input order."""
    results: list[list[float]] = [[] for _ in texts]
    non_empty = [
        (index, text) for index, text in enumerate(texts) if text and text.strip()
    ]
    if not non_empty:
        return results

    vectors: list[list[float]] = []
    for start in range(0, len(non_empty), config.batch_size):
        batch = [text for _, text in non_empty[start : start + config.batch_size]]
        batch_vectors = client.embed(
            batch,
            model=config.model,
            dimensions=config.dimensions,
        )
        if len(batch_vectors) != len(batch):
            raise EmbeddingStageError(
                "Embedding client returned "
                f"{len(batch_vectors)} vectors for {len(batch)} texts"
            )
        vectors.extend(batch_vectors)
        if progress_callback is not None:
            progress_callback()

    for vector_index, (original_index, _) in enumerate(non_empty):
        results[original_index] = vectors[vector_index]
    return results


def _embed_terms(
    client: Any,
    terms_by_unit: list[list[str]],
    config: EmbeddingConfig,
    progress_callback: Callable[[], None] | None,
) -> list[list[TermEmbedding]]:
    """Embed individual keyword or metric terms while preserving unit order."""
    results: list[list[TermEmbedding]] = [[] for _ in terms_by_unit]
    positions: list[tuple[int, str]] = []
    for unit_index, terms in enumerate(terms_by_unit):
        for term in terms:
            positions.append((unit_index, term))

    vectors = _batch_embed(
        client,
        [term for _, term in positions],
        config,
        progress_callback,
    )
    for (unit_index, term), vector in zip(positions, vectors, strict=True):
        if vector:
            results[unit_index].append(TermEmbedding(term=term, vector=vector))
    return results


def _build_document_rows(
    document: EnrichmentDocument,
    vectors: DocumentEmbeddingVectors,
    processed_at: str,
) -> list[dict[str, Any]]:
    """Build one final retrieval-facing row per content unit."""
    rows = []
    for index, unit in enumerate(document.content_units):
        section = _find_section(unit, document.sections)
        row = {
            "row_id": _row_id(document.source, unit),
            **_source_fields(document.source),
            "is_current": True,
            **_content_unit_fields(unit),
            **_section_fields(section),
            **_document_fields(document),
            "content_embedding_json": vectors.content[index],
            "keyword_embedding_json": vectors.keyword_groups[index],
            "metric_embedding_json": vectors.metric_groups[index],
            "section_summary_embedding_json": vectors.sections.get(
                unit.section_id,
                [],
            ),
            "document_summary_embedding_json": vectors.document_summary,
            "created_at": processed_at,
        }
        rows.append(row)
    return rows


def _build_embedding_index_rows(
    document: EnrichmentDocument,
    vectors: DocumentEmbeddingVectors,
    config: EmbeddingConfig,
    processed_at: str,
) -> list[dict[str, Any]]:
    """Build long-form embedding index rows for all searchable facets."""
    rows: list[dict[str, Any]] = []
    section_lookup = {
        section.section_id: section
        for section in document.sections
        if section.section_id
    }

    for index, unit in enumerate(document.content_units):
        section = section_lookup.get(unit.section_id)
        content_text = _build_content_text(unit, section)
        rows.extend(
            _content_unit_index_rows(
                document=document,
                unit=unit,
                content_text=content_text,
                content_vector=vectors.content[index],
                keyword_terms=vectors.keyword_terms[index],
                metric_terms=vectors.metric_terms[index],
                config=config,
                processed_at=processed_at,
            )
        )

    for section in document.sections:
        vector = vectors.sections.get(section.section_id, [])
        if not vector:
            continue
        text = _build_section_summary_text(section)
        rows.append(
            _index_row(
                document=document,
                embedding_id=_section_embedding_id(document.source, section),
                embedding_type="section_summary",
                embedding_scope="section",
                text=text,
                vector=vector,
                config=config,
                processed_at=processed_at,
                section_id=section.section_id,
                content_unit_ids=list(section.chunk_ids),
            )
        )

    if vectors.document_summary:
        text = str(document.document_metadata.get("executive_summary", ""))
        rows.append(
            _index_row(
                document=document,
                embedding_id=f"{_source_key(document.source)}::document_summary",
                embedding_type="document_summary",
                embedding_scope="document",
                text=text,
                vector=vectors.document_summary,
                config=config,
                processed_at=processed_at,
                content_unit_ids=[
                    unit.content_unit_id for unit in document.content_units
                ],
            )
        )
    return rows


def _content_unit_index_rows(
    document: EnrichmentDocument,
    unit: EnrichedContentUnit,
    content_text: str,
    content_vector: list[float],
    keyword_terms: list[TermEmbedding],
    metric_terms: list[TermEmbedding],
    config: EmbeddingConfig,
    processed_at: str,
) -> list[dict[str, Any]]:
    """Build long-form facet rows for one content unit."""
    rows = []
    row_id = _row_id(document.source, unit)
    if content_vector:
        rows.append(
            _index_row(
                document=document,
                embedding_id=f"{row_id}::content",
                embedding_type="content",
                embedding_scope="content_unit",
                text=content_text,
                vector=content_vector,
                config=config,
                processed_at=processed_at,
                unit=unit,
            )
        )

    for ordinal, term_embedding in enumerate(keyword_terms, start=1):
        rows.append(
            _index_row(
                document=document,
                embedding_id=_term_embedding_id(
                    row_id,
                    "keyword",
                    ordinal,
                    term_embedding.term,
                ),
                embedding_type="keyword",
                embedding_scope="content_unit",
                text=term_embedding.term,
                vector=term_embedding.vector,
                config=config,
                processed_at=processed_at,
                unit=unit,
            )
        )

    for ordinal, term_embedding in enumerate(metric_terms, start=1):
        rows.append(
            _index_row(
                document=document,
                embedding_id=_term_embedding_id(
                    row_id,
                    "metric",
                    ordinal,
                    term_embedding.term,
                ),
                embedding_type="metric",
                embedding_scope="content_unit",
                text=term_embedding.term,
                vector=term_embedding.vector,
                config=config,
                processed_at=processed_at,
                unit=unit,
            )
        )
    return rows


def _index_row(
    document: EnrichmentDocument,
    embedding_id: str,
    embedding_type: str,
    embedding_scope: str,
    text: str,
    vector: list[float],
    config: EmbeddingConfig,
    processed_at: str,
    unit: EnrichedContentUnit | None = None,
    section_id: str = "",
    content_unit_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Return one normalized long-form embedding index row."""
    unit_ids = (
        list(content_unit_ids)
        if content_unit_ids is not None
        else ([unit.content_unit_id] if unit is not None else [])
    )
    return {
        "embedding_id": embedding_id,
        "embedding_type": embedding_type,
        "embedding_scope": embedding_scope,
        **_source_fields(document.source),
        "content_unit_id": unit.content_unit_id if unit is not None else "",
        "content_unit_ids": unit_ids,
        "chunk_id": unit.chunk_id if unit is not None else "",
        "section_id": section_id or (unit.section_id if unit is not None else ""),
        "text": text,
        "text_hash": _text_hash(text),
        "embedding_json": vector,
        "embedding_model": config.model,
        "embedding_dimensions": config.dimensions,
        "created_at": processed_at,
    }


def _build_content_text(
    unit: EnrichedContentUnit,
    section: EnrichmentSection | None,
) -> str:
    """Assemble content-unit text for the content embedding."""
    text = unit.embedding_content or unit.raw_content
    if unit.chunk_header:
        text = "\n".join(
            part
            for part in (
                unit.chunk_header,
                unit.sheet_passthrough_content,
                unit.section_passthrough_content,
                text,
            )
            if part
        )
    if section is not None and section.title:
        text = f"Section: {section.title}. {text}"
    return text


def _build_section_summary_texts(
    sections: list[EnrichmentSection],
) -> dict[str, str]:
    """Build embeddable section-summary text keyed by section id."""
    return {
        section.section_id: _build_section_summary_text(section)
        for section in sections
        if _is_embeddable_section(section)
    }


def _build_section_summary_text(section: EnrichmentSection) -> str:
    """Build one section-summary embedding payload."""
    return f"Section: {section.title}. {section.summary}".strip()


def _is_embeddable_section(section: EnrichmentSection) -> bool:
    """Return whether a section should receive a summary embedding."""
    if section.level == "section":
        return True
    return section.level == "subsection" and bool(section.summary.strip())


def _source_fields(source: dict[str, Any]) -> dict[str, Any]:
    """Return final-row source metadata fields."""
    return {
        "source_type": source.get("source_type", ""),
        "period": source.get("period", ""),
        "ticker": source.get("ticker", ""),
        "filename": source.get("filename", ""),
        "file_path": source.get("file_path", ""),
        "filetype": source.get("filetype", ""),
        "file_hash": source.get("file_hash", ""),
        "file_id": source.get("file_id", ""),
        "data_source": source.get("data_source", ""),
        "fiscal_year": source.get("fiscal_year", ""),
        "quarter": source.get("quarter", ""),
        "bank": source.get("bank", ""),
    }


def _content_unit_fields(unit: EnrichedContentUnit) -> dict[str, Any]:
    """Return final-row fields sourced from one content unit."""
    unit_type = _unit_type(unit)
    return {
        "content_unit_id": unit.content_unit_id,
        "chunk_id": unit.chunk_id,
        "parent_content_unit_id": "",
        "unit_type": unit_type,
        "page_number": unit.item_number,
        "sheet_name": unit.item_title if unit.filetype == "xlsx" else "",
        "parent_page_number": unit.item_number if unit_type.endswith("_chunk") else "",
        "raw_content": unit.raw_content,
        "embedding_content": unit.embedding_content,
        "chunk_context": unit.chunk_context,
        "chunk_header": unit.chunk_header,
        "sheet_passthrough_content": unit.sheet_passthrough_content,
        "section_passthrough_content": unit.section_passthrough_content,
        "keywords_json": list(unit.keywords),
        "metrics_json": list(unit.metrics),
        "token_count": unit.embedding_token_count or unit.raw_token_count,
    }


def _section_fields(section: EnrichmentSection | None) -> dict[str, Any]:
    """Return final-row section metadata fields."""
    if section is None:
        return {
            "section_id": "",
            "parent_section_id": "",
            "section_level": "",
            "section_title": "",
            "section_summary": "",
        }
    return {
        "section_id": section.section_id,
        "parent_section_id": section.parent_section_id,
        "section_level": section.level,
        "section_title": section.title,
        "section_summary": section.summary,
    }


def _document_fields(document: EnrichmentDocument) -> dict[str, str]:
    """Return final-row document metadata fields."""
    metadata = document.document_metadata
    return {
        "document_title": str(metadata.get("title") or document.filename),
        "document_publisher": _text_value(
            metadata.get("publisher") or metadata.get("authors") or ""
        ),
        "publication_date": _text_value(metadata.get("publication_date", "")),
        "document_summary": str(metadata.get("executive_summary", "")),
    }


def _unit_type(unit: EnrichedContentUnit) -> str:
    """Return retriever unit type for an enriched content unit."""
    if unit.filetype == "xlsx":
        return "xlsx_sheet" if _is_full_unit(unit) else "xlsx_chunk"
    if unit.filetype == "pdf":
        return "pdf_page" if _is_full_unit(unit) else "pdf_chunk"
    return unit.item_type or unit.filetype


def _is_full_unit(unit: EnrichedContentUnit) -> bool:
    """Return whether a unit represents a full page/sheet."""
    expected_context = f"Full {unit.item_type}".strip().lower()
    has_full_context = unit.chunk_context.strip().lower() == expected_context
    return has_full_context and not unit.chunk_header


def _find_section(
    unit: EnrichedContentUnit,
    sections: list[EnrichmentSection],
) -> EnrichmentSection | None:
    """Find the section assigned to a content unit."""
    for section in sections:
        if section.section_id == unit.section_id:
            return section
    return None


def _unique_terms(terms: list[str]) -> list[str]:
    """Return stripped, de-duplicated terms while preserving order."""
    seen = set()
    unique_terms = []
    for term in terms:
        cleaned = " ".join(str(term).split())
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        unique_terms.append(cleaned)
    return unique_terms


def _join_terms(terms: list[str]) -> str:
    """Join keyword or metric terms for a row-level facet embedding."""
    return "\n".join(term for term in terms if term)


def _load_embedding_document(
    progress_dir: Path,
    record: ManifestRecord,
    max_content_units: int | None,
) -> tuple[EnrichmentDocument, Path]:
    """Load one enriched document and its artifact path for embedding."""
    enrichment_root = _artifact_root(progress_dir, record.file_id) / ENRICHMENT_DIR_NAME
    document_path = enrichment_root / DOCUMENT_ARTIFACT_FILE_NAME
    payload = _read_required_json(document_path)
    source = dict(payload.get("source") or _source_from_record(record))

    unit_records = payload.get("content_units")
    if not isinstance(unit_records, list):
        raise EmbeddingStageError(f"{document_path} is missing content_units list")
    if max_content_units is not None:
        unit_records = unit_records[:max_content_units]

    section_records = payload.get("sections")
    if not isinstance(section_records, list):
        raise EmbeddingStageError(f"{document_path} is missing sections list")

    document = EnrichmentDocument(
        source=source,
        source_stage_run_id=str(payload.get("source_stage_run_id", "")),
        source_artifact_root=Path(str(payload.get("source_artifact_root", ""))),
        chunking_artifact_root=Path(str(payload.get("chunking_artifact_root", ""))),
        chunks_jsonl_path=Path(str(payload.get("chunks_jsonl_path", ""))),
        content_units=[
            _content_unit_from_record(unit_record, source)
            for unit_record in unit_records
        ],
        document_metadata=dict(payload.get("document_metadata") or {}),
        sections=[
            _section_from_record(section_record)
            for section_record in section_records
        ],
    )
    return document, document_path


def _source_from_record(record: ManifestRecord) -> dict[str, Any]:
    """Build source metadata from a manifest record."""
    return {
        "source_type": record.data_source,
        "period": f"{record.fiscal_year}_{record.quarter}",
        "ticker": record.bank,
        "filename": record.file_name,
        "file_path": record.file_path,
        "filetype": record.file_type,
        "file_hash": record.file_hash,
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


def _content_unit_from_record(
    record: dict[str, Any],
    source: dict[str, Any],
) -> EnrichedContentUnit:
    """Convert one enriched content-unit record to a dataclass."""
    return EnrichedContentUnit(
        source=source,
        source_run_id=str(record.get("source_run_id", "")),
        source_artifact_root=Path(str(record.get("source_artifact_root", ""))),
        filetype=str(record.get("filetype", source.get("filetype", ""))),
        item_type=str(record.get("item_type", "")),
        item_number=int(record.get("item_number", 0)),
        item_title=str(record.get("item_title", "")),
        content_unit_id=str(record.get("content_unit_id", "")),
        chunk_id=str(record.get("chunk_id", "")),
        chunk_index=int(record.get("chunk_index", 0)),
        raw_content=str(record.get("raw_content", "")),
        embedding_content=str(record.get("embedding_content", "")),
        chunk_context=str(record.get("chunk_context", "")),
        chunk_header=str(record.get("chunk_header", "")),
        sheet_passthrough_content=str(record.get("sheet_passthrough_content", "")),
        section_passthrough_content=str(record.get("section_passthrough_content", "")),
        raw_token_count=int(record.get("raw_token_count", 0)),
        embedding_token_count=int(record.get("embedding_token_count", 0)),
        token_tier=str(record.get("token_tier", "")),
        section_id=str(record.get("section_id", "")),
        keywords=list(record.get("keywords") or []),
        metrics=list(record.get("metrics") or []),
    )


def _section_from_record(record: dict[str, Any]) -> EnrichmentSection:
    """Convert one enriched section record to a dataclass."""
    return EnrichmentSection(
        section_id=str(record.get("section_id", "")),
        parent_section_id=str(record.get("parent_section_id", "")),
        level=str(record.get("level", "")),
        title=str(record.get("title", "")),
        sequence=int(record.get("sequence", 0)),
        page_start=int(record.get("page_start", 0)),
        page_end=int(record.get("page_end", 0)),
        chunk_ids=list(record.get("chunk_ids") or []),
        token_count=int(record.get("token_count", 0)),
        summary=str(record.get("summary", "")),
        keywords=list(record.get("keywords") or []),
        metrics=list(record.get("metrics") or []),
    )


def _embedding_counts(
    content_rows: list[dict[str, Any]],
    index_rows: list[dict[str, Any]],
) -> dict[str, int]:
    """Return embedding counts by family."""
    return {
        "content_rows": len(content_rows),
        "content_embeddings": _non_empty_count(
            content_rows,
            "content_embedding_json",
        ),
        "keyword_group_embeddings": _non_empty_count(
            content_rows,
            "keyword_embedding_json",
        ),
        "metric_group_embeddings": _non_empty_count(
            content_rows,
            "metric_embedding_json",
        ),
        "section_summary_embeddings": _non_empty_count(
            content_rows,
            "section_summary_embedding_json",
        ),
        "document_summary_embeddings": _non_empty_count(
            content_rows,
            "document_summary_embedding_json",
        ),
        "embedding_index_rows": len(index_rows),
        "content_index_rows": _index_type_count(index_rows, "content"),
        "keyword_index_rows": _index_type_count(index_rows, "keyword"),
        "metric_index_rows": _index_type_count(index_rows, "metric"),
        "section_summary_index_rows": _index_type_count(
            index_rows,
            "section_summary",
        ),
        "document_summary_index_rows": _index_type_count(
            index_rows,
            "document_summary",
        ),
    }


def _non_empty_text_count(texts: list[str]) -> int:
    """Count non-blank embedding input texts."""
    return sum(1 for text in texts if text and text.strip())


def _estimated_batch_count(text_count: int, batch_size: int) -> int:
    """Return the number of embedding API batches for a text count."""
    if text_count <= 0:
        return 0
    return (text_count + batch_size - 1) // batch_size


def _non_empty_count(records: list[dict[str, Any]], field: str) -> int:
    """Count records with a non-empty list field."""
    return sum(1 for record in records if record.get(field))


def _index_type_count(records: list[dict[str, Any]], embedding_type: str) -> int:
    """Count embedding index rows of one type."""
    return sum(
        1
        for record in records
        if record.get("embedding_type") == embedding_type
    )


def _load_process_records(progress_dir: Path) -> tuple[ManifestRecord, ...]:
    """Load manifest records selected for processing."""
    path = progress_dir / FILES_TO_PROCESS_FILE_NAME
    payload = _read_required_json(path)
    rows = payload.get("files_to_process")
    if not isinstance(rows, list):
        raise EmbeddingStageError(f"{path} is missing files_to_process list")
    return tuple(_record_from_mapping(row, path) for row in rows)


def _record_from_mapping(row: Any, path: Path) -> ManifestRecord:
    """Build a ManifestRecord from one progress JSON row."""
    if not isinstance(row, dict):
        raise EmbeddingStageError(f"Invalid manifest row in {path}: {row!r}")
    missing = [field for field in MANIFEST_FIELDS if field not in row]
    if missing:
        raise EmbeddingStageError(f"Manifest row missing fields {missing}: {path}")
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
    """Return the artifact root for one source file."""
    return progress_dir / ARTIFACTS_DIR_NAME / file_id


def _row_id(source: dict[str, Any], unit: EnrichedContentUnit) -> str:
    """Build a stable row id for a content unit."""
    return f"{_source_key(source)}::{unit.content_unit_id}"


def _source_key(source: dict[str, Any]) -> str:
    """Build a stable source key for embedding ids."""
    return str(source.get("file_hash") or source.get("filename") or "source")


def _section_embedding_id(
    source: dict[str, Any],
    section: EnrichmentSection,
) -> str:
    """Build a stable section-summary embedding id."""
    return f"{_source_key(source)}::section::{section.section_id}::summary"


def _term_embedding_id(
    row_id: str,
    embedding_type: str,
    ordinal: int,
    term: str,
) -> str:
    """Build a stable individual term embedding id."""
    return f"{row_id}::{embedding_type}::{ordinal:03d}::{_text_hash(term)[:12]}"


def _text_hash(text: str) -> str:
    """Return a SHA-256 hash for embedded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text_value(value: Any) -> str:
    """Render scalar/list metadata as text."""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if str(item).strip())
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in value.items())
    return str(value or "")


def _read_required_json(path: Path) -> dict[str, Any]:
    """Read a required JSON object artifact."""
    if not path.is_file():
        raise EmbeddingStageError(f"Required artifact missing: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise EmbeddingStageError(f"Artifact must be a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    """Write deterministic UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write deterministic UTF-8 JSONL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row) for row in rows]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def _default_client_factory() -> LLMClient:
    """Return the configured project LLM client."""
    return LLMClient(verify_ssl=get_ssl_verify())


def _utc_now() -> str:
    """Return the current UTC time as an ISO string."""
    return datetime.now(tz=UTC).isoformat()
