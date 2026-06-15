"""Single-pass PDF OCR extraction stage for RTS documents."""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from ..connections.llm_connector import LLMClient
from ..utils.config_setup import (
    PdfExtractionConfig,
    get_input_source_config,
    get_pdf_extraction_config,
    load_config,
)
from ..utils.logging_setup import get_stage_logger
from ..utils.prompt_loader import load_prompt
from ..utils.qa import finding_records, qa_counts, qa_status
from .manifest import (
    ARTIFACTS_DIR_NAME,
    FILES_TO_PROCESS_FILE_NAME,
    PROGRESS_DIR,
    ManifestRecord,
)

LOGGER = logging.getLogger(__name__)

DOCUMENT_ARTIFACT_FILE_NAME = "document.json"
WORKBOOK_ARTIFACT_FILE_NAME = DOCUMENT_ARTIFACT_FILE_NAME
EXTRACTION_DIR_NAME = "extraction"
EXTRACTION_MANIFEST_FILE_NAME = "extraction_manifest.json"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
FITZ_ERRORS = (RuntimeError, ValueError, OSError)
FITZ_RENDER_LOCK = threading.Lock()
DEFAULT_RAW_OCR_MODEL = "gpt-5-mini"
DEFAULT_RAW_OCR_DETAIL = "high"
DEFAULT_RAW_OCR_REASONING_EFFORT = "low"
DEFAULT_RAW_OCR_MAX_OUTPUT_TOKENS = 12000
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


class ExtractionStageError(RuntimeError):
    """Raised when the PDF extraction stage cannot continue safely."""


@dataclass(frozen=True)
class RenderedPage:
    """A single rendered PDF page."""

    page_number: int
    image_bytes: bytes


@dataclass(frozen=True)
class RenderedPdf:
    """Open PDF handle configured for rendering."""

    pdf_path: Path
    document: Any
    matrix: Any
    total_pages: int


