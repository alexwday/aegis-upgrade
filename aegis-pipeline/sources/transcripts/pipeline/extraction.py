"""FactSet XML extraction stage for earnings-call transcripts."""

from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from utils.config_setup import get_input_source_config, load_config
from utils.logging_setup import get_stage_logger
from utils.qa import finding_records, qa_counts, qa_status
from .manifest import (
    ARTIFACTS_DIR_NAME,
    FILES_TO_PROCESS_FILE_NAME,
    PROGRESS_DIR,
    ManifestRecord,
)

DOCUMENT_ARTIFACT_FILE_NAME = "document.json"
WORKBOOK_ARTIFACT_FILE_NAME = DOCUMENT_ARTIFACT_FILE_NAME
EXTRACTION_DIR_NAME = "extraction"
EXTRACTION_MANIFEST_FILE_NAME = "extraction_manifest.json"
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

SECTION_MD = "MANAGEMENT DISCUSSION SECTION"
SECTION_QA = "Q&A"
WHITESPACE_RE = re.compile(r"\s+")
TRANSCRIPT_FILENAME_PATTERN = re.compile(
    r"^(?P<ticker>.+?)_(?P<quarter>Q[1-4])_(?P<fiscal_year>\d{4})_"
    r"(?P<transcript_type>[^_]+)_(?P<event_id>[^_]+)_(?P<version_id>\d+)\.xml$",
    re.IGNORECASE,
)


class ExtractionStageError(RuntimeError):
    """Raised when the XML extraction stage cannot continue safely."""


@dataclass(frozen=True)
class TranscriptParticipant:
    """One participant declared in the FactSet transcript metadata."""

    participant_id: str
    name: str
    participant_type: str = ""
    title: str = ""
    affiliation: str = ""
    affiliation_entity: str = ""

    @property
    def display_name(self) -> str:
        """Return a readable speaker label with role context when present."""
        parts = [self.name or "Unknown Speaker"]
        if self.title and self.title not in parts:
            parts.append(self.title)
        if self.affiliation and self.affiliation not in parts:
            parts.append(self.affiliation)
        return ", ".join(parts)


@dataclass(frozen=True)
class TranscriptSpeakerBlock:
    """One consecutive speaker turn from the XML body."""

    speaker_block_id: int
    section_name: str
    raw_section_name: str
    participant_id: str
    speaker_type_hint: str
    participant: TranscriptParticipant
    paragraphs: list[str]

    @property
    def speaker_name(self) -> str:
        """Return the speaker's clean display name."""
        return self.participant.name or "Unknown Speaker"


@dataclass(frozen=True)
class TranscriptDocument:
    """Parsed FactSet transcript document."""

    title: str
    transcript_date: str
    participants: dict[str, TranscriptParticipant]
    blocks: list[TranscriptSpeakerBlock]
    companies: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractedTranscriptUnit:
    """A transcript unit exposed to downstream chunking as a page-like item."""

    unit_number: int
    unit_id: str
    unit_type: str
    title: str
    markdown: str
    section_name: str
    speaker_block_ids: list[int]
    qa_group_id: int | None = None
    speakers: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class ExtractedTranscriptDocument:
    """Extracted transcript units plus source-level metadata."""

    units: list[ExtractedTranscriptUnit]
    source_unit_count: int
    selected_unit_numbers: list[int]
    title: str
    transcript_date: str
    companies: list[str]


@dataclass(frozen=True)
class TranscriptDocumentArtifacts:
    """Paths and counts written for one extracted XML transcript."""

    artifact_root: Path
    document_json: Path
    document_markdown: Path
    unit_count: int
    source_unit_count: int
    selected_unit_numbers: tuple[int, ...]
    qa_report_json: Path
    qa_status: str


@dataclass(frozen=True)
class ExtractionStageResult:
    """Summary returned after writing transcript extraction artifacts."""

    processed_file_count: int
    document_artifact_paths: tuple[Path, ...]

    @property
    def workbook_artifact_paths(self) -> tuple[Path, ...]:
        """Compatibility alias used by shared pipeline callers."""
        return self.document_artifact_paths


