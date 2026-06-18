"""Generate browser-preview bytes for persisted source documents."""

from __future__ import annotations

import io
import re
import textwrap
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PDF_MIME_TYPE = "application/pdf"
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XML_MIME_TYPES = {"application/xml", "text/xml"}
PREVIEW_RENDERER_VERSION = "source_document_preview_v1"

_SECTION_MD = "MANAGEMENT DISCUSSION SECTION"
_SECTION_QA = "Q&A"
_WHITESPACE_RE = re.compile(r"\s+")
_XLSX_MAX_ROWS = 38
_XLSX_MAX_COLUMNS = 9
_CELL_MAX_CHARS = 24
_PAGE_MARGIN = 42


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
    from openpyxl import load_workbook
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.pdfgen import canvas

    workbook = load_workbook(
        io.BytesIO(original_bytes),
        data_only=True,
        read_only=True,
    )
    workbook_sheet_count = len(workbook.sheetnames)
    page_size = landscape(letter)
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=page_size, pageCompression=1)
    pdf.setTitle(f"{filename} preview")

    sheets: list[dict[str, Any]] = []
    preview_page = 1
    try:
        for sheet_number, sheet_name in enumerate(workbook.sheetnames, start=1):
            worksheet = workbook[sheet_name]
            if getattr(worksheet, "sheet_state", "visible") != "visible":
                continue
            is_chartsheet = not hasattr(worksheet, "iter_rows")
            rows, row_count, column_count = _worksheet_preview_rows(worksheet)
            truncated_rows = row_count > _XLSX_MAX_ROWS
            truncated_columns = column_count > _XLSX_MAX_COLUMNS
            _draw_sheet_page(
                pdf,
                page_size=page_size,
                filename=filename,
                sheet_name=str(worksheet.title),
                sheet_number=sheet_number,
                preview_page=preview_page,
                rows=rows,
                row_count=row_count,
                column_count=column_count,
                truncated_rows=truncated_rows,
                truncated_columns=truncated_columns,
            )
            sheets.append(
                {
                    "name": str(worksheet.title),
                    "sheet_number": sheet_number,
                    "preview_page": preview_page,
                    "is_chartsheet": is_chartsheet,
                    "row_count": row_count,
                    "column_count": column_count,
                    "truncated_rows": truncated_rows,
                    "truncated_columns": truncated_columns,
                }
            )
            preview_page += 1

        if not sheets:
            _draw_message_page(
                pdf,
                page_size=page_size,
                title=filename,
                lines=["Workbook has no visible sheets."],
            )
    finally:
        workbook.close()

    pdf.save()
    return SourceDocumentPreview(
        preview_mime_type=PDF_MIME_TYPE,
        preview_bytes=buffer.getvalue(),
        preview_metadata={
            **metadata,
            "preview_kind": "sheet_pdf",
            "page_model": "one_visible_sheet_per_preview_page",
            "sheets": sheets,
            "visible_sheet_count": len(sheets),
            "workbook_sheet_count": workbook_sheet_count,
        },
    )


def _worksheet_preview_rows(worksheet: Any) -> tuple[list[list[str]], int, int]:
    if not hasattr(worksheet, "iter_rows"):
        return [], 0, 0
    row_count = int(getattr(worksheet, "max_row", 0) or 0)
    column_count = int(getattr(worksheet, "max_column", 0) or 0)
    max_rows = min(row_count, _XLSX_MAX_ROWS)
    max_columns = min(column_count, _XLSX_MAX_COLUMNS)
    rows: list[list[str]] = []
    if max_rows <= 0 or max_columns <= 0:
        return rows, row_count, column_count
    for row in worksheet.iter_rows(
        min_row=1,
        max_row=max_rows,
        min_col=1,
        max_col=max_columns,
        values_only=True,
    ):
        rows.append([_cell_text(value) for value in row])
    return rows, row_count, column_count


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        text = f"{value:.6g}"
    else:
        text = str(value)
    return _clip(_clean_text(text), _CELL_MAX_CHARS)


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
    for unit in units:
        truncated = _draw_transcript_page(
            pdf,
            page_size=letter,
            filename=filename,
            document=document,
            unit=unit,
        )
        unit_metadata.append(
            {
                "unit_number": unit.unit_number,
                "preview_page": unit.unit_number,
                "title": unit.title,
                "unit_type": unit.unit_type,
                "section_name": unit.section_name,
                "speaker_block_ids": unit.speaker_block_ids,
                "qa_group_id": unit.qa_group_id,
                "truncated": truncated,
            }
        )
    pdf.save()
    return SourceDocumentPreview(
        preview_mime_type=PDF_MIME_TYPE,
        preview_bytes=buffer.getvalue(),
        preview_metadata={
            **metadata,
            "preview_kind": "transcript_pdf",
            "page_model": "one_transcript_unit_per_preview_page",
            "transcript_title": document.title,
            "transcript_date": document.transcript_date,
            "unit_count": len(units),
            "units": unit_metadata,
        },
    )


