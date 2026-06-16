"""Create token-counted chunks from extracted transcript artifacts.

This stage runs after XML extraction. Transcript extraction emits page-shaped
records for downstream compatibility, where each "page" is one transcript unit:
a management speaker block or a grouped Q&A exchange.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.config_setup import REPO_ROOT, ensure_tokenizer_cache_env, load_config
from utils.logging_setup import get_stage_logger
from .extraction import EXTRACTION_DIR_NAME, WORKBOOK_ARTIFACT_FILE_NAME
from .manifest import (
    ARTIFACTS_DIR_NAME,
    FILES_TO_PROCESS_FILE_NAME,
    PROGRESS_DIR,
    ManifestRecord,
)

CHUNKING_DIR_NAME = "chunking"
CHUNKING_ARTIFACT_FILE_NAME = "chunking.json"
CHUNKING_MANIFEST_FILE_NAME = "chunking_manifest.json"
CHUNKS_JSONL_FILE_NAME = "chunks.jsonl"
DEFAULT_TOKENIZER_MODEL = "o200k_base"
DEFAULT_EMBEDDING_TOKEN_LIMIT = 8_000
DEFAULT_TRUNCATION_TOKEN_LIMIT: int | None = None
TOKEN_TIER_LOW_MAX = 5_000
TOKEN_TIER_MEDIUM_MAX = 10_000
EMBEDDING_FORMAT_VERSION = "row_column_values_v1"
PAGE_EMBEDDING_FORMAT_VERSION = "transcript_unit_markdown_v1"
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


class ChunkingStageError(RuntimeError):
    """Raised when chunking cannot read required extraction artifacts."""


@dataclass(frozen=True)
class TokenizerContext:
    """Tokenizer encoder and metadata used for a chunking run."""

    model: str
    encoding_name: str
    version: str
    cache_dir: Path
    encoder: Any

    def to_record(self) -> dict[str, Any]:
        """Return tokenizer metadata without the encoder object."""
        return {
            "model": self.model,
            "encoding_name": self.encoding_name,
            "tiktoken_version": self.version,
            "cache_dir": str(self.cache_dir),
            "cache_files": _tokenizer_cache_files(self.cache_dir),
        }


@dataclass(frozen=True)
class ContentChunk:
    """One extracted transcript unit prepared for embedding and retrieval."""

    source: dict[str, Any]
    source_workbook_artifact_path: str
    source_sheet_artifact_path: str
    file_id: str
    sheet_number: int
    sheet_title: str
    chunk_id: str
    chunk_index: int
    chunk_context: str
    content: str
    embedding_content: str
    embedding_format: str
    content_token_count: int
    embedding_source_token_count: int
    embedding_token_count: int
    embedding_token_limit: int
    embedding_content_truncated: bool
    embedding_tokens_omitted: int
    token_tier: str
    was_chunked: bool
    was_truncated: bool
    chunking_strategy: str
    artifact_path: str = ""
    item_type: str = "sheet"
    markdown_path: str = ""

    def to_record(self) -> dict[str, Any]:
        """Return the chunk as a JSON-serializable record."""
        is_page = self.item_type == "page"
        return {
            "stage": "chunk",
            "source": self.source,
            "source_run_id": "",
            "source_workbook_artifact_path": self.source_workbook_artifact_path,
            "source_document_artifact_path": self.source_workbook_artifact_path,
            "source_sheet_artifact_path": self.source_sheet_artifact_path,
            "source_page_artifact_path": self.source_sheet_artifact_path
            if is_page
            else "",
            "source_artifact_root": str(
                Path(self.source_workbook_artifact_path).parent
            ),
            "filetype": str(
                self.source.get("filetype") or self.source.get("file_type") or "xlsx"
            ),
            "item_type": self.item_type,
            "item_number": self.sheet_number,
            "item_title": self.sheet_title,
            "file_id": self.file_id,
            "sheet_number": self.sheet_number,
            "sheet_title": self.sheet_title,
            "page_number": self.sheet_number if is_page else 0,
            "page_title": self.sheet_title if is_page else "",
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "chunk_context": self.chunk_context,
            "chunk_header": "",
            "sheet_passthrough_content": "",
            "section_passthrough_content": "",
            "content": self.content,
            "embedding_content": self.embedding_content,
            "embedding_format": self.embedding_format,
            "raw_token_count": self.content_token_count,
            "content_token_count": self.content_token_count,
            "embedding_source_token_count": self.embedding_source_token_count,
            "embedding_token_count": self.embedding_token_count,
            "embedding_token_limit": self.embedding_token_limit,
            "embedding_content_truncated": self.embedding_content_truncated,
            "embedding_tokens_omitted": self.embedding_tokens_omitted,
            "token_tier": self.token_tier,
            "was_chunked": self.was_chunked,
            "was_truncated": self.was_truncated,
            "chunking_strategy": self.chunking_strategy,
            "markdown_path": self.markdown_path,
            "artifact_path": self.artifact_path,
        }


@dataclass(frozen=True)
class ChunkingStageResult:
    """Summary returned after writing chunking artifacts."""

    processed_file_count: int
    chunk_count: int
    chunking_artifact_paths: tuple[Path, ...]


def run_chunking_stage(
    progress_dir: Path = PROGRESS_DIR,
    tokenizer_model: str = DEFAULT_TOKENIZER_MODEL,
    embedding_token_limit: int = DEFAULT_EMBEDDING_TOKEN_LIMIT,
    truncation_token_limit: int | None = DEFAULT_TRUNCATION_TOKEN_LIMIT,
    max_files: int | None = None,
    max_sheets_per_file: int | None = None,
) -> ChunkingStageResult:
    """Create one chunk artifact per extracted transcript unit.

    Args:
        progress_dir: Folder containing manifest progress and artifacts.
        tokenizer_model: Model or encoding name used to select the tokenizer.
        embedding_token_limit: Maximum tokens allowed in final embedding content.
        truncation_token_limit: Optional safety cap for full markdown copied into
            content. By default, page/sheet content is not truncated; only
            embedding content is token-budgeted.
        max_files: Optional file limit for deterministic smoke checks.
        max_sheets_per_file: Optional per-file transcript-unit limit for smoke checks.

    Returns:
        ChunkingStageResult with artifact paths and total chunk count.

    Raises:
        ChunkingStageError: If extraction artifacts are missing or malformed.

    External side effects:
        Writes artifacts under ``progress_dir/artifacts/<file_id>/chunking``.
    """
    logger = get_stage_logger(__name__, "CHUNKING")
    load_config()
    tokenizer = get_tokenizer_context(tokenizer_model)
    records = list(_load_process_records(progress_dir))
    if max_files is not None:
        records = records[:max_files]
    processed_at = _utc_now()
    artifact_paths: list[Path] = []
    chunk_count = 0

    for index, record in enumerate(records, start=1):
        logger.info(
            "Chunking document %d/%d: %s",
            index,
            len(records),
            record.file_name,
        )
        started_at = _utc_now()
        start = time.perf_counter()
        extraction_path = (
            _artifact_root(progress_dir, record.file_id)
            / EXTRACTION_DIR_NAME
            / WORKBOOK_ARTIFACT_FILE_NAME
        )
        extraction = _read_required_json(extraction_path)
        chunks = _chunk_extracted_document(
            record=record,
            extraction=extraction,
            extraction_path=extraction_path,
            tokenizer=tokenizer,
            embedding_token_limit=embedding_token_limit,
            truncation_token_limit=truncation_token_limit,
            max_sheets=max_sheets_per_file,
        )
        duration_seconds = time.perf_counter() - start
        completed_at = _utc_now()
        artifact_path = write_chunking_artifact(
            record=record,
            extraction=extraction,
            extraction_path=extraction_path,
            chunks=chunks,
            chunking_root=_artifact_root(progress_dir, record.file_id)
            / CHUNKING_DIR_NAME,
            tokenizer=tokenizer,
            processed_at=processed_at,
            file_started_at=started_at,
            file_completed_at=completed_at,
            duration_seconds=duration_seconds,
            embedding_token_limit=embedding_token_limit,
            truncation_token_limit=truncation_token_limit,
        )
        artifact_paths.append(artifact_path)
        chunk_count += len(chunks)

    logger.info(
        "Chunking complete: files=%d chunks=%d",
        len(records),
        chunk_count,
    )
    return ChunkingStageResult(
        processed_file_count=len(records),
        chunk_count=chunk_count,
        chunking_artifact_paths=tuple(artifact_paths),
    )


def get_tokenizer_context(model: str = DEFAULT_TOKENIZER_MODEL) -> TokenizerContext:
    """Return a tiktoken encoder configured to use the project cache."""
    cache_dir = ensure_tokenizer_cache_env()
    _validate_tokenizer_cache(cache_dir)
    try:
        import tiktoken
    except ModuleNotFoundError as exc:
        raise ChunkingStageError(
            "Chunking requires tiktoken. Install project requirements before "
            "running chunking."
        ) from exc

    try:
        encoding_name = tiktoken.encoding_name_for_model(model)
    except KeyError:
        encoding_name = DEFAULT_TOKENIZER_MODEL
    return TokenizerContext(
        model=model,
        encoding_name=encoding_name,
        version=str(getattr(tiktoken, "__version__", "")),
        cache_dir=cache_dir,
        encoder=tiktoken.get_encoding(encoding_name),
    )


def count_tokens(text: str, tokenizer: TokenizerContext) -> int:
    """Count tokens in text using the configured tokenizer."""
    return len(tokenizer.encoder.encode(text))


def truncate_content(
    content: str,
    limit: int,
    tokenizer: TokenizerContext,
) -> str:
    """Truncate text to a token limit using tiktoken token boundaries."""
    tokens = tokenizer.encoder.encode(content)
    if len(tokens) <= limit:
        return content
    return tokenizer.encoder.decode(tokens[:limit])


def build_embedding_content(sheet_payload: dict[str, Any]) -> str:
    """Build compact row/cell text for sheet embedding.

    The format intentionally excludes style, color, borders, number formats,
    formulas, merged-range coordinates, hidden-row metadata, and raw JSON noise.
    It keeps the sheet title, used range, row numbers, column letters, and
    displayed cell values so semantic search sees the financial facts and their
    approximate worksheet structure.
    """
    title = str(sheet_payload.get("title", "")).strip()
    metadata = sheet_payload.get("metadata") or {}
    used_range = str(metadata.get("used_range", "")).strip()
    lines = [f"Sheet: {title}"]
    if used_range:
        lines.append(f"Range: {used_range}")
    lines.append("Rows:")

    cell_grid = sheet_payload.get("cell_grid")
    rows = cell_grid.get("rows", []) if isinstance(cell_grid, dict) else []
    if not rows:
        markdown = str(sheet_payload.get("markdown", "")).strip()
        if markdown:
            lines.append(markdown)
        else:
            lines.append("(empty)")
        return "\n".join(lines)

    for row in rows:
        row_number = int(row.get("row_number", 0) or 0)
        cells = row.get("cells", [])
        rendered_cells = []
        for cell in cells if isinstance(cells, list) else []:
            value = _normalize_embedding_value(cell.get("value", ""))
            if not value:
                continue
            label = _embedding_cell_label(cell)
            rendered_cells.append(f"{label}={value}" if label else value)
        if rendered_cells:
            lines.append(f"R{row_number}: " + " | ".join(rendered_cells))
    return "\n".join(lines)


def build_page_embedding_content(page_payload: dict[str, Any]) -> str:
    """Build compact embedding text for one extracted transcript unit."""
    page_number = int(page_payload.get("page_number", 0) or 0)
    markdown = str(page_payload.get("markdown", "")).strip()
    title = _markdown_title(markdown) or f"Transcript unit {page_number}"
    lines = [f"Transcript unit: {page_number}", f"Title: {title}", "Markdown:"]
    lines.append(markdown if markdown else "(empty)")
    return "\n".join(lines)


def classify_token_tier(token_count: int) -> str:
    """Classify token count into low, medium, or high."""
    if token_count <= TOKEN_TIER_LOW_MAX:
        return "low"
    if token_count <= TOKEN_TIER_MEDIUM_MAX:
        return "medium"
    return "high"


def write_chunking_artifact(
    record: ManifestRecord,
    extraction: dict[str, Any],
    extraction_path: Path,
    chunks: list[ContentChunk],
    chunking_root: Path,
    tokenizer: TokenizerContext,
    processed_at: str,
    file_started_at: str,
    file_completed_at: str,
    duration_seconds: float,
    embedding_token_limit: int,
    truncation_token_limit: int | None,
) -> Path:
    """Write chunking document summary, per-chunk JSON, and JSONL artifacts."""
    chunking_root.mkdir(parents=True, exist_ok=True)
    chunks_dir = chunking_root / "chunks"
    chunk_records = []
    for chunk in chunks:
        artifact_path = chunks_dir / _chunk_file_name(chunk)
        chunk_with_path = _with_artifact_path(chunk, artifact_path)
        record_payload = chunk_with_path.to_record()
        _write_json(artifact_path, record_payload)
        chunk_records.append(record_payload)

    chunks_jsonl_path = chunking_root / CHUNKS_JSONL_FILE_NAME
    _write_jsonl(chunks_jsonl_path, chunk_records)
    qa_findings = _chunking_qa_findings(chunk_records)
    item_records = _item_records(chunk_records, extraction)
    sheet_records = [
        item for item in item_records if item.get("item_type", "sheet") == "sheet"
    ]
    page_records = [item for item in item_records if item.get("item_type") == "page"]
    embedding_format = _artifact_embedding_format(chunk_records)
    artifact = {
        "stage": "chunking",
        "processed_at": processed_at,
        "file_started_at": file_started_at,
        "file_completed_at": file_completed_at,
        "duration_seconds": round(duration_seconds, 6),
        "source": extraction.get("source", _source_record(record)),
        "source_stage": "extraction",
        "source_workbook_artifact_path": str(extraction_path),
        "source_document_artifact_path": str(extraction_path),
        "tokenizer": tokenizer.to_record(),
        "limits": {
            "embedding_token_limit": embedding_token_limit,
            "truncation_token_limit": truncation_token_limit,
        },
        "embedding_format": embedding_format,
        "item_count": len(item_records),
        "page_count": len(page_records),
        "sheet_count": len(sheet_records),
        "chunk_count": len(chunk_records),
        "chunked_item_count": 0,
        "single_item_count": len(item_records),
        "chunked_sheet_count": 0,
        "single_sheet_count": len(sheet_records),
        "embedding_truncated_item_count": sum(
            1 for item in item_records if item["embedding_content_truncated"]
        ),
        "content_truncated_item_count": sum(
            1 for item in item_records if item["was_truncated"]
        ),
        "embedding_truncated_sheet_count": sum(
            1 for sheet in sheet_records if sheet["embedding_content_truncated"]
        ),
        "content_truncated_sheet_count": sum(
            1 for sheet in sheet_records if sheet["was_truncated"]
        ),
        "token_summary": _token_summary(chunk_records),
        "qa_status": _qa_status(qa_findings),
        "qa_counts": _qa_counts(qa_findings),
        "qa_findings": qa_findings,
        "chunks_jsonl_path": str(chunks_jsonl_path),
        "items": item_records,
        "pages": page_records,
        "sheets": sheet_records,
        "chunks": _chunk_index_records(chunk_records),
    }
    artifact_path = chunking_root / CHUNKING_ARTIFACT_FILE_NAME
    _write_json(artifact_path, artifact)
    _write_json(
        chunking_root / CHUNKING_MANIFEST_FILE_NAME,
        {
            "stage": artifact["stage"],
            "processed_at": artifact["processed_at"],
            "source": artifact["source"],
            "chunking_artifact_path": str(artifact_path),
            "chunks_jsonl_path": str(chunks_jsonl_path),
            "duration_seconds": artifact["duration_seconds"],
            "item_count": artifact["item_count"],
            "page_count": artifact["page_count"],
            "sheet_count": artifact["sheet_count"],
            "chunk_count": artifact["chunk_count"],
            "embedding_format": artifact["embedding_format"],
            "token_summary": artifact["token_summary"],
            "qa_status": artifact["qa_status"],
            "qa_counts": artifact["qa_counts"],
        },
    )
    return artifact_path


def _chunk_extracted_document(
    record: ManifestRecord,
    extraction: dict[str, Any],
    extraction_path: Path,
    tokenizer: TokenizerContext,
    embedding_token_limit: int,
    truncation_token_limit: int | None,
    max_sheets: int | None,
) -> list[ContentChunk]:
    """Create one chunk per transcript unit from one extraction artifact."""
    sheets = extraction.get("sheets")
    if isinstance(sheets, list):
        if max_sheets is not None:
            sheets = sheets[:max_sheets]
        return [
            _chunk_sheet(
                record=record,
                source=extraction.get("source", _source_record(record)),
                extraction_path=extraction_path,
                sheet_payload=_read_sheet_payload(sheet),
                tokenizer=tokenizer,
                embedding_token_limit=embedding_token_limit,
                truncation_token_limit=truncation_token_limit,
            )
            for sheet in sheets
        ]

    pages = extraction.get("pages")
    if isinstance(pages, list):
        if max_sheets is not None:
            pages = pages[:max_sheets]
        return [
            _chunk_page(
                record=record,
                source=extraction.get("source", _source_record(record)),
                extraction_path=extraction_path,
                page_payload=_read_page_payload(page),
                tokenizer=tokenizer,
                embedding_token_limit=embedding_token_limit,
                truncation_token_limit=truncation_token_limit,
            )
            for page in pages
        ]

    raise ChunkingStageError(f"{extraction_path} is missing pages or sheets list")


def _chunk_sheet(
    record: ManifestRecord,
    source: dict[str, Any],
    extraction_path: Path,
    sheet_payload: dict[str, Any],
    tokenizer: TokenizerContext,
    embedding_token_limit: int,
    truncation_token_limit: int | None,
) -> ContentChunk:
    """Create the one-chunk representation of a sheet."""
    content = str(sheet_payload.get("markdown", ""))
    source_content_token_count = count_tokens(content, tokenizer)
    was_truncated = (
        truncation_token_limit is not None
        and source_content_token_count > truncation_token_limit
    )
    if was_truncated:
        content = truncate_content(content, truncation_token_limit, tokenizer)
    content_token_count = count_tokens(content, tokenizer)

    embedding_source_content = build_embedding_content(sheet_payload)
    embedding_source_token_count = count_tokens(embedding_source_content, tokenizer)
    embedding_content_truncated = embedding_source_token_count > embedding_token_limit
    embedding_content = (
        truncate_content(embedding_source_content, embedding_token_limit, tokenizer)
        if embedding_content_truncated
        else embedding_source_content
    )
    embedding_token_count = count_tokens(embedding_content, tokenizer)
    sheet_number = int(sheet_payload["sheet_number"])
    sheet_title = str(sheet_payload.get("title", ""))
    return ContentChunk(
        source=source,
        source_workbook_artifact_path=str(extraction_path),
        source_sheet_artifact_path=_sheet_artifact_path(sheet_payload),
        file_id=record.file_id,
        sheet_number=sheet_number,
        sheet_title=sheet_title,
        chunk_id=f"sheet_{sheet_number}.1",
        chunk_index=1,
        chunk_context="Full sheet",
        content=content,
        embedding_content=embedding_content,
        embedding_format=EMBEDDING_FORMAT_VERSION,
        content_token_count=content_token_count,
        embedding_source_token_count=embedding_source_token_count,
        embedding_token_count=embedding_token_count,
        embedding_token_limit=embedding_token_limit,
        embedding_content_truncated=embedding_content_truncated,
        embedding_tokens_omitted=max(
            0,
            embedding_source_token_count - embedding_token_count,
        ),
        token_tier=classify_token_tier(embedding_token_count),
        was_chunked=False,
        was_truncated=was_truncated,
        chunking_strategy="single_sheet_compact_embedding",
        item_type="sheet",
    )


def _chunk_page(
    record: ManifestRecord,
    source: dict[str, Any],
    extraction_path: Path,
    page_payload: dict[str, Any],
    tokenizer: TokenizerContext,
    embedding_token_limit: int,
    truncation_token_limit: int | None,
) -> ContentChunk:
    """Create the one-chunk representation of a transcript unit."""
    content = str(page_payload.get("markdown", ""))
    source_content_token_count = count_tokens(content, tokenizer)
    was_truncated = (
        truncation_token_limit is not None
        and source_content_token_count > truncation_token_limit
    )
    if was_truncated:
        content = truncate_content(content, truncation_token_limit, tokenizer)
    content_token_count = count_tokens(content, tokenizer)

    embedding_source_content = build_page_embedding_content(
        {**page_payload, "markdown": content}
    )
    embedding_source_token_count = count_tokens(embedding_source_content, tokenizer)
    embedding_content_truncated = embedding_source_token_count > embedding_token_limit
    embedding_content = (
        truncate_content(embedding_source_content, embedding_token_limit, tokenizer)
        if embedding_content_truncated
        else embedding_source_content
    )
    embedding_token_count = count_tokens(embedding_content, tokenizer)
    page_number = int(page_payload["page_number"])
    page_title = _markdown_title(content) or f"Transcript unit {page_number}"
    return ContentChunk(
        source=source,
        source_workbook_artifact_path=str(extraction_path),
        source_sheet_artifact_path=_page_artifact_path(page_payload),
        file_id=record.file_id,
        sheet_number=page_number,
        sheet_title=page_title,
        chunk_id=f"page_{page_number}.1",
        chunk_index=1,
        chunk_context=f"Transcript unit {page_number}",
        content=content,
        embedding_content=embedding_content,
        embedding_format=PAGE_EMBEDDING_FORMAT_VERSION,
        content_token_count=content_token_count,
        embedding_source_token_count=embedding_source_token_count,
        embedding_token_count=embedding_token_count,
        embedding_token_limit=embedding_token_limit,
        embedding_content_truncated=embedding_content_truncated,
        embedding_tokens_omitted=max(
            0,
            embedding_source_token_count - embedding_token_count,
        ),
        token_tier=classify_token_tier(embedding_token_count),
        was_chunked=False,
        was_truncated=was_truncated,
        chunking_strategy="single_transcript_unit_markdown_embedding",
        item_type="page",
        markdown_path=_page_markdown_path(page_payload),
    )


def _read_sheet_payload(sheet_index_record: Any) -> dict[str, Any]:
    """Read one extraction sheet JSON payload from a workbook sheet record."""
    if not isinstance(sheet_index_record, dict):
        raise ChunkingStageError("Extraction sheet index record is not an object")
    path = _resolve_artifact_path(str(sheet_index_record.get("sheet_json_path", "")))
    payload = _read_required_json(path)
    if not isinstance(payload, dict):
        raise ChunkingStageError(f"Sheet artifact is not an object: {path}")
    payload["source_sheet_artifact_path"] = str(path)
    return payload


def _read_page_payload(page_index_record: Any) -> dict[str, Any]:
    """Read one extraction transcript-unit JSON payload."""
    if not isinstance(page_index_record, dict):
        raise ChunkingStageError("Extraction page index record is not an object")
    path = _resolve_artifact_path(str(page_index_record.get("page_json_path", "")))
    payload = _read_required_json(path)
    if not isinstance(payload, dict):
        raise ChunkingStageError(f"Page artifact is not an object: {path}")
    payload["source_page_artifact_path"] = str(path)
    markdown_path = str(page_index_record.get("markdown_path", ""))
    if markdown_path:
        payload["source_page_markdown_path"] = str(
            _resolve_artifact_path(markdown_path)
        )
    return payload


def _resolve_artifact_path(path_value: str) -> Path:
    """Resolve a stored artifact path from extraction JSON."""
    path = Path(path_value)
    if path.is_absolute():
        return path
    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path
    return REPO_ROOT / path


def _sheet_artifact_path(sheet_payload: dict[str, Any]) -> str:
    """Return the source sheet artifact path recorded in the sheet payload."""
    source_path = sheet_payload.get("source_sheet_artifact_path")
    if source_path:
        return str(source_path)
    return ""


def _page_artifact_path(page_payload: dict[str, Any]) -> str:
    """Return the source transcript-unit artifact path recorded in the payload."""
    source_path = page_payload.get("source_page_artifact_path")
    if source_path:
        return str(source_path)
    return ""


def _page_markdown_path(page_payload: dict[str, Any]) -> str:
    """Return the source transcript-unit markdown path recorded in the payload."""
    markdown_path = page_payload.get("source_page_markdown_path")
    if markdown_path:
        return str(markdown_path)
    return str(page_payload.get("markdown_path", ""))


def _chunk_file_name(chunk: ContentChunk) -> str:
    """Return the per-chunk artifact file name."""
    if chunk.item_type == "page":
        return f"page_{chunk.sheet_number:03d}_chunk_{chunk.chunk_index:03d}.json"
    return f"sheet_{chunk.sheet_number:03d}_chunk_{chunk.chunk_index:03d}.json"


def _with_artifact_path(chunk: ContentChunk, artifact_path: Path) -> ContentChunk:
    """Return a chunk copy with its artifact path populated."""
    return ContentChunk(
        source=chunk.source,
        source_workbook_artifact_path=chunk.source_workbook_artifact_path,
        source_sheet_artifact_path=chunk.source_sheet_artifact_path,
        file_id=chunk.file_id,
        sheet_number=chunk.sheet_number,
        sheet_title=chunk.sheet_title,
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
        chunk_context=chunk.chunk_context,
        content=chunk.content,
        embedding_content=chunk.embedding_content,
        embedding_format=chunk.embedding_format,
        content_token_count=chunk.content_token_count,
        embedding_source_token_count=chunk.embedding_source_token_count,
        embedding_token_count=chunk.embedding_token_count,
        embedding_token_limit=chunk.embedding_token_limit,
        embedding_content_truncated=chunk.embedding_content_truncated,
        embedding_tokens_omitted=chunk.embedding_tokens_omitted,
        token_tier=chunk.token_tier,
        was_chunked=chunk.was_chunked,
        was_truncated=chunk.was_truncated,
        chunking_strategy=chunk.chunking_strategy,
        artifact_path=str(artifact_path),
        item_type=chunk.item_type,
        markdown_path=chunk.markdown_path,
    )


def _item_records(
    chunk_records: list[dict[str, Any]],
    extraction: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build one item summary record per extracted page or sheet."""
    extraction_sheets = {
        int(sheet["sheet_number"]): sheet
        for sheet in extraction.get("sheets", [])
        if isinstance(sheet, dict) and "sheet_number" in sheet
    }
    extraction_pages = {
        int(page["page_number"]): page
        for page in extraction.get("pages", [])
        if isinstance(page, dict) and "page_number" in page
    }
    records = []
    for chunk in chunk_records:
        item_type = str(chunk.get("item_type", "sheet"))
        item_number = int(chunk["item_number"])
        extraction_item = (
            extraction_pages.get(item_number, {})
            if item_type == "page"
            else extraction_sheets.get(item_number, {})
        )
        record = {
            "item_type": item_type,
            "item_number": item_number,
            "item_title": chunk["item_title"],
            "content_token_count": chunk["content_token_count"],
            "embedding_source_token_count": chunk["embedding_source_token_count"],
            "embedding_token_count": chunk["embedding_token_count"],
            "embedding_token_limit": chunk["embedding_token_limit"],
            "embedding_content_truncated": chunk["embedding_content_truncated"],
            "embedding_tokens_omitted": chunk["embedding_tokens_omitted"],
            "token_tier": chunk["token_tier"],
            "chunk_count": 1,
            "chunk_ids": [chunk["chunk_id"]],
            "chunk_artifact_paths": [chunk["artifact_path"]],
            "was_chunked": chunk["was_chunked"],
            "was_truncated": chunk["was_truncated"],
            "chunking_strategy": chunk["chunking_strategy"],
        }
        if item_type == "page":
            record.update(
                {
                    "page_number": item_number,
                    "page_title": chunk["item_title"],
                    "page_json_path": extraction_item.get("page_json_path", ""),
                    "markdown_path": extraction_item.get("markdown_path", ""),
                    "base_markdown_chars": extraction_item.get(
                        "base_markdown_chars", 0
                    ),
                    "final_markdown_chars": extraction_item.get(
                        "final_markdown_chars", 0
                    ),
                }
            )
        else:
            record.update(
                {
                    "sheet_number": item_number,
                    "sheet_title": chunk["item_title"],
                    "sheet_json_path": extraction_item.get("sheet_json_path", ""),
                    "row_count": extraction_item.get("row_count", 0),
                    "used_range": extraction_item.get("used_range", ""),
                }
            )
        records.append(record)
    return records