def run_extraction_stage(
    input_base_path: Path | None = None,
    progress_dir: Path = PROGRESS_DIR,
    max_pages: int | None = None,
    page_numbers: list[int] | tuple[int, ...] | None = None,
    llm_client: Any | None = None,
) -> ExtractionStageResult:
    """Extract all pending FactSet XML transcripts selected by the manifest stage.

    Args:
        input_base_path: Optional local transcripts input folder override.
        progress_dir: Folder containing manifest progress files and artifacts.
        max_pages: Optional first-N transcript-unit limit for smoke runs.
        page_numbers: Optional one-indexed transcript unit numbers to emit.
        llm_client: Accepted for API compatibility; not used by XML extraction.

    Returns:
        ExtractionStageResult with the document artifact paths written.

    Raises:
        ExtractionStageError: If progress files are invalid, source XML files
            are missing, or a pending record is not XML.
        NotImplementedError: If configured input source is not local.
    """
    _ = llm_client
    load_config()
    logger = get_stage_logger(__name__, "EXTRACTION")
    input_base = _resolve_input_base_path(input_base_path)
    records = _load_process_records(progress_dir)
    processed_at = _utc_now()
    document_paths: list[Path] = []

    for index, record in enumerate(records, start=1):
        if record.file_type.lower() != "xml":
            raise ExtractionStageError(
                f"Extraction received non-xml file: {record.file_id}"
            )
        source_path = input_base / record.file_path
        if not source_path.is_file():
            raise ExtractionStageError(f"Source XML is missing: {source_path}")

        logger.info(
            "Extracting transcript XML %d/%d: %s",
            index,
            len(records),
            record.file_name,
        )
        file_started_at = _utc_now()
        file_start = time.perf_counter()
        document = extract_transcript_document(
            source_path,
            max_units=max_pages,
            unit_numbers=page_numbers,
        )
        duration_seconds = time.perf_counter() - file_start
        file_completed_at = _utc_now()
        artifacts = write_transcript_document_artifacts(
            record=record,
            source_path=source_path,
            document=document,
            extraction_root=_artifact_root(progress_dir, record.file_id)
            / EXTRACTION_DIR_NAME,
            processed_at=processed_at,
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


def extract_transcript_document(
    xml_path: Path,
    max_units: int | None = None,
    unit_numbers: list[int] | tuple[int, ...] | None = None,
) -> ExtractedTranscriptDocument:
    """Parse a FactSet XML file and return downstream-ready transcript units."""
    parsed = parse_factset_transcript(xml_path)
    units = build_transcript_units(parsed)
    selected_numbers = _selected_unit_numbers(len(units), max_units, unit_numbers)
    selected_units = [
        unit for unit in units if unit.unit_number in set(selected_numbers)
    ]
    return ExtractedTranscriptDocument(
        units=selected_units,
        source_unit_count=len(units),
        selected_unit_numbers=selected_numbers,
        title=parsed.title,
        transcript_date=parsed.transcript_date,
        companies=parsed.companies,
    )


def parse_factset_transcript(xml_path: Path) -> TranscriptDocument:
    """Parse a local FactSet transcript XML file."""
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError as exc:
        raise ExtractionStageError(f"Invalid XML in {xml_path}: {exc}") from exc

    namespace = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
    meta = _find(root, namespace, "meta")
    if meta is None:
        raise ExtractionStageError(f"XML meta section is missing: {xml_path}")
    body = _find(root, namespace, "body")
    if body is None:
        raise ExtractionStageError(f"XML body section is missing: {xml_path}")

    title = _element_text(_find(meta, namespace, "title"))
    transcript_date = _element_text(_find(meta, namespace, "date"))
    participants = _parse_participants(meta, namespace)
    companies = _parse_companies(meta, namespace) or _companies_from_participants(
        participants
    )
    blocks = _parse_speaker_blocks(body, namespace, participants)
    if not blocks:
        raise ExtractionStageError(f"No transcript speaker blocks found: {xml_path}")

    return TranscriptDocument(
        title=title,
        transcript_date=transcript_date,
        participants=participants,
        companies=companies,
        blocks=blocks,
    )


def build_transcript_units(
    document: TranscriptDocument,
) -> list[ExtractedTranscriptUnit]:
    """Build retrieval units from parsed speaker blocks."""
    units: list[ExtractedTranscriptUnit] = []
    unit_number = 1

    for block in document.blocks:
        if block.section_name != SECTION_MD:
            continue
        units.append(
            ExtractedTranscriptUnit(
                unit_number=unit_number,
                unit_id=f"md_block_{block.speaker_block_id}",
                unit_type="management_discussion",
                title=f"Management Discussion: {block.speaker_name}",
                markdown=_markdown_for_md_block(block),
                section_name=block.section_name,
                speaker_block_ids=[block.speaker_block_id],
                speakers=[_speaker_record(block)],
            )
        )
        unit_number += 1

    for qa_group_id, qa_blocks in enumerate(_qa_groups(document.blocks), start=1):
        title_speaker = _qa_title_speaker(qa_blocks)
        units.append(
            ExtractedTranscriptUnit(
                unit_number=unit_number,
                unit_id=f"qa_group_{qa_group_id}",
                unit_type="qa_exchange",
                title=f"Q&A Exchange {qa_group_id}: {title_speaker}",
                markdown=_markdown_for_qa_group(qa_group_id, qa_blocks),
                section_name=SECTION_QA,
                speaker_block_ids=[block.speaker_block_id for block in qa_blocks],
                qa_group_id=qa_group_id,
                speakers=[_speaker_record(block) for block in qa_blocks],
            )
        )
        unit_number += 1

    return units


def write_document_artifact(
    record: ManifestRecord,
    source_path: Path,
    document: ExtractedTranscriptDocument,
    extraction_root: Path,
    processed_at: str,
    *,
    file_started_at: str | None = None,
    file_completed_at: str | None = None,
    duration_seconds: float | None = None,
) -> Path:
    """Write one transcript document artifact set and return document.json."""
    artifacts = write_transcript_document_artifacts(
        record=record,
        source_path=source_path,
        document=document,
        extraction_root=extraction_root,
        processed_at=processed_at,
        file_started_at=file_started_at,
        file_completed_at=file_completed_at,
        duration_seconds=duration_seconds,
    )
    return artifacts.document_json


def write_transcript_document_artifacts(
    record: ManifestRecord,
    source_path: Path,
    document: ExtractedTranscriptDocument,
    extraction_root: Path,
    processed_at: str,
    *,
    file_started_at: str | None = None,
    file_completed_at: str | None = None,
    duration_seconds: float | None = None,
) -> TranscriptDocumentArtifacts:
    """Write inspectable JSON and markdown artifacts for one transcript XML."""
    extraction_root.mkdir(parents=True, exist_ok=True)
    source = _source_record(record, source_path)
    unit_records, document_markdown = _write_document_outputs(
        source=source,
        document=document,
        artifact_root=extraction_root,
        processed_at=processed_at,
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
        "extraction_type": "factset_transcript_xml",
        "processed_at": processed_at,
        "file_started_at": file_started_at or processed_at,
        "file_completed_at": file_completed_at or processed_at,
        "duration_seconds": round(float(duration_seconds or 0.0), 6),
        "source": source,
        "transcript_title": document.title,
        "transcript_date": document.transcript_date,
        "companies": document.companies,
        "page_count": len(document.units),
        "unit_count": len(document.units),
        "source_page_count": document.source_unit_count,
        "source_unit_count": document.source_unit_count,
        "selected_page_numbers": list(document.selected_unit_numbers),
        "selected_unit_numbers": list(document.selected_unit_numbers),
        "extracted_page_count": len(document.units),
        "extracted_unit_count": len(document.units),
        "visual_region_count": 0,
        "qa_status": qa_record["qa_status"],
        "qa_counts": qa_record["qa_counts"],
        "qa_report_path": str(qa_report_json),
        "document_markdown_path": str(document_markdown),
        "pages": unit_records,
        "units": unit_records,
    }
    _write_json(document_json, payload)
    _write_extraction_manifest(extraction_root, payload)
    return TranscriptDocumentArtifacts(
        artifact_root=extraction_root,
        document_json=document_json,
        document_markdown=document_markdown,
        unit_count=len(document.units),
        source_unit_count=document.source_unit_count,
        selected_unit_numbers=tuple(document.selected_unit_numbers),
        qa_report_json=qa_report_json,
        qa_status=str(qa_record["qa_status"]),
    )


def _write_document_outputs(
    source: dict[str, Any],
    document: ExtractedTranscriptDocument,
    artifact_root: Path,
    processed_at: str,
) -> tuple[list[dict[str, Any]], Path]:
    """Write unit-level outputs and the recombined transcript markdown."""
    unit_records = []
    document_parts = [
        f"# {document.title or source['file_name']}",
        f"Source file: {source['file_name']}",
    ]
    if document.transcript_date:
        document_parts.append(f"Transcript date: {document.transcript_date}")
    for unit in document.units:
        unit_records.append(
            _write_unit_artifacts(source, unit, artifact_root, processed_at)
        )
        document_parts.append(unit.markdown)

    document_markdown = artifact_root / "document.md"
    document_markdown.write_text(
        "\n\n".join(part.strip() for part in document_parts if part).strip() + "\n",
        encoding="utf-8",
    )
    return unit_records, document_markdown


def _write_unit_artifacts(
    source: dict[str, Any],
    unit: ExtractedTranscriptUnit,
    artifact_root: Path,
    processed_at: str,
) -> dict[str, Any]:
    """Write artifacts for one transcript unit and return its index record."""
    unit_label = f"unit_{unit.unit_number:03d}"
    unit_json = artifact_root / "pages" / f"{unit_label}.json"
    base_markdown = artifact_root / "base_markdown" / f"{unit_label}.md"
    markdown = artifact_root / "markdown" / f"{unit_label}.md"
    layout_json = artifact_root / "layouts" / f"{unit_label}_layout.json"
    for path in (unit_json, base_markdown, markdown, layout_json):
        path.parent.mkdir(parents=True, exist_ok=True)

    final_markdown = unit.markdown.strip()
    base_markdown.write_text(final_markdown + "\n", encoding="utf-8")
    markdown.write_text(final_markdown + "\n", encoding="utf-8")
    page_record = {
        "page_number": unit.unit_number,
        "unit_number": unit.unit_number,
        "unit_id": unit.unit_id,
        "unit_type": unit.unit_type,
        "title": unit.title,
        "section_name": unit.section_name,
        "speaker_block_ids": unit.speaker_block_ids,
        "qa_group_id": unit.qa_group_id,
        "speakers": unit.speakers,
        "page_json_path": str(unit_json),
        "unit_json_path": str(unit_json),
        "layout_json_path": str(layout_json),
        "base_markdown_path": str(base_markdown),
        "markdown_path": str(markdown),
        "base_markdown_chars": len(final_markdown),
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
            "page_number": unit.unit_number,
            "unit_number": unit.unit_number,
            "page_text_markdown": final_markdown,
            "rationale": "FactSet XML text parsed directly.",
        },
    )
    _write_json(
        unit_json,
        {
            "stage": "extraction_page",
            "processed_at": processed_at,
            "source": source,
            "page_number": unit.unit_number,
            "unit_number": unit.unit_number,
            "unit_id": unit.unit_id,
            "unit_type": unit.unit_type,
            "title": unit.title,
            "section_name": unit.section_name,
            "speaker_block_ids": unit.speaker_block_ids,
            "qa_group_id": unit.qa_group_id,
            "speakers": unit.speakers,
            "markdown": final_markdown,
            "markdown_path": str(markdown),
            "qa_status": "passed",
            "qa_counts": qa_counts([]),
            "qa_findings": [],
        },
    )
    return page_record