def _draw_sheet_page(
    pdf: Any,
    *,
    page_size: tuple[float, float],
    filename: str,
    sheet_name: str,
    sheet_number: int,
    preview_page: int,
    rows: list[list[str]],
    row_count: int,
    column_count: int,
    truncated_rows: bool,
    truncated_columns: bool,
) -> None:
    width, height = page_size
    title = f"{sheet_name}"
    subtitle = (
        f"{filename} | sheet {sheet_number} | preview page {preview_page} | "
        f"{row_count} rows x {column_count} columns"
    )
    pdf.setFillColorRGB(1, 1, 1)
    pdf.rect(0, 0, width, height, fill=1, stroke=0)
    y = _draw_page_title(pdf, width, height, title, subtitle)
    lines = _sheet_lines(rows)
    if truncated_rows or truncated_columns:
        lines.append("")
        lines.append(
            "Preview truncated to keep each workbook sheet on one reference page."
        )
    _draw_monospace_lines(pdf, lines, x=_PAGE_MARGIN, y=y, width=width)
    _draw_footer(pdf, width, preview_page)
    pdf.showPage()


def _sheet_lines(rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["Sheet contains no cell values in the detected used range."]
    column_count = max((len(row) for row in rows), default=0)
    header = ["row", *(_excel_column_name(index) for index in range(1, column_count + 1))]
    lines = [_format_sheet_row(header)]
    for row_index, row in enumerate(rows, start=1):
        lines.append(_format_sheet_row([str(row_index), *row]))
    return lines


def _format_sheet_row(cells: list[str]) -> str:
    widths = [5, *([_CELL_MAX_CHARS] * max(0, len(cells) - 1))]
    padded = [
        _clip(cell, widths[index]).ljust(widths[index])
        for index, cell in enumerate(cells)
    ]
    return " | ".join(padded).rstrip()


def _draw_transcript_page(
    pdf: Any,
    *,
    page_size: tuple[float, float],
    filename: str,
    document: _TranscriptDocument,
    unit: _TranscriptUnit,
) -> bool:
    width, height = page_size
    subtitle_parts = [filename]
    if document.transcript_date:
        subtitle_parts.append(document.transcript_date)
    subtitle_parts.append(f"unit/page {unit.unit_number}")
    pdf.setFillColorRGB(1, 1, 1)
    pdf.rect(0, 0, width, height, fill=1, stroke=0)
    y = _draw_page_title(
        pdf,
        width,
        height,
        unit.title,
        " | ".join(subtitle_parts),
    )
    lines = _wrapped_markdown_lines(unit.markdown, max_chars=96)
    line_height = 10.8
    max_lines = max(1, int((y - 52) // line_height))
    truncated = len(lines) > max_lines
    visible_lines = lines[:max_lines]
    if truncated and visible_lines:
        visible_lines[-1] = "Preview text truncated to keep one transcript unit per page."
    _draw_text_lines(pdf, visible_lines, x=_PAGE_MARGIN, y=y, line_height=line_height)
    _draw_footer(pdf, width, unit.unit_number)
    pdf.showPage()
    return truncated


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


def _draw_monospace_lines(
    pdf: Any,
    lines: list[str],
    *,
    x: float,
    y: float,
    width: float,
) -> None:
    pdf.setFillColorRGB(0.1, 0.13, 0.18)
    pdf.setFont("Courier", 7.1)
    line_height = 9
    max_chars = max(20, int((width - (2 * _PAGE_MARGIN)) / 4.35))
    for line in lines:
        if y < 34:
            break
        pdf.drawString(x, y, _pdf_text(_clip(line, max_chars)))
        y -= line_height


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


def _wrapped_markdown_lines(text: str, *, max_chars: int) -> list[str]:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_text(raw_line)
        if not line:
            lines.append("")
            continue
        lines.extend(textwrap.wrap(line, width=max_chars) or [""])
    return lines


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


def _excel_column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def _clip(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)] + "..."


def _pdf_text(text: str) -> str:
    return str(text).encode("latin-1", errors="replace").decode("latin-1")