@dataclass(frozen=True)
class PageLayout:
    """Minimal page layout metadata for raw OCR artifacts."""

    page_text_markdown: str
    rationale: str

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-serializable layout record."""
        return {
            "page_text_markdown": self.page_text_markdown,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class ExtractedPdfPage:
    """One fully extracted PDF page."""

    page_number: int
    image_bytes: bytes
    markdown: str
    base_markdown: str = ""
    layout: PageLayout | None = None


@dataclass(frozen=True)
class ExtractedPdfDocument:
    """Extracted PDF pages with source document selection metadata."""

    pages: list[ExtractedPdfPage]
    source_page_count: int
    selected_page_numbers: list[int]


@dataclass(frozen=True)
class PdfExtractionContext:
    """Shared state for a single PDF extraction run."""

    client: Any
    config: Any
    file_label: str
    total_pages: int
    page_ocr_prompt: dict[str, Any]


@dataclass(frozen=True)
class PdfDocumentArtifacts:
    """Paths and counts written for one extracted PDF."""

    artifact_root: Path
    document_json: Path
    document_markdown: Path
    page_count: int
    source_page_count: int
    selected_page_numbers: tuple[int, ...]
    qa_report_json: Path
    qa_status: str


@dataclass(frozen=True)
class ExtractionStageResult:
    """Summary returned after writing PDF extraction artifacts."""

    processed_file_count: int
    document_artifact_paths: tuple[Path, ...]

    @property
    def workbook_artifact_paths(self) -> tuple[Path, ...]:
        """Compatibility alias until downstream stages are renamed."""
        return self.document_artifact_paths


def _load_openai_retry_errors() -> tuple[type[BaseException], ...]:
    """Return OpenAI retryable exception classes when the SDK is installed."""
    try:
        openai_module = __import__("openai")
    except ModuleNotFoundError:
        return tuple()
    error_names = (
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
    )
    errors = []
    for error_name in error_names:
        error_class = getattr(openai_module, error_name, None)
        if isinstance(error_class, type) and issubclass(error_class, BaseException):
            errors.append(error_class)
    return tuple(errors)


RETRYABLE_ERRORS = _load_openai_retry_errors()
PARSE_RETRYABLE_ERRORS = RETRYABLE_ERRORS + (ValueError,)


def run_extraction_stage(
    input_base_path: Path | None = None,
    progress_dir: Path = PROGRESS_DIR,
    max_pages: int | None = None,
    page_numbers: list[int] | tuple[int, ...] | None = None,
    llm_client: LLMClient | None = None,
    pdf_config: PdfExtractionConfig | None = None,
) -> ExtractionStageResult:
    """Extract all pending RTS PDFs selected by the manifest stage.

    Args:
        input_base_path: Optional local RTS input folder override.
        progress_dir: Folder containing manifest progress files and artifacts.
        max_pages: Optional first-N page limit for deterministic smoke runs.
        page_numbers: Optional explicit one-indexed source page numbers.
        llm_client: Optional LLM client override for tests.
        pdf_config: Optional PDF extraction config override for tests.

    Returns:
        ExtractionStageResult with the document artifact paths written.

    Raises:
        ExtractionStageError: If progress files are invalid, source PDFs are
            missing, or a pending record is not a PDF.
        NotImplementedError: If configured input source is not local.

    External side effects:
        Writes artifacts under ``progress_dir/artifacts/<file_id>/extraction``.
    """
    load_config()
    logger = get_stage_logger(__name__, "EXTRACTION")
    input_base = _resolve_input_base_path(input_base_path)
    records = _load_process_records(progress_dir)
    processed_at = _utc_now()
    client = llm_client or LLMClient()
    runtime_config = SimpleNamespace(
        pdf_extraction=pdf_config or get_pdf_extraction_config()
    )
    document_paths: list[Path] = []

    for index, record in enumerate(records, start=1):
        if record.file_type.lower() != "pdf":
            raise ExtractionStageError(
                f"Extraction received non-pdf file: {record.file_id}"
            )
        source_path = input_base / record.file_path
        if not source_path.is_file():
            raise ExtractionStageError(f"Source PDF is missing: {source_path}")

        logger.info(
            "Extracting PDF %d/%d: %s",
            index,
            len(records),
            record.file_name,
        )
        file_started_at = _utc_now()
        file_start = time.perf_counter()
        document = extract_pdf_document_pages(
            source_path,
            client=client,
            config=runtime_config,
            max_pages=max_pages,
            page_numbers=page_numbers,
        )
        duration_seconds = time.perf_counter() - file_start
        file_completed_at = _utc_now()
        artifacts = write_pdf_document_artifacts(
            record,
            source_path,
            document,
            _artifact_root(progress_dir, record.file_id) / EXTRACTION_DIR_NAME,
            processed_at,
            file_started_at=file_started_at,
            file_completed_at=file_completed_at,
            duration_seconds=duration_seconds,
        )
        document_paths.append(artifacts.document_json)

    logger.info("Extraction complete: files=%d", len(records))
    return ExtractionStageResult(
        processed_file_count=len(records),
        document_artifact_paths=tuple(document_paths),
    )


def extract_pdf_pages(
    pdf_path: Path,
    client: Any,
    config: Any,
    max_pages: int | None = None,
    page_numbers: list[int] | tuple[int, ...] | None = None,
) -> list[ExtractedPdfPage]:
    """Render and OCR pages from one PDF file."""
    return extract_pdf_document_pages(
        pdf_path,
        client,
        config,
        max_pages=max_pages,
        page_numbers=page_numbers,
    ).pages


def extract_pdf_document_pages(
    pdf_path: Path,
    client: Any,
    config: Any,
    max_pages: int | None = None,
    page_numbers: list[int] | tuple[int, ...] | None = None,
) -> ExtractedPdfDocument:
    """Render selected PDF pages and OCR each page with one GPT-5.4 call."""
    LOGGER.info("PDF raw OCR open start: %s", pdf_path.name)
    page_ocr_prompt = load_prompt("page_ocr", PROMPTS_DIR)
    with open_rendered_pdf(
        pdf_path,
        config.pdf_extraction.vision_dpi_scale,
    ) as rendered:
        selected_page_numbers = _selected_page_numbers(
            rendered.total_pages,
            max_pages,
            page_numbers,
        )
        context = PdfExtractionContext(
            client=client,
            config=config,
            file_label=pdf_path.name,
            total_pages=rendered.total_pages,
            page_ocr_prompt=page_ocr_prompt,
        )
        pages = _extract_rendered_pages(rendered, context, selected_page_numbers)
        LOGGER.info("PDF raw OCR done: %s pages=%d", pdf_path.name, len(pages))
        return ExtractedPdfDocument(
            pages=pages,
            source_page_count=rendered.total_pages,
            selected_page_numbers=selected_page_numbers,
        )


def _extract_rendered_pages(
    rendered_pdf: RenderedPdf,
    context: PdfExtractionContext,
    selected_page_numbers: list[int],
) -> list[ExtractedPdfPage]:
    """Render pages sequentially and OCR them with bounded concurrency."""
    if not selected_page_numbers:
        return []
    page_workers = min(
        context.config.pdf_extraction.page_workers,
        len(selected_page_numbers),
    )
    results: list[ExtractedPdfPage | None] = [None] * len(selected_page_numbers)
    in_flight: dict[Any, int] = {}

    with ThreadPoolExecutor(max_workers=page_workers) as pool:
        for result_index, page_number in enumerate(selected_page_numbers):
            rendered_page = RenderedPage(
                page_number=page_number,
                image_bytes=render_page(rendered_pdf, page_number),
            )
            future = pool.submit(_extract_single_page, context, rendered_page)
            in_flight[future] = result_index
            if len(in_flight) >= page_workers:
                completed = next(as_completed(in_flight))
                completed_index = in_flight.pop(completed)
                results[completed_index] = completed.result()

        for completed in as_completed(in_flight):
            completed_index = in_flight[completed]
            results[completed_index] = completed.result()

    return [page for page in results if page is not None]


@contextmanager
def open_rendered_pdf(pdf_path: Path, dpi_scale: float) -> Iterator[RenderedPdf]:
    """Open a PDF once for page-by-page rendering."""
    fitz_module = _load_fitz_module()
    try:
        document = _open_fitz_document(fitz_module, pdf_path)
    except FITZ_ERRORS as exc:
        raise RuntimeError(f"Failed to open PDF '{pdf_path.name}': {exc}") from exc

    rendered = RenderedPdf(
        pdf_path=pdf_path,
        document=document,
        matrix=fitz_module.Matrix(dpi_scale, dpi_scale),
        total_pages=document.page_count,
    )
    try:
        yield rendered
    finally:
        document.close()


def render_page(rendered_pdf: RenderedPdf, page_number: int) -> bytes:
    """Render one 1-indexed PDF page to PNG bytes."""
    if page_number < 1 or page_number > rendered_pdf.total_pages:
        raise ValueError(
            f"Page {page_number} is out of range for '{rendered_pdf.pdf_path.name}'"
        )
    try:
        return _render_fitz_page_to_png(
            _load_fitz_module(),
            rendered_pdf.document,
            page_number - 1,
            rendered_pdf.matrix,
        )
    except FITZ_ERRORS as exc:
        raise RuntimeError(
            f"Failed to render page {page_number} "
            f"of '{rendered_pdf.pdf_path.name}': {exc}"
        ) from exc


def render_all_pages(rendered_pdf: RenderedPdf) -> list[RenderedPage]:
    """Render all PDF pages sequentially."""
    return [
        RenderedPage(
            page_number=page_number,
            image_bytes=render_page(rendered_pdf, page_number),
        )
        for page_number in range(1, rendered_pdf.total_pages + 1)
    ]


def _extract_single_page(
    context: PdfExtractionContext,
    rendered_page: RenderedPage,
) -> ExtractedPdfPage:
    """OCR one rendered page with a single full-page request."""
    page_context = (
        f"{context.file_label} page {rendered_page.page_number}/"
        f"{context.total_pages} raw ocr"
    )
    markdown = _call_raw_page_ocr_with_retries(
        context,
        rendered_page.image_bytes,
        page_context,
    )
    layout = PageLayout(
        page_text_markdown=markdown,
        rationale="Single-pass full-page RTS OCR.",
    )
    return ExtractedPdfPage(
        page_number=rendered_page.page_number,
        image_bytes=rendered_page.image_bytes,
        markdown=markdown,
        base_markdown=markdown,
        layout=layout,
    )


def _call_raw_page_ocr_with_retries(
    context: PdfExtractionContext,
    img_bytes: bytes,
    page_context: str,
) -> str:
    """Call raw OCR with retry handling and return markdown."""
    extraction_config = context.config.pdf_extraction
    last_error: BaseException | None = None
    for attempt in range(1, extraction_config.max_retries + 1):
        try:
            response = _create_raw_ocr_chat_completion(
                context,
                img_bytes,
                page_context,
            )
            markdown = _chat_completion_output_text(response).strip()
            if not markdown:
                raise ValueError("raw OCR response did not include output text")
            return markdown
        except PARSE_RETRYABLE_ERRORS as exc:
            last_error = exc
            if attempt >= extraction_config.max_retries:
                break
            LOGGER.warning(
                "PDF raw OCR retrying: %s attempt=%d/%d error=%s",
                page_context,
                attempt,
                extraction_config.max_retries,
                exc,
            )
            time.sleep(extraction_config.retry_delay_seconds * attempt)
    raise RuntimeError(f"Raw OCR failed for {page_context}: {last_error}") from last_error


def _create_raw_ocr_chat_completion(
    context: PdfExtractionContext,
    img_bytes: bytes,
    page_context: str,
) -> Any:
    """Create a chat-completions raw OCR request."""
    extraction_config = context.config.pdf_extraction
    chat_client = _chat_client(context.client)
    model = getattr(extraction_config, "raw_ocr_model", DEFAULT_RAW_OCR_MODEL)
    detail = getattr(extraction_config, "raw_ocr_detail", DEFAULT_RAW_OCR_DETAIL)
    reasoning_effort = getattr(
        extraction_config,
        "raw_ocr_reasoning_effort",
        DEFAULT_RAW_OCR_REASONING_EFFORT,
    )
    max_output_tokens = getattr(
        extraction_config,
        "raw_ocr_max_output_tokens",
        DEFAULT_RAW_OCR_MAX_OUTPUT_TOKENS,
    )
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": _raw_ocr_prompt_text(
                            context.page_ocr_prompt,
                            page_context,
                        ),
                    },
                    _chat_image_content(img_bytes, detail),
                ],
            }
        ],
        "max_completion_tokens": max_output_tokens,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    return chat_client.chat.completions.create(**kwargs)


def _chat_client(client: Any) -> Any:
    """Return an OpenAI client exposing the chat-completions API."""
    if hasattr(client, "get_client"):
        return client.get_client()
    return client


def _raw_ocr_prompt_text(prompt: dict[str, Any], page_context: str) -> str:
    """Build the single-pass OCR prompt sent with the page image."""
    system_prompt = str(prompt.get("system_prompt", "")).strip()
    user_prompt = str(prompt.get("user_prompt", "")).strip()
    return (
        f"{system_prompt}\n\n"
        f"{user_prompt}\n\n"
        f"Source context: {page_context}.\n"
        "Return markdown only."
    ).strip()


def _chat_image_content(img_bytes: bytes, detail: str) -> dict[str, Any]:
    """Build a chat-completions image content part from PNG bytes."""
    b64 = base64.b64encode(img_bytes).decode("ascii")
    image_url = {"url": f"data:image/png;base64,{b64}"}
    normalized_detail = _chat_image_detail(detail)
    if normalized_detail:
        image_url["detail"] = normalized_detail
    return {
        "type": "image_url",
        "image_url": image_url,
    }


def _chat_image_detail(detail: str) -> str:
    """Return a chat-completions image detail value."""
    normalized = str(detail or "").strip().lower()
    if normalized == "original":
        return "high"
    if normalized in {"auto", "high", "low"}:
        return normalized
    return DEFAULT_RAW_OCR_DETAIL


def _chat_completion_output_text(response: Any) -> str:
    """Extract assistant text from an OpenAI chat-completions result."""
    if isinstance(response, dict):
        choices = response.get("choices", [])
    else:
        choices = getattr(response, "choices", [])
    if not choices:
        return ""
    first_choice = choices[0]
    message = (
        first_choice.get("message", {})
        if isinstance(first_choice, dict)
        else getattr(first_choice, "message", None)
    )
    if message is None:
        return ""
    content = (
        message.get("content", "")
        if isinstance(message, dict)
        else getattr(message, "content", "")
    )
    return _message_content_text(content)


def _message_content_text(content: Any) -> str:
    """Return text from string or structured chat message content."""
    if isinstance(content, str):
        return content
    chunks: list[str] = []
    for part in content or []:
        if isinstance(part, str):
            chunks.append(part)
            continue
        text = (
            part.get("text")
            if isinstance(part, dict)
            else getattr(part, "text", None)
        )
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)


def write_document_artifact(
    record: ManifestRecord,
    source_path: Path,
    document: ExtractedPdfDocument,
    extraction_root: Path,
    processed_at: str,
    *,
    file_started_at: str | None = None,
    file_completed_at: str | None = None,
    duration_seconds: float | None = None,
) -> Path:
    """Write one PDF document artifact set and return document.json."""
    artifacts = write_pdf_document_artifacts(
        record,
        source_path,
        document,
        extraction_root,
        processed_at,
        file_started_at=file_started_at,
        file_completed_at=file_completed_at,
        duration_seconds=duration_seconds,
    )
    return artifacts.document_json


def write_pdf_document_artifacts(
    record: ManifestRecord,
    source_path: Path,
    document: ExtractedPdfDocument,
    extraction_root: Path,
    processed_at: str,
    *,
    file_started_at: str | None = None,
    file_completed_at: str | None = None,
    duration_seconds: float | None = None,
) -> PdfDocumentArtifacts:
    """Write inspectable PNG, JSON, and markdown artifacts for one PDF."""
    extraction_root.mkdir(parents=True, exist_ok=True)
    source = _source_record(record, source_path)
    page_records, document_markdown = _write_document_outputs(
        source,
        document.pages,
        extraction_root,
        processed_at,
    )
    findings: list[Any] = []
    qa_record = {
        "qa_status": qa_status(findings),
        "qa_counts": qa_counts(findings),
        "findings": finding_records(findings),
    }
    qa_report_json = extraction_root / "qa_report.json"
    _write_json(
        qa_report_json,
        {
            "stage": "extraction_qa",
            "processed_at": processed_at,
            "source": source,
            **qa_record,
        },
    )

    document_json = extraction_root / DOCUMENT_ARTIFACT_FILE_NAME
    payload = {
        "stage": "extraction",
        "extraction_type": "pdf_rts_raw_ocr",
        "processed_at": processed_at,
        "file_started_at": file_started_at or processed_at,
        "file_completed_at": file_completed_at or processed_at,
        "duration_seconds": round(float(duration_seconds or 0.0), 6),
        "source": source,
        "page_count": len(document.pages),
        "source_page_count": document.source_page_count,
        "selected_page_numbers": list(document.selected_page_numbers),
        "extracted_page_count": len(document.pages),
        "visual_region_count": 0,
        "qa_status": qa_record["qa_status"],
        "qa_counts": qa_record["qa_counts"],
        "qa_report_path": str(qa_report_json),
        "document_markdown_path": str(document_markdown),
        "pages": page_records,
    }
    _write_json(document_json, payload)
    _write_extraction_manifest(extraction_root, payload)
    return PdfDocumentArtifacts(
        artifact_root=extraction_root,
        document_json=document_json,
        document_markdown=document_markdown,
        page_count=len(document.pages),
        source_page_count=document.source_page_count,
        selected_page_numbers=tuple(document.selected_page_numbers),
        qa_report_json=qa_report_json,
        qa_status=str(qa_record["qa_status"]),
    )


def _write_document_outputs(
    source: dict[str, Any],
    pages: list[ExtractedPdfPage],
    artifact_root: Path,
    processed_at: str,
) -> tuple[list[dict[str, Any]], Path]:
    """Write page-level outputs and the recombined document markdown."""
    page_records = []
    document_parts = [f"# {source['file_name']}"]
    for page in pages:
        page_records.append(
            _write_page_artifacts(source, page, artifact_root, processed_at)
        )
        document_parts.append(f"## Page {page.page_number}\n\n{page.markdown}")

    document_markdown = artifact_root / "document.md"
    document_markdown.write_text(
        "\n\n".join(document_parts).strip() + "\n",
        encoding="utf-8",
    )
    return page_records, document_markdown


def _write_page_artifacts(
    source: dict[str, Any],
    page: ExtractedPdfPage,
    artifact_root: Path,
    processed_at: str,
) -> dict[str, Any]:
    """Write artifacts for one extracted page and return its index record."""
    page_label = f"page_{page.page_number:03d}"
    page_json = artifact_root / "pages" / f"{page_label}.json"
    page_image = artifact_root / "pages" / f"{page_label}.png"
    base_markdown = artifact_root / "base_markdown" / f"{page_label}.md"
    markdown = artifact_root / "markdown" / f"{page_label}.md"
    layout_json = artifact_root / "layouts" / f"{page_label}_layout.json"
    for path in (page_json, page_image, base_markdown, markdown, layout_json):
        path.parent.mkdir(parents=True, exist_ok=True)

    final_markdown = page.markdown.strip()
    raw_markdown = (page.base_markdown or page.markdown).strip()
    page_image.write_bytes(page.image_bytes)
    base_markdown.write_text(raw_markdown + "\n", encoding="utf-8")
    markdown.write_text(final_markdown + "\n", encoding="utf-8")
    layout = page.layout or PageLayout(
        page_text_markdown=final_markdown,
        rationale="Single-pass OCR page artifact.",
    )
    page_record = {
        "page_number": page.page_number,
        "page_json_path": str(page_json),
        "page_image_path": str(page_image),
        "layout_json_path": str(layout_json),
        "base_markdown_path": str(base_markdown),
        "markdown_path": str(markdown),
        "base_markdown_chars": len(raw_markdown),
        "final_markdown_chars": len(final_markdown),
        "qa_status": "passed",
        "qa_counts": qa_counts([]),
        "qa_findings": [],
    }
    _write_json(
        layout_json,
        {
            "stage": "extraction_layout",
            "processed_at": processed_at,
            "source": source,
            "page_number": page.page_number,
            **layout.to_record(),
            "base_markdown_path": str(base_markdown),
            "final_markdown_path": str(markdown),
        },
    )
    _write_json(
        page_json,
        {
            "stage": "extraction_page",
            "processed_at": processed_at,
            "source": source,
            "page_number": page.page_number,
            "markdown": page.markdown,
            "base_markdown": page.base_markdown or page.markdown,
            "layout": layout.to_record(),
            "layout_json_path": str(layout_json),
            "base_markdown_path": str(base_markdown),
            "markdown_path": str(markdown),
            "page_image_path": str(page_image),
            "qa_status": page_record["qa_status"],
            "qa_counts": page_record["qa_counts"],
        },
    )
    return page_record


def _write_extraction_manifest(
    extraction_root: Path,
    document_artifact: dict[str, Any],
) -> None:
    """Write a compact extraction-stage manifest next to document.json."""
    _write_json(
        extraction_root / EXTRACTION_MANIFEST_FILE_NAME,
        {
            "stage": document_artifact["stage"],
            "extraction_type": document_artifact["extraction_type"],
            "processed_at": document_artifact["processed_at"],
            "source": document_artifact["source"],
            "document_artifact_path": str(
                extraction_root / DOCUMENT_ARTIFACT_FILE_NAME
            ),
            "document_markdown_path": document_artifact["document_markdown_path"],
            "duration_seconds": document_artifact["duration_seconds"],
            "page_count": document_artifact["page_count"],
            "visual_region_count": document_artifact["visual_region_count"],
            "qa_status": document_artifact["qa_status"],
            "qa_counts": document_artifact["qa_counts"],
        },
    )


def _load_process_records(progress_dir: Path) -> tuple[ManifestRecord, ...]:
    """Load manifest records selected for processing."""
    path = progress_dir / FILES_TO_PROCESS_FILE_NAME
    payload = _read_required_json(path)
    rows = payload.get("files_to_process")
    if not isinstance(rows, list):
        raise ExtractionStageError(f"{path} is missing files_to_process list")
    return tuple(_manifest_record_from_mapping(row, path) for row in rows)


def _manifest_record_from_mapping(row: Any, source_path: Path) -> ManifestRecord:
    """Convert one progress JSON row into a manifest record."""
    if not isinstance(row, Mapping):
        raise ExtractionStageError(f"Progress row is not an object in {source_path}")
    missing = [field for field in MANIFEST_FIELDS if field not in row]
    if missing:
        raise ExtractionStageError(
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


def _resolve_input_base_path(input_base_path: Path | None) -> Path:
    """Resolve the local RTS input folder."""
    if input_base_path is not None:
        path = input_base_path.expanduser().resolve()
        if not path.is_dir():
            raise ExtractionStageError(f"Input base path is not a directory: {path}")
        return path
    load_config()
    input_config = get_input_source_config()
    if input_config.source != "local":
        raise NotImplementedError(
            "Extraction currently supports local input paths only."
        )
    configured_path = input_config.base_path
    if not isinstance(configured_path, Path):
        configured_path = Path(configured_path)
    return configured_path.expanduser().resolve()


def _artifact_root(progress_dir: Path, file_id: str) -> Path:
    """Return the per-file artifact root created by the manifest stage."""
    return progress_dir / ARTIFACTS_DIR_NAME / file_id


def _read_required_json(path: Path) -> Any:
    """Read required UTF-8 JSON and raise a stage error on failure."""
    if not path.is_file():
        raise ExtractionStageError(f"Required JSON artifact is missing: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExtractionStageError(f"Invalid JSON artifact: {path}") from exc


def _source_record(record: ManifestRecord, source_path: Path) -> dict[str, Any]:
    """Return source metadata stored in extraction artifacts."""
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
        "source_path": str(source_path),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a stable JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _utc_now() -> str:
    """Return the current UTC time as an ISO string."""
    return datetime.now(tz=UTC).isoformat()


def _bounded_page_count(total_pages: int, max_pages: int | None) -> int:
    """Apply an optional page limit to a document page count."""
    if total_pages <= 0:
        return 0
    if max_pages is None:
        return total_pages
    if max_pages < 1:
        raise ValueError("max_pages must be >= 1")
    return min(total_pages, max_pages)


def _selected_page_numbers(
    total_pages: int,
    max_pages: int | None,
    page_numbers: list[int] | tuple[int, ...] | None,
) -> list[int]:
    """Resolve page-limit settings to concrete 1-indexed page numbers."""
    if max_pages is not None and page_numbers is not None:
        raise ValueError("max_pages and page_numbers are mutually exclusive")
    if total_pages <= 0:
        return []
    if page_numbers is not None:
        selected = sorted(set(page_numbers))
        invalid = [page_number for page_number in selected if page_number < 1]
        out_of_range = [
            page_number for page_number in selected if page_number > total_pages
        ]
        if invalid or out_of_range:
            raise ValueError(
                "Selected PDF pages out of range: "
                f"{', '.join(str(page) for page in invalid + out_of_range)}"
            )
        return selected
    return list(range(1, _bounded_page_count(total_pages, max_pages) + 1))


def _open_fitz_document(fitz_module: Any, pdf_path: Path) -> Any:
    """Open a fitz document under the shared render lock."""
    with FITZ_RENDER_LOCK:
        fitz_module.TOOLS.mupdf_display_errors(False)
        try:
            document = fitz_module.open(str(pdf_path))
            fitz_module.TOOLS.mupdf_warnings()
            return document
        finally:
            fitz_module.TOOLS.mupdf_display_errors(True)


def _render_fitz_page_to_png(
    fitz_module: Any,
    document: Any,
    page_index: int,
    matrix: Any,
) -> bytes:
    """Render one fitz page to PNG bytes under the shared render lock."""
    with FITZ_RENDER_LOCK:
        fitz_module.TOOLS.mupdf_display_errors(False)
        try:
            try:
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                return pixmap.tobytes("png")
            finally:
                fitz_module.TOOLS.mupdf_warnings()
        finally:
            fitz_module.TOOLS.mupdf_display_errors(True)


def _load_fitz_module() -> Any:
    """Import PyMuPDF lazily so unit tests do not require runtime deps."""
    try:
        return __import__("fitz")
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "PyMuPDF is required for PDF rendering. "
            "Install project dependencies before running extraction."
        ) from exc


__all__ = [
    "DOCUMENT_ARTIFACT_FILE_NAME",
    "EXTRACTION_DIR_NAME",
    "EXTRACTION_MANIFEST_FILE_NAME",
    "MANIFEST_FIELDS",
    "WORKBOOK_ARTIFACT_FILE_NAME",
    "ExtractedPdfDocument",
    "ExtractedPdfPage",
    "ExtractionStageError",
    "ExtractionStageResult",
    "PdfExtractionContext",
    "RenderedPage",
    "extract_pdf_document_pages",
    "extract_pdf_pages",
    "run_extraction_stage",
    "write_document_artifact",
]