def _write_extraction_manifest(
    extraction_root: Path,
    payload: dict[str, Any],
) -> None:
    """Write a compact extraction manifest for later inspection."""
    _write_json(
        extraction_root / EXTRACTION_MANIFEST_FILE_NAME,
        {
            "stage": payload["stage"],
            "extraction_type": payload["extraction_type"],
            "processed_at": payload["processed_at"],
            "source": payload["source"],
            "document_artifact_path": str(
                extraction_root / DOCUMENT_ARTIFACT_FILE_NAME
            ),
            "document_markdown_path": payload["document_markdown_path"],
            "page_count": payload["page_count"],
            "unit_count": payload["unit_count"],
            "source_unit_count": payload["source_unit_count"],
            "qa_status": payload["qa_status"],
            "qa_counts": payload["qa_counts"],
        },
    )


def _parse_companies(meta: ET.Element, namespace: str) -> list[str]:
    """Return company labels declared in transcript metadata."""
    companies = []
    companies_el = _find(meta, namespace, "companies")
    if companies_el is not None:
        for company in _findall(companies_el, namespace, "company"):
            company_text = _element_text(company)
            if company_text:
                companies.append(company_text)
    return companies


def _companies_from_participants(
    participants: dict[str, TranscriptParticipant],
) -> list[str]:
    """Infer company labels from participant affiliations."""
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
) -> dict[str, TranscriptParticipant]:
    """Return participant metadata keyed by FactSet participant id."""
    participants: dict[str, TranscriptParticipant] = {}
    participants_el = _find(meta, namespace, "participants")
    if participants_el is None:
        return participants

    for participant_el in _findall(participants_el, namespace, "participant"):
        participant_id = participant_el.get("id", "").strip()
        if not participant_id:
            continue
        participants[participant_id] = TranscriptParticipant(
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
    participants: dict[str, TranscriptParticipant],
) -> list[TranscriptSpeakerBlock]:
    """Return ordered speaker blocks from the transcript body."""
    blocks: list[TranscriptSpeakerBlock] = []
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
                TranscriptParticipant(
                    participant_id=participant_id,
                    name="Unknown Speaker",
                ),
            )
            blocks.append(
                TranscriptSpeakerBlock(
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
    """Return clean paragraph text from one speaker element."""
    paragraphs = []
    plist = _find(speaker_el, namespace, "plist")
    paragraph_parent = plist if plist is not None else speaker_el
    for paragraph_el in _findall(paragraph_parent, namespace, "p"):
        paragraph = _element_text(paragraph_el)
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def _qa_groups(
    blocks: list[TranscriptSpeakerBlock],
) -> list[list[TranscriptSpeakerBlock]]:
    """Group Q&A speaker blocks into conversation exchanges."""
    qa_blocks = [block for block in blocks if block.section_name == SECTION_QA]
    groups: list[list[TranscriptSpeakerBlock]] = []
    current_group: list[TranscriptSpeakerBlock] = []
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


def _markdown_for_md_block(block: TranscriptSpeakerBlock) -> str:
    """Return markdown for one management discussion speaker block."""
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
    blocks: list[TranscriptSpeakerBlock],
) -> str:
    """Return markdown for one Q&A exchange."""
    lines = [
        f"# Q&A Exchange {qa_group_id}: {_qa_title_speaker(blocks)}",
        f"Section: {SECTION_QA}",
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


def _speaker_record(block: TranscriptSpeakerBlock) -> dict[str, str]:
    """Return serializable speaker metadata for one block."""
    return {
        "speaker_block_id": str(block.speaker_block_id),
        "participant_id": block.participant_id,
        "name": block.participant.name,
        "title": block.participant.title,
        "affiliation": block.participant.affiliation,
        "participant_type": block.participant.participant_type,
        "speaker_type_hint": block.speaker_type_hint,
    }


def _speaker_role(block: TranscriptSpeakerBlock) -> str:
    """Return a display role from FactSet Q&A hints and participant metadata."""
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


def _qa_title_speaker(blocks: list[TranscriptSpeakerBlock]) -> str:
    """Return a compact title label for a Q&A group."""
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
    """Normalize FactSet section labels into transcript retrieval sections."""
    normalized = section_name.casefold()
    if "management" in normalized and "discussion" in normalized:
        return SECTION_MD
    if "question" in normalized or "q&a" in normalized:
        return SECTION_QA
    return section_name.strip() or "Transcript"


def _selected_unit_numbers(
    total_units: int,
    max_units: int | None,
    unit_numbers: list[int] | tuple[int, ...] | None,
) -> list[int]:
    """Resolve selected one-indexed transcript unit numbers."""
    if unit_numbers:
        selected = sorted({int(unit_number) for unit_number in unit_numbers})
        invalid = [
            unit_number
            for unit_number in selected
            if unit_number < 1 or unit_number > total_units
        ]
        if invalid:
            raise ExtractionStageError(
                f"Selected transcript unit number(s) out of range: {invalid}"
            )
        return selected
    upper = min(total_units, max_units) if max_units is not None else total_units
    return list(range(1, upper + 1))


def _source_record(record: ManifestRecord, source_path: Path | None = None) -> dict[str, Any]:
    """Return source metadata shared by all extraction artifacts."""
    filename_metadata = _parse_filename_metadata(record.file_name)
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
        "source_local_path": str(source_path) if source_path is not None else "",
        **filename_metadata,
    }


def _parse_filename_metadata(filename: str) -> dict[str, str | int]:
    """Return FactSet filename metadata when the name follows vendor format."""
    match = TRANSCRIPT_FILENAME_PATTERN.fullmatch(filename)
    if match is None:
        return {
            "transcript_type": "",
            "event_id": "",
            "version_id": "",
        }
    return {
        "transcript_type": match.group("transcript_type"),
        "event_id": match.group("event_id"),
        "version_id": int(match.group("version_id")),
    }


def _find(parent: ET.Element, namespace: str, tag: str) -> ET.Element | None:
    """Find a direct child, respecting an optional XML namespace."""
    return parent.find(f"{namespace}{tag}")


def _findall(parent: ET.Element, namespace: str, tag: str) -> list[ET.Element]:
    """Find all direct children, respecting an optional XML namespace."""
    return list(parent.findall(f"{namespace}{tag}"))


def _element_text(element: ET.Element | None) -> str:
    """Return normalized text from an XML element and its descendants."""
    if element is None:
        return ""
    return _clean_text(" ".join(element.itertext()))


def _clean_text(text: str) -> str:
    """Normalize whitespace in XML text content."""
    return WHITESPACE_RE.sub(" ", text or "").strip()


def _load_process_records(progress_dir: Path) -> tuple[ManifestRecord, ...]:
    """Load records selected for processing by the manifest stage."""
    path = progress_dir / FILES_TO_PROCESS_FILE_NAME
    payload = _read_json(path)
    rows = payload.get("files_to_process")
    if not isinstance(rows, list):
        raise ExtractionStageError(f"{path} is missing files_to_process list")
    return tuple(_manifest_record_from_mapping(row, path) for row in rows)


def _manifest_record_from_mapping(row: Any, source_path: Path) -> ManifestRecord:
    """Convert one JSON manifest row into a ManifestRecord."""
    if not isinstance(row, dict):
        raise ExtractionStageError(f"Invalid manifest row in {source_path}: {row!r}")
    missing = [field for field in MANIFEST_FIELDS if field not in row]
    if missing:
        raise ExtractionStageError(
            f"Manifest row missing field(s) {missing}: {source_path}"
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
    """Resolve the local earnings-transcripts input folder."""
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
    """Return the per-file artifact root."""
    return progress_dir / ARTIFACTS_DIR_NAME / file_id


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ExtractionStageError(f"Required JSON file is missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ExtractionStageError(f"Invalid JSON file: {path}") from exc
    if not isinstance(payload, dict):
        raise ExtractionStageError(f"JSON file is not an object: {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write deterministic UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _utc_now() -> str:
    """Return the current UTC time as an ISO string."""
    return datetime.now(tz=UTC).isoformat()


def main() -> int:
    """Run the extraction stage."""
    run_extraction_stage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