def _sheet_records(
    chunk_records: list[dict[str, Any]],
    extraction: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build one sheet summary record per extracted sheet."""
    return [
        item
        for item in _item_records(chunk_records, extraction)
        if item.get("item_type") == "sheet"
    ]


def _page_records(
    chunk_records: list[dict[str, Any]],
    extraction: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build one summary record per extracted transcript unit."""
    return [
        item
        for item in _item_records(chunk_records, extraction)
        if item.get("item_type") == "page"
    ]


def _chunk_index_records(
    chunk_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build compact chunk index records for the document artifact."""
    return [
        {
            "chunk_id": chunk["chunk_id"],
            "item_type": chunk.get("item_type", "sheet"),
            "item_number": chunk["item_number"],
            "item_title": chunk["item_title"],
            "sheet_number": chunk["sheet_number"],
            "sheet_title": chunk["sheet_title"],
            "page_number": chunk.get("page_number", 0),
            "page_title": chunk.get("page_title", ""),
            "chunk_index": chunk["chunk_index"],
            "chunk_context": chunk["chunk_context"],
            "artifact_path": chunk["artifact_path"],
            "content_token_count": chunk["content_token_count"],
            "embedding_source_token_count": chunk["embedding_source_token_count"],
            "embedding_token_count": chunk["embedding_token_count"],
            "embedding_content_truncated": chunk["embedding_content_truncated"],
            "embedding_tokens_omitted": chunk["embedding_tokens_omitted"],
            "token_tier": chunk["token_tier"],
            "was_chunked": chunk["was_chunked"],
            "was_truncated": chunk["was_truncated"],
            "chunking_strategy": chunk["chunking_strategy"],
        }
        for chunk in chunk_records
    ]


def _artifact_embedding_format(chunk_records: list[dict[str, Any]]) -> str:
    """Return the shared embedding format for a chunking artifact."""
    formats = {
        str(chunk.get("embedding_format", ""))
        for chunk in chunk_records
        if chunk.get("embedding_format")
    }
    if len(formats) == 1:
        return next(iter(formats))
    if not formats:
        return ""
    return "mixed"


def _token_summary(chunk_records: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize token counts for one document's chunks."""
    if not chunk_records:
        return {
            "content_document_token_count": 0,
            "embedding_source_document_token_count": 0,
            "embedding_document_token_count": 0,
            "max_content_token_count": 0,
            "max_embedding_source_token_count": 0,
            "max_embedding_token_count": 0,
            "embedding_tokens_omitted": 0,
            "token_tier_counts": {"low": 0, "medium": 0, "high": 0},
        }
    tier_counts = {"low": 0, "medium": 0, "high": 0}
    for chunk in chunk_records:
        tier = str(chunk["token_tier"])
        if tier in tier_counts:
            tier_counts[tier] += 1
    return {
        "content_document_token_count": sum(
            int(chunk["content_token_count"]) for chunk in chunk_records
        ),
        "embedding_source_document_token_count": sum(
            int(chunk["embedding_source_token_count"]) for chunk in chunk_records
        ),
        "embedding_document_token_count": sum(
            int(chunk["embedding_token_count"]) for chunk in chunk_records
        ),
        "max_content_token_count": max(
            int(chunk["content_token_count"]) for chunk in chunk_records
        ),
        "max_embedding_source_token_count": max(
            int(chunk["embedding_source_token_count"]) for chunk in chunk_records
        ),
        "max_embedding_token_count": max(
            int(chunk["embedding_token_count"]) for chunk in chunk_records
        ),
        "embedding_tokens_omitted": sum(
            int(chunk["embedding_tokens_omitted"]) for chunk in chunk_records
        ),
        "token_tier_counts": tier_counts,
    }


def _chunking_qa_findings(chunk_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return chunking QA findings from chunk records."""
    findings = []
    for chunk in chunk_records:
        item_type = str(chunk.get("item_type", "sheet"))
        item_label = "page" if item_type == "page" else "sheet"
        if chunk["was_truncated"]:
            findings.append(
                {
                    "code": f"{item_label}_content_truncated",
                    "severity": "medium",
                    "message": (
                        f"Full {item_label} markdown was truncated in chunk content"
                    ),
                    "item_type": item_type,
                    "item_number": int(chunk["item_number"]),
                    "sheet_number": int(chunk["sheet_number"]),
                    "chunk_id": chunk["chunk_id"],
                    "artifact_path": chunk["artifact_path"],
                }
            )
        if chunk["embedding_content_truncated"]:
            findings.append(
                {
                    "code": "embedding_content_truncated",
                    "severity": "low",
                    "message": (
                        f"Compact {item_label} embedding content exceeded the embedding "
                        "token limit and was truncated"
                    ),
                    "item_type": item_type,
                    "item_number": int(chunk["item_number"]),
                    "sheet_number": int(chunk["sheet_number"]),
                    "chunk_id": chunk["chunk_id"],
                    "embedding_source_token_count": int(
                        chunk["embedding_source_token_count"]
                    ),
                    "embedding_token_count": int(chunk["embedding_token_count"]),
                    "artifact_path": chunk["artifact_path"],
                }
            )
    return findings


def _normalize_embedding_value(value: Any) -> str:
    """Normalize one cell value for compact embedding text."""
    return " ".join(str(value).replace("\n", " ").split())


def _embedding_cell_label(cell: dict[str, Any]) -> str:
    """Return the compact column label for one extracted cell."""
    column_letter = str(cell.get("column_letter", "")).strip()
    if column_letter:
        return column_letter
    address = str(cell.get("address", "")).strip()
    return "".join(char for char in address if char.isalpha())


def _markdown_title(markdown: str) -> str:
    """Return the first markdown heading text from extracted page markdown."""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _qa_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    """Count QA findings by supported severity."""
    counts = {"high": 0, "low": 0, "medium": 0}
    for finding in findings:
        severity = str(finding.get("severity", "")).lower()
        if severity in counts:
            counts[severity] += 1
    return counts


def _qa_status(findings: list[dict[str, Any]]) -> str:
    """Return failed when high-severity findings are present."""
    return (
        "failed"
        if any(finding.get("severity") == "high" for finding in findings)
        else "passed"
    )


def _tokenizer_cache_files(cache_dir: Path) -> list[dict[str, Any]]:
    """Return stable tokenizer cache file metadata."""
    if not cache_dir.exists():
        return []
    return [
        {
            "name": path.name,
            "size": path.stat().st_size,
        }
        for path in sorted(cache_dir.iterdir())
        if path.is_file()
    ]


def _validate_tokenizer_cache(cache_dir: Path) -> None:
    """Ensure the repo-local tokenizer cache is present before token counting."""
    if not cache_dir.is_dir():
        raise ChunkingStageError(f"Tokenizer cache directory is missing: {cache_dir}")
    if not any(path.is_file() for path in cache_dir.iterdir()):
        raise ChunkingStageError(f"Tokenizer cache directory is empty: {cache_dir}")


def _load_process_records(progress_dir: Path) -> tuple[ManifestRecord, ...]:
    """Load manifest records selected for processing."""
    path = progress_dir / FILES_TO_PROCESS_FILE_NAME
    payload = _read_required_json(path)
    rows = payload.get("files_to_process")
    if not isinstance(rows, list):
        raise ChunkingStageError(f"{path} is missing files_to_process list")
    return tuple(_manifest_record_from_mapping(row, path) for row in rows)


def _manifest_record_from_mapping(row: Any, source_path: Path) -> ManifestRecord:
    """Convert one progress JSON row into a manifest record."""
    if not isinstance(row, dict):
        raise ChunkingStageError(f"Progress row is not an object in {source_path}")
    missing = [field for field in MANIFEST_FIELDS if field not in row]
    if missing:
        raise ChunkingStageError(
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


def _source_record(record: ManifestRecord) -> dict[str, Any]:
    """Build fallback source metadata when extraction source is unavailable."""
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
        "period": f"{record.fiscal_year}_{record.quarter}",
    }


def _artifact_root(progress_dir: Path, file_id: str) -> Path:
    """Return the per-file artifact root created by the manifest stage."""
    return progress_dir / ARTIFACTS_DIR_NAME / file_id


def _read_required_json(path: Path) -> Any:
    """Read required UTF-8 JSON and raise a stage error on failure."""
    if not path.is_file():
        raise ChunkingStageError(f"Required JSON artifact is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ChunkingStageError(f"Invalid JSON artifact: {path}") from exc


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
