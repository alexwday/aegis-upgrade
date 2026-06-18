"""Generate browser-preview bytes for persisted source documents."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
import textwrap
import warnings
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PDF_MIME_TYPE = "application/pdf"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XML_MIME_TYPES = {"application/xml", "text/xml"}
PREVIEW_RENDERER_VERSION = "source_document_preview_v2"

_SECTION_MD = "MANAGEMENT DISCUSSION SECTION"
_SECTION_QA = "Q&A"
_WHITESPACE_RE = re.compile(r"\s+")
_PAGE_MARGIN = 42
_SOFFICE_TIMEOUT_SECONDS = 180
_SOFFICE_CANDIDATES = (
    "soffice",
    "libreoffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


@dataclass(frozen=True)
class SourceDocumentPreview:
    """Generated preview payload ready to persist in Postgres."""

    preview_mime_type: str
    preview_bytes: bytes
    preview_metadata: dict[str, Any]


@dataclass(frozen=True)
class _TranscriptParticipant:
    """One FactSet transcript participant."""

    participant_id: str
    name: str
    participant_type: str = ""
    title: str = ""
    affiliation: str = ""
    affiliation_entity: str = ""

    @property
    def display_name(self) -> str:
        parts = [self.name or "Unknown Speaker"]
        if self.title and self.title not in parts:
            parts.append(self.title)
        if self.affiliation and self.affiliation not in parts:
            parts.append(self.affiliation)
        return ", ".join(parts)


@dataclass(frozen=True)
class _TranscriptSpeakerBlock:
    """One consecutive speaker block from a transcript body."""

    speaker_block_id: int
    section_name: str
    raw_section_name: str
    participant_id: str
    speaker_type_hint: str
    participant: _TranscriptParticipant
    paragraphs: list[str]

    @property
    def speaker_name(self) -> str:
        return self.participant.name or "Unknown Speaker"


@dataclass(frozen=True)
class _TranscriptDocument:
    """Parsed transcript document."""

    title: str
    transcript_date: str
    participants: dict[str, _TranscriptParticipant]
    blocks: list[_TranscriptSpeakerBlock]
    companies: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _TranscriptUnit:
    """A page-like transcript preview unit."""

    unit_number: int
    unit_id: str
    unit_type: str
    title: str
    markdown: str
    section_name: str
    speaker_block_ids: list[int]
    qa_group_id: int | None = None


@dataclass(frozen=True)
class _WorkbookSheet:
    """One visible workbook sheet selected for preview rendering."""

    sheet_number: int
    name: str
    is_chartsheet: bool
    sheet_state: str


def build_source_document_preview(
    *,
    original_bytes: bytes,
    filename: str,
    file_type: str,
    mime_type: str,
    source_type: str = "",
    file_hash: str = "",
) -> SourceDocumentPreview:
    """Return deterministic preview bytes plus metadata for one source document."""
    normalized_type = (file_type or Path(filename).suffix.lstrip(".")).lower()
    normalized_mime = (mime_type or "").lower()
    metadata = _base_metadata(
        filename=filename,
        file_type=normalized_type,
        mime_type=normalized_mime,
        source_type=source_type,
        file_hash=file_hash,
    )

    if normalized_type == "pdf" or normalized_mime == PDF_MIME_TYPE:
        return SourceDocumentPreview(
            preview_mime_type=PDF_MIME_TYPE,
            preview_bytes=original_bytes,
            preview_metadata={
                **metadata,
                "preview_kind": "pdf",
                "page_model": "original_pdf_pages",
            },
        )
    if normalized_type == "xlsx" or normalized_mime == XLSX_MIME_TYPE:
        return _build_xlsx_preview(
            original_bytes=original_bytes,
            filename=filename,
            metadata=metadata,
        )
    if normalized_type == "xml" or normalized_mime in XML_MIME_TYPES:
        return _build_xml_transcript_preview(
            original_bytes=original_bytes,
            filename=filename,
            metadata=metadata,
        )

    raise ValueError(
        f"Unsupported source document preview type: {file_type or mime_type or filename}"
    )


def is_preview_current(
    *,
    stored_file_hash: str,
    expected_file_hash: str,
    preview_mime_type: str | None,
    has_preview_bytes: bool,
    preview_metadata: Mapping[str, Any] | None,
    preview_error: str | None,
) -> bool:
    """Return whether a stored preview matches the current renderer contract."""
    if str(stored_file_hash) != str(expected_file_hash):
        return False
    if preview_error:
        return False
    if preview_mime_type != PDF_MIME_TYPE or not has_preview_bytes:
        return False
    return preview_metadata_is_current(preview_metadata)


def preview_metadata_is_current(
    preview_metadata: Mapping[str, Any] | None,
) -> bool:
    """Return whether metadata was produced by the current preview renderer."""
    return bool(
        preview_metadata
        and preview_metadata.get("renderer_version") == PREVIEW_RENDERER_VERSION
    )


def preview_error_metadata(
    *,
    filename: str,
    file_type: str,
    mime_type: str,
    source_type: str = "",
    file_hash: str = "",
    error: BaseException,
) -> dict[str, Any]:
    """Return metadata for a failed preview generation attempt."""
    return {
        **_base_metadata(
            filename=filename,
            file_type=file_type,
            mime_type=mime_type,
            source_type=source_type,
            file_hash=file_hash,
        ),
        "preview_kind": "error",
        "error_type": type(error).__name__,
    }


def _base_metadata(
    *,
    filename: str,
    file_type: str,
    mime_type: str,
    source_type: str,
    file_hash: str,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "renderer_version": PREVIEW_RENDERER_VERSION,
        "filename": filename,
        "source_file_type": file_type,
        "source_mime_type": mime_type,
    }
    if source_type:
        metadata["source_type"] = source_type
    if file_hash:
        metadata["source_file_hash"] = file_hash
    return metadata


def _build_xlsx_preview(
    *,
    original_bytes: bytes,
    filename: str,
    metadata: dict[str, Any],
) -> SourceDocumentPreview:
    workbook = _load_preview_workbook(original_bytes)
    workbook_sheet_count = len(workbook.sheetnames)
    visible_sheets = _visible_workbook_sheets(workbook)
    try:
        if not visible_sheets:
            preview_bytes = _message_pdf(
                title=filename,
                lines=["Workbook has no visible sheets."],
            )
            sheet_records: list[dict[str, Any]] = []
        else:
            preview_bytes, sheet_records = _render_workbook_sheets_with_libreoffice(
                workbook=workbook,
                filename=filename,
                visible_sheets=visible_sheets,
            )
    finally:
        workbook.close()

    return SourceDocumentPreview(
        preview_mime_type=PDF_MIME_TYPE,
        preview_bytes=preview_bytes,
        preview_metadata={
            **metadata,
            "preview_kind": "libreoffice_sheet_pdf",
            "page_model": "visible_sheet_pdf_page_ranges",
            "render_engine": "libreoffice",
            "sheets": sheet_records,
            "visible_sheet_count": len(visible_sheets),
            "workbook_sheet_count": workbook_sheet_count,
        },
    )


def _load_preview_workbook(original_bytes: bytes) -> Any:
    """Load an XLSX workbook while suppressing known metadata warnings."""
    from openpyxl import load_workbook

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="Print area cannot be set to Defined name:*",
            category=UserWarning,
            module="openpyxl.reader.workbook",
        )
        return load_workbook(
            io.BytesIO(original_bytes),
            data_only=False,
            read_only=False,
        )


def _visible_workbook_sheets(workbook: Any) -> list[_WorkbookSheet]:
    """Return visible sheets in workbook order, matching extraction numbering."""
    visible_sheets: list[_WorkbookSheet] = []
    for sheet_number, sheet_name in enumerate(workbook.sheetnames, start=1):
        sheet = workbook[sheet_name]
        sheet_state = str(getattr(sheet, "sheet_state", "visible"))
        if sheet_state != "visible":
            continue
        visible_sheets.append(
            _WorkbookSheet(
                sheet_number=sheet_number,
                name=str(sheet.title),
                is_chartsheet=not hasattr(sheet, "iter_rows"),
                sheet_state=sheet_state,
            )
        )
    return visible_sheets


def _render_workbook_sheets_with_libreoffice(
    *,
    workbook: Any,
    filename: str,
    visible_sheets: list[_WorkbookSheet],
) -> tuple[bytes, list[dict[str, Any]]]:
    """Render each visible sheet through LibreOffice and merge page ranges."""
    soffice = _resolve_soffice()
    pdf_parts: list[tuple[_WorkbookSheet, Path]] = []
    suffix = Path(filename).suffix or ".xlsx"
    with tempfile.TemporaryDirectory(prefix="aegis-xlsx-preview-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        output_dir = temp_dir / "pdf"
        output_dir.mkdir()
        profile_dir = temp_dir / "lo-profile"
        profile_dir.mkdir()

        for sheet in visible_sheets:
            workbook_path = temp_dir / f"sheet_{sheet.sheet_number:03d}{suffix}"
            _save_single_visible_sheet_workbook(workbook, sheet, workbook_path)
            pdf_path = _convert_xlsx_file_to_pdf(
                soffice=soffice,
                input_path=workbook_path,
                output_dir=output_dir,
                profile_dir=profile_dir,
            )
            pdf_parts.append((sheet, pdf_path))

        return _merge_sheet_pdfs(pdf_parts)


def _save_single_visible_sheet_workbook(
    workbook: Any,
    target_sheet: _WorkbookSheet,
    output_path: Path,
) -> None:
    """Save a temporary workbook with exactly one visible sheet."""
    for sheet_name in workbook.sheetnames:
        sheet = workbook[sheet_name]
        sheet.sheet_state = "visible" if sheet_name == target_sheet.name else "hidden"
    _set_active_sheet(workbook, target_sheet.name)
    workbook.save(output_path)


def _set_active_sheet(workbook: Any, sheet_name: str) -> None:
    """Best-effort active sheet selection for LibreOffice exports."""
    try:
        workbook.active = workbook.sheetnames.index(sheet_name)
    except Exception:
        return


def _convert_xlsx_file_to_pdf(
    *,
    soffice: str,
    input_path: Path,
    output_dir: Path,
    profile_dir: Path,
) -> Path:
    """Convert one temporary XLSX file to PDF with LibreOffice."""
    command = [
        soffice,
        "--headless",
        "--nologo",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile_dir.as_uri()}",
        "--convert-to",
        "pdf:calc_pdf_Export",
        "--outdir",
        str(output_dir),
        str(input_path),
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_SOFFICE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"LibreOffice PDF conversion timed out for {input_path.name}."
        ) from exc

    output_path = output_dir / input_path.with_suffix(".pdf").name
    if result.returncode != 0 or not output_path.is_file():
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        raise RuntimeError(
            "LibreOffice failed to convert XLSX preview sheet "
            f"{input_path.name}: {detail}"
        )
    return output_path


def _resolve_soffice() -> str:
    """Return a LibreOffice executable path for XLSX preview rendering."""
    for candidate in _SOFFICE_CANDIDATES:
        resolved = shutil.which(candidate) if "/" not in candidate else candidate
        if resolved and Path(resolved).is_file():
            return resolved
    raise RuntimeError(
        "XLSX preview generation requires LibreOffice/soffice. Install "
        "LibreOffice or add soffice to PATH before running preview backfill."
    )


def _merge_sheet_pdfs(
    pdf_parts: list[tuple[_WorkbookSheet, Path]],
) -> tuple[bytes, list[dict[str, Any]]]:
    """Merge rendered sheet PDFs and return sheet-to-page metadata."""
    PdfReader, PdfWriter = _load_pypdf_classes()
    writer = PdfWriter()
    sheet_records: list[dict[str, Any]] = []
    merged_page_count = 0
    for sheet, pdf_path in pdf_parts:
        reader = PdfReader(str(pdf_path))
        page_count = len(reader.pages)
        if page_count < 1:
            raise RuntimeError(f"LibreOffice produced empty PDF: {pdf_path}")
        start_page = merged_page_count + 1
        for page in reader.pages:
            writer.add_page(page)
        merged_page_count += page_count
        end_page = merged_page_count
        sheet_records.append(
            {
                "name": sheet.name,
                "sheet_number": sheet.sheet_number,
                "preview_page": start_page,
                "preview_start_page": start_page,
                "preview_end_page": end_page,
                "preview_page_count": end_page - start_page + 1,
                "is_chartsheet": sheet.is_chartsheet,
                "sheet_state": sheet.sheet_state,
            }
        )
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue(), sheet_records


def _load_pypdf_classes() -> tuple[Any, Any]:
    try:
        from pypdf import PdfReader, PdfWriter

        return PdfReader, PdfWriter
    except ImportError as exc:
        raise RuntimeError(
            "PDF preview merging requires pypdf. Install requirements-workstation.txt."
        ) from exc


def _build_xml_transcript_preview(
    *,
    original_bytes: bytes,
    filename: str,
    metadata: dict[str, Any],
) -> SourceDocumentPreview:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    document = _parse_factset_transcript(original_bytes)
    units = _build_transcript_units(document)
    if not units:
        raise ValueError("Transcript XML produced no preview units.")

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, pageCompression=1)
    pdf.setTitle(f"{filename} preview")
    unit_metadata: list[dict[str, Any]] = []
    next_page_number = 1
    for unit in units:
        start_page = next_page_number
        next_page_number = _draw_transcript_unit_pages(
            pdf,
            page_size=letter,
            filename=filename,
            document=document,
            unit=unit,
            start_page=start_page,
        )
        end_page = next_page_number - 1
        unit_metadata.append(
            {
                "unit_number": unit.unit_number,
                "preview_page": start_page,
                "preview_start_page": start_page,
                "preview_end_page": end_page,
                "preview_page_count": end_page - start_page + 1,
                "title": unit.title,
                "unit_type": unit.unit_type,
                "section_name": unit.section_name,
                "speaker_block_ids": unit.speaker_block_ids,
                "qa_group_id": unit.qa_group_id,
            }
        )
    pdf.save()
    return SourceDocumentPreview(
        preview_mime_type=PDF_MIME_TYPE,
        preview_bytes=buffer.getvalue(),
        preview_metadata={
            **metadata,
            "preview_kind": "styled_transcript_pdf",
            "page_model": "transcript_unit_pdf_page_ranges",
            "transcript_title": document.title,
            "transcript_date": document.transcript_date,
            "unit_count": len(units),
            "preview_page_count": next_page_number - 1,
            "units": unit_metadata,
        },
    )


def _message_pdf(*, title: str, lines: list[str]) -> bytes:
    """Return a simple one-page PDF message."""
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter, pageCompression=1)
    _draw_message_page(
        pdf,
        page_size=letter,
        title=title,
        lines=lines,
    )
    pdf.save()
    return buffer.getvalue()


def _draw_transcript_unit_pages(
    pdf: Any,
    *,
    page_size: tuple[float, float],
    filename: str,
    document: _TranscriptDocument,
    unit: _TranscriptUnit,
    start_page: int,
) -> int:
    """Draw a full transcript unit and return the next available page number."""
    width, height = page_size
    page_number = start_page
    y = _draw_transcript_page_header(
        pdf,
        page_size=page_size,
        filename=filename,
        document=document,
        unit=unit,
        page_number=page_number,
        continued=False,
    )
    max_chars = 96
    for kind, text in _markdown_blocks(unit.markdown):
        spacing_before, spacing_after, line_height = _text_style_spacing(kind)
        if kind == "blank":
            y -= spacing_after
            continue
        if y < _PAGE_MARGIN + spacing_before + line_height:
            _draw_footer(pdf, width, page_number)
            pdf.showPage()
            page_number += 1
            y = _draw_transcript_page_header(
                pdf,
                page_size=page_size,
                filename=filename,
                document=document,
                unit=unit,
                page_number=page_number,
                continued=True,
            )
        y -= spacing_before
        for line in textwrap.wrap(text, width=max_chars) or [""]:
            if y < _PAGE_MARGIN + line_height:
                _draw_footer(pdf, width, page_number)
                pdf.showPage()
                page_number += 1
                y = _draw_transcript_page_header(
                    pdf,
                    page_size=page_size,
                    filename=filename,
                    document=document,
                    unit=unit,
                    page_number=page_number,
                    continued=True,
                )
            _draw_text_line(pdf, kind, line, _PAGE_MARGIN, y)
            y -= line_height
        y -= spacing_after

    _draw_footer(pdf, width, page_number)
    pdf.showPage()
    return page_number + 1


def _draw_transcript_page_header(
    pdf: Any,
    *,
    page_size: tuple[float, float],
    filename: str,
    document: _TranscriptDocument,
    unit: _TranscriptUnit,
    page_number: int,
    continued: bool,
) -> float:
    width, height = page_size
    subtitle_parts = [filename]
    if document.transcript_date:
        subtitle_parts.append(document.transcript_date)
    subtitle_parts.append(f"unit {unit.unit_number}")
    subtitle_parts.append(f"preview page {page_number}")
    title = unit.title + (" (continued)" if continued else "")
    pdf.setFillColorRGB(1, 1, 1)
    pdf.rect(0, 0, width, height, fill=1, stroke=0)
    return _draw_page_title(pdf, width, height, title, " | ".join(subtitle_parts))


def _markdown_blocks(markdown: str) -> list[tuple[str, str]]:
    """Return styled transcript blocks without markdown control characters."""
    blocks: list[tuple[str, str]] = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            blocks.append(("blank", ""))
            continue
        if line.startswith("## "):
            blocks.append(("h2", _clean_text(line[3:])))
            continue
        if line.startswith("# "):
            blocks.append(("h1", _clean_text(line[2:])))
            continue
        if _is_transcript_metadata_line(line):
            blocks.append(("meta", _clean_text(line)))
            continue
        blocks.append(("body", _clean_text(line)))
    return blocks


def _is_transcript_metadata_line(line: str) -> bool:
    prefixes = (
        "Section:",
        "Speaker:",
        "Speaker block ID:",
        "Q&A group ID:",
        "Speaker block IDs:",
    )
    return line.startswith(prefixes)


def _text_style_spacing(kind: str) -> tuple[float, float, float]:
    if kind == "h1":
        return 2, 7, 15
    if kind == "h2":
        return 8, 5, 12.5
    if kind == "meta":
        return 1, 2, 10
    return 2, 6, 11.5


def _draw_text_line(pdf: Any, kind: str, text: str, x: float, y: float) -> None:
    if kind == "h1":
        pdf.setFillColorRGB(0.1, 0.13, 0.18)
        pdf.setFont("Helvetica-Bold", 13)
    elif kind == "h2":
        pdf.setFillColorRGB(0.12, 0.22, 0.32)
        pdf.setFont("Helvetica-Bold", 10.5)
    elif kind == "meta":
        pdf.setFillColorRGB(0.36, 0.42, 0.5)
        pdf.setFont("Helvetica", 8.5)
    else:
        pdf.setFillColorRGB(0.1, 0.13, 0.18)
        pdf.setFont("Helvetica", 9)
    pdf.drawString(x, y, _pdf_text(text))


def _draw_message_page(
    pdf: Any,
    *,
    page_size: tuple[float, float],
    title: str,
    lines: list[str],
) -> None:
    width, height = page_size
    y = _draw_page_title(pdf, width, height, title, "Aegis source document preview")
    _draw_text_lines(pdf, lines, x=_PAGE_MARGIN, y=y, line_height=12)
    _draw_footer(pdf, width, 1)
    pdf.showPage()


def _draw_page_title(
    pdf: Any,
    width: float,
    height: float,
    title: str,
    subtitle: str,
) -> float:
    pdf.setFillColorRGB(0.1, 0.13, 0.18)
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(_PAGE_MARGIN, height - _PAGE_MARGIN, _pdf_text(_clip(title, 96)))
    pdf.setFillColorRGB(0.35, 0.4, 0.48)
    pdf.setFont("Helvetica", 8.5)
    pdf.drawString(
        _PAGE_MARGIN,
        height - _PAGE_MARGIN - 16,
        _pdf_text(_clip(subtitle, 140)),
    )
    pdf.setStrokeColorRGB(0.78, 0.82, 0.87)
    pdf.line(_PAGE_MARGIN, height - _PAGE_MARGIN - 27, width - _PAGE_MARGIN, height - _PAGE_MARGIN - 27)
    return height - _PAGE_MARGIN - 42


def _draw_text_lines(
    pdf: Any,
    lines: list[str],
    *,
    x: float,
    y: float,
    line_height: float,
) -> None:
    pdf.setFillColorRGB(0.1, 0.13, 0.18)
    pdf.setFont("Helvetica", 8.7)
    for line in lines:
        if y < 34:
            break
        pdf.drawString(x, y, _pdf_text(line))
        y -= line_height


def _draw_footer(pdf: Any, width: float, page_number: int) -> None:
    pdf.setFillColorRGB(0.45, 0.5, 0.57)
    pdf.setFont("Helvetica", 8)
    pdf.drawRightString(width - _PAGE_MARGIN, 24, f"Preview page {page_number}")


def _parse_factset_transcript(original_bytes: bytes) -> _TranscriptDocument:
    try:
        root = ET.fromstring(original_bytes)
    except ET.ParseError as exc:
        raise ValueError(f"Invalid XML transcript: {exc}") from exc

    namespace = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    meta = _find(root, namespace, "meta")
    if meta is None:
        raise ValueError("XML transcript meta section is missing.")
    body = _find(root, namespace, "body")
    if body is None:
        raise ValueError("XML transcript body section is missing.")

    title = _element_text(_find(meta, namespace, "title"))
    transcript_date = _element_text(_find(meta, namespace, "date"))
    participants = _parse_participants(meta, namespace)
    companies = _parse_companies(meta, namespace) or _companies_from_participants(
        participants
    )
    blocks = _parse_speaker_blocks(body, namespace, participants)
    if not blocks:
        raise ValueError("No transcript speaker blocks found.")
    return _TranscriptDocument(
        title=title,
        transcript_date=transcript_date,
        participants=participants,
        companies=companies,
        blocks=blocks,
    )


def _build_transcript_units(document: _TranscriptDocument) -> list[_TranscriptUnit]:
    units: list[_TranscriptUnit] = []
    unit_number = 1

    for block in document.blocks:
        if block.section_name != _SECTION_MD:
            continue
        units.append(
            _TranscriptUnit(
                unit_number=unit_number,
                unit_id=f"md_block_{block.speaker_block_id}",
                unit_type="management_discussion",
                title=f"Management Discussion: {block.speaker_name}",
                markdown=_markdown_for_md_block(block),
                section_name=block.section_name,
                speaker_block_ids=[block.speaker_block_id],
            )
        )
        unit_number += 1

    for qa_group_id, qa_blocks in enumerate(_qa_groups(document.blocks), start=1):
        units.append(
            _TranscriptUnit(
                unit_number=unit_number,
                unit_id=f"qa_group_{qa_group_id}",
                unit_type="qa_exchange",
                title=f"Q&A Exchange {qa_group_id}: {_qa_title_speaker(qa_blocks)}",
                markdown=_markdown_for_qa_group(qa_group_id, qa_blocks),
                section_name=_SECTION_QA,
                speaker_block_ids=[block.speaker_block_id for block in qa_blocks],
                qa_group_id=qa_group_id,
            )
        )
        unit_number += 1
    return units


def _parse_companies(meta: ET.Element, namespace: str) -> list[str]:
    companies = []
    companies_el = _find(meta, namespace, "companies")
    if companies_el is not None:
        for company in _findall(companies_el, namespace, "company"):
            company_text = _element_text(company)
            if company_text:
                companies.append(company_text)
    return companies


def _companies_from_participants(
    participants: dict[str, _TranscriptParticipant],
) -> list[str]:
    company_side = [
        participant
        for participant in participants.values()
        if participant.participant_type.lower() in {"executive", "moderator"}
    ]
    companies: list[str] = []
    seen: set[str] = set()
    for participant in company_side or list(participants.values()):
        for value in (participant.affiliation, participant.affiliation_entity):
            company = _clean_text(value)
            key = company.casefold()
            if not company or key in seen:
                continue
            seen.add(key)
            companies.append(company)
    return companies


def _parse_participants(
    meta: ET.Element,
    namespace: str,
) -> dict[str, _TranscriptParticipant]:
    participants: dict[str, _TranscriptParticipant] = {}
    participants_el = _find(meta, namespace, "participants")
    if participants_el is None:
        return participants

    for participant_el in _findall(participants_el, namespace, "participant"):
        participant_id = participant_el.get("id", "").strip()
        if not participant_id:
            continue
        participants[participant_id] = _TranscriptParticipant(
            participant_id=participant_id,
            name=_clean_text(
                participant_el.get("name", "")
                or _element_text(participant_el)
                or "Unknown Speaker"
            ),
            participant_type=participant_el.get("type", "").strip(),
            title=_clean_text(participant_el.get("title", "")),
            affiliation=_clean_text(participant_el.get("affiliation", "")),
            affiliation_entity=_clean_text(
                participant_el.get("affiliation_entity", "")
            ),
        )
    return participants


def _parse_speaker_blocks(
    body: ET.Element,
    namespace: str,
    participants: dict[str, _TranscriptParticipant],
) -> list[_TranscriptSpeakerBlock]:
    blocks: list[_TranscriptSpeakerBlock] = []
    speaker_block_id = 1
    for section_el in _findall(body, namespace, "section"):
        raw_section_name = section_el.get("name", "").strip()
        section_name = _normalize_section_name(raw_section_name)
        for speaker_el in _findall(section_el, namespace, "speaker"):
            participant_id = speaker_el.get("id", "").strip()
            paragraphs = _speaker_paragraphs(speaker_el, namespace)
            if not paragraphs:
                continue
            participant = participants.get(
                participant_id,
                _TranscriptParticipant(
                    participant_id=participant_id,
                    name="Unknown Speaker",
                ),
            )
            blocks.append(
                _TranscriptSpeakerBlock(
                    speaker_block_id=speaker_block_id,
                    section_name=section_name,
                    raw_section_name=raw_section_name,
                    participant_id=participant_id,
                    speaker_type_hint=speaker_el.get("type", "").strip().lower(),
                    participant=participant,
                    paragraphs=paragraphs,
                )
            )
            speaker_block_id += 1
    return blocks


def _speaker_paragraphs(speaker_el: ET.Element, namespace: str) -> list[str]:
    paragraphs = []
    plist = _find(speaker_el, namespace, "plist")
    paragraph_parent = plist if plist is not None else speaker_el
    for paragraph_el in _findall(paragraph_parent, namespace, "p"):
        paragraph = _element_text(paragraph_el)
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def _qa_groups(
    blocks: list[_TranscriptSpeakerBlock],
) -> list[list[_TranscriptSpeakerBlock]]:
    qa_blocks = [block for block in blocks if block.section_name == _SECTION_QA]
    groups: list[list[_TranscriptSpeakerBlock]] = []
    current_group: list[_TranscriptSpeakerBlock] = []
    for block in qa_blocks:
        starts_question = block.speaker_type_hint == "q"
        if starts_question and current_group:
            groups.append(current_group)
            current_group = [block]
            continue
        current_group.append(block)
    if current_group:
        groups.append(current_group)
    return groups


def _markdown_for_md_block(block: _TranscriptSpeakerBlock) -> str:
    lines = [
        f"# Management Discussion: {block.speaker_name}",
        f"Section: {block.section_name}",
        f"Speaker: {block.participant.display_name}",
        f"Speaker block ID: {block.speaker_block_id}",
        "",
    ]
    lines.extend(block.paragraphs)
    return "\n\n".join(lines).strip()


def _markdown_for_qa_group(
    qa_group_id: int,
    blocks: list[_TranscriptSpeakerBlock],
) -> str:
    lines = [
        f"# Q&A Exchange {qa_group_id}: {_qa_title_speaker(blocks)}",
        f"Section: {_SECTION_QA}",
        f"Q&A group ID: {qa_group_id}",
        f"Speaker block IDs: {', '.join(str(block.speaker_block_id) for block in blocks)}",
        "",
    ]
    for block in blocks:
        role = _speaker_role(block)
        lines.append(f"## {role}: {block.participant.display_name}")
        lines.extend(block.paragraphs)
        lines.append("")
    return "\n\n".join(line for line in lines if line is not None).strip()


def _speaker_role(block: _TranscriptSpeakerBlock) -> str:
    if block.speaker_type_hint == "q":
        return "Question"
    if block.speaker_type_hint == "a":
        return "Answer"
    participant_type = block.participant.participant_type.casefold()
    if participant_type == "analyst":
        return "Question"
    if participant_type in {"executive", "moderator"}:
        return "Answer"
    return "Speaker"


def _qa_title_speaker(blocks: list[_TranscriptSpeakerBlock]) -> str:
    question_block = next(
        (
            block
            for block in blocks
            if block.speaker_type_hint == "q"
            or block.participant.participant_type.casefold() == "analyst"
        ),
        None,
    )
    return (question_block or blocks[0]).speaker_name if blocks else "Unknown Speaker"


def _normalize_section_name(section_name: str) -> str:
    normalized = section_name.casefold()
    if "management" in normalized and "discussion" in normalized:
        return _SECTION_MD
    if "question" in normalized or "q&a" in normalized:
        return _SECTION_QA
    return section_name.strip() or "Transcript"


def _find(parent: ET.Element, namespace: str, tag: str) -> ET.Element | None:
    return parent.find(f"{namespace}{tag}")


def _findall(parent: ET.Element, namespace: str, tag: str) -> list[ET.Element]:
    return list(parent.findall(f"{namespace}{tag}"))


def _element_text(element: ET.Element | None) -> str:
    if element is None:
        return ""
    return _clean_text(" ".join(element.itertext()))


def _clean_text(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text or "").strip()


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _pdf_text(text: str) -> str:
    return str(text).encode("latin-1", errors="replace").decode("latin-1")
