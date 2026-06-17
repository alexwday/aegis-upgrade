"""
Tool implementation for multi-source Aegis document research.
"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

from sqlalchemy import text

from ...connections.postgres_connector import get_connection
from ...utils.logging import get_logger
from ...utils.monitor import add_monitor_entry
from ...utils.settings import config
from .charts import build_chart_options, chart_instruction_text, publish_chart_artifacts
from .progress import ResearchProgressStore, emit_event
from .schemas import (
    BankPeriodCombination,
    Citation,
    CoverageItem,
    DEFAULT_DOCUMENT_SOURCES,
    EvidenceReference,
    Finding,
    Gap,
    MetricObservation,
    ResearchRequest,
    ResearchResult,
    ResearchTable,
    SourceId,
)
from .status_reporter import run_status_reporter


@dataclass(frozen=True)
class RetrieverSpec:
    """Import details for one processed Aegis document retriever."""

    label: str
    retrieve_module: str
    retrieve_function: str
    pipeline_module: str


SOURCE_LABELS: Dict[str, str] = {
    "transcripts": "Transcripts",
    "event_transcripts": "Event transcripts",
    "investor_slides": "Investor slides",
    "supplementary_financials": "Supplementary financials",
    "rts": "Reports to shareholders",
    "pillar3": "Pillar 3",
}

DOCUMENT_RETRIEVERS: Dict[str, RetrieverSpec] = {
    "transcripts": RetrieverSpec(
        label="Transcripts",
        retrieve_module="aegis_agent.model.subagents.transcripts.main",
        retrieve_function="retrieve_transcripts",
        pipeline_module="aegis_agent.model.subagents.transcripts.pipeline",
    ),
    "event_transcripts": RetrieverSpec(
        label="Event transcripts",
        retrieve_module="aegis_agent.model.subagents.event_transcripts.main",
        retrieve_function="retrieve_event_transcripts",
        pipeline_module="aegis_agent.model.subagents.event_transcripts.pipeline",
    ),
    "investor_slides": RetrieverSpec(
        label="Investor slides",
        retrieve_module="aegis_agent.model.subagents.investor_slides.investor_slides",
        retrieve_function="retrieve_investor_slides",
        pipeline_module="aegis_agent.model.subagents.investor_slides.pipeline",
    ),
    "supplementary_financials": RetrieverSpec(
        label="Supplementary financials",
        retrieve_module="aegis_agent.model.subagents.supplementary_financials.supplementary_financials",
        retrieve_function="retrieve_supplementary_financials",
        pipeline_module="aegis_agent.model.subagents.supplementary_financials.pipeline",
    ),
    "rts": RetrieverSpec(
        label="Reports to shareholders",
        retrieve_module="aegis_agent.model.subagents.rts.main",
        retrieve_function="retrieve_rts",
        pipeline_module="aegis_agent.model.subagents.rts.pipeline",
    ),
    "pillar3": RetrieverSpec(
        label="Pillar 3",
        retrieve_module="aegis_agent.model.subagents.pillar3.main",
        retrieve_function="retrieve_pillar3",
        pipeline_module="aegis_agent.model.subagents.pillar3.pipeline",
    ),
}


def _database_names_include(value: Any, source: str) -> bool:
    """Return whether the availability row contains the requested source."""
    if value is None:
        return False
    if isinstance(value, str):
        names = [part.strip().lower() for part in value.strip("{}").split(",")]
    else:
        names = [str(part).strip().lower() for part in value]
    return source.lower() in names


def _bank_matches(combo: BankPeriodCombination, row: Dict[str, Any]) -> bool:
    """Match a requested bank-period combo to one availability row."""
    requested = {
        str(value).lower()
        for value in (combo.bank_id, combo.bank_symbol, combo.bank_name)
        if value is not None and str(value).strip()
    }
    aliases = row.get("bank_aliases") or []
    tags = row.get("bank_tags") or []
    available = {
        str(row.get("bank_id", "")).lower(),
        str(row.get("bank_symbol", "")).lower(),
        str(row.get("bank_name", "")).lower(),
    }
    available.update(str(value).lower() for value in aliases)
    available.update(str(value).lower() for value in tags)
    return bool(requested.intersection(available))


def _hydrate_combo(combo: BankPeriodCombination, row: Dict[str, Any]) -> BankPeriodCombination:
    """Fill missing bank labels from an availability row."""
    return BankPeriodCombination(
        bank_id=str(row.get("bank_id") or combo.bank_id or ""),
        bank_symbol=row.get("bank_symbol") or combo.bank_symbol,
        bank_name=row.get("bank_name") or combo.bank_name,
        fiscal_year=combo.fiscal_year,
        quarter=combo.quarter,
    )


async def check_source_availability(
    source: str,
    combinations: Sequence[BankPeriodCombination],
    context: Dict[str, Any],
) -> Tuple[List[BankPeriodCombination], List[BankPeriodCombination]]:
    """Split requested combinations into source-available and unavailable sets."""
    years = sorted({combo.fiscal_year for combo in combinations})
    quarters = sorted({combo.quarter for combo in combinations})

    async with get_connection(context.get("execution_id")) as conn:
        result = await conn.execute(
            text(
                """
                SELECT
                    bank_id::text AS bank_id,
                    bank_name,
                    bank_symbol,
                    bank_aliases,
                    bank_tags,
                    fiscal_year,
                    quarter,
                    database_names
                FROM aegis_data_availability
                WHERE fiscal_year = ANY(:years)
                  AND quarter = ANY(:quarters)
                """
            ),
            {"years": years, "quarters": quarters},
        )
        rows = [dict(row._mapping) for row in result]  # pylint: disable=protected-access

    available: List[BankPeriodCombination] = []
    unavailable: List[BankPeriodCombination] = []

    for combo in combinations:
        matching_row = next(
            (
                row
                for row in rows
                if int(row["fiscal_year"]) == combo.fiscal_year
                and row["quarter"] == combo.quarter
                and _bank_matches(combo, row)
                and _database_names_include(row.get("database_names"), source)
            ),
            None,
        )
        if matching_row:
            available.append(_hydrate_combo(combo, matching_row))
        else:
            unavailable.append(combo)

    return available, unavailable


async def check_transcript_availability(
    combinations: Sequence[BankPeriodCombination],
    context: Dict[str, Any],
) -> Tuple[List[BankPeriodCombination], List[BankPeriodCombination]]:
    """Backward-compatible transcript availability helper used by tests."""
    return await check_source_availability("transcripts", combinations, context)


def _combo_to_dict(combo: BankPeriodCombination) -> Dict[str, Any]:
    """Convert an agent combo into the dict shape expected by Aegis retrievers."""
    return {
        "bank_id": combo.bank_id,
        "bank_name": combo.bank_name,
        "bank_symbol": combo.bank_symbol,
        "fiscal_year": combo.fiscal_year,
        "quarter": combo.quarter,
    }


def _source_label(source: str) -> str:
    """Return a display label for one source id."""
    return SOURCE_LABELS.get(source, source)


def _placeholder_s3_base(context: Optional[Dict[str, Any]] = None) -> str:
    """Return the configured or placeholder base URL for evidence links."""
    if context and context.get("s3_base_url"):
        return str(context["s3_base_url"]).rstrip("/")
    configured = getattr(config, "s3_reports_base_url", "")
    return str(configured or "https://s3.placeholder.local/aegis").rstrip("/")


def _build_evidence_href(
    s3_key: Optional[str],
    file_type: Optional[str],
    page_number: Optional[int],
    context: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Build a deterministic placeholder evidence URL."""
    if not s3_key:
        return None
    href = f"{_placeholder_s3_base(context)}/{quote(str(s3_key), safe='/')}"
    if str(file_type or "").lower() == "pdf" and page_number:
        href = f"{href}#page={page_number}"
    return href


def _clean_location(value: Any) -> Optional[str]:
    """Normalize empty source-location fields to None."""
    text_value = str(value or "").strip()
    return text_value or None


def _display_label(
    source: str,
    source_label: str,
    file_type: Optional[str],
    page_number: Optional[int],
    location_label: Optional[str],
    sheet_name: Optional[str],
) -> str:
    """Build a compact user-facing evidence label."""
    location = sheet_name or location_label
    if str(file_type or "").lower() != "pdf" and location:
        return f"{source_label} {location}"
    if page_number:
        return f"{source_label} p.{page_number}"
    if location:
        return f"{source_label} {location}"
    return source_label


def _reference_to_evidence(
    source: str,
    reference: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> EvidenceReference:
    """Convert retriever source metadata into the canonical evidence contract."""
    source_label = _source_label(source)
    filename = _clean_location(reference.get("filename"))
    s3_key = _clean_location(reference.get("s3_key") or filename)
    file_type = _clean_location(reference.get("file_type") or Path(s3_key or "").suffix.lstrip("."))
    raw_page = reference.get("page") or reference.get("page_number")
    page_number = int(raw_page) if isinstance(raw_page, (int, float)) and raw_page else None
    location_label = _clean_location(reference.get("location") or reference.get("location_detail"))
    sheet_name = _clean_location(reference.get("sheet") or reference.get("sheet_name"))
    section_name = _clean_location(reference.get("section") or reference.get("section_name"))
    display_label = _display_label(
        source,
        source_label,
        file_type,
        page_number,
        location_label,
        sheet_name or section_name,
    )
    return EvidenceReference(
        source_id=source,  # type: ignore[arg-type]
        source_label=source_label,
        filename=filename,
        page_number=page_number,
        location_label=location_label,
        sheet_name=sheet_name,
        section_name=section_name,
        s3_key=s3_key,
        href=_build_evidence_href(s3_key, file_type, page_number, context),
        display_label=display_label,
    )


def _finding_evidence_refs(
    source: str,
    finding: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> List[EvidenceReference]:
    """Extract canonical evidence references from one raw retriever finding."""
    raw_references = finding.get("references") or []
    refs = [
        _reference_to_evidence(source, reference, context)
        for reference in raw_references
        if isinstance(reference, dict)
    ]
    if refs:
        return refs

    # Fallback for older tests or sources that only return page/location fields.
    fallback_reference = {
        "page": finding.get("page"),
        "location_detail": finding.get("location_detail"),
    }
    return [_reference_to_evidence(source, fallback_reference, context)]


def _load_document_retriever(source: str) -> Tuple[Any, Any]:
    """Return the retrieve function and formatter for one document source."""
    spec = DOCUMENT_RETRIEVERS[source]
    retrieve_module = importlib.import_module(spec.retrieve_module)
    pipeline_module = importlib.import_module(spec.pipeline_module)
    return getattr(retrieve_module, spec.retrieve_function), getattr(
        pipeline_module,
        "format_retrieval_response",
    )


def _format_metric_evidence(finding: Dict[str, Any]) -> List[str]:
    """Build compact evidence strings from a document finding."""
    evidence = []
    refs = finding.get("source_ref_ids") or []
    if refs:
        evidence.append(f"Source refs: {', '.join(str(ref) for ref in refs)}")
    page = finding.get("page")
    location = finding.get("location_detail")
    if page or location:
        evidence.append(f"Location: page {page}, {location}".strip().rstrip(","))
    metric_name = finding.get("metric_name")
    metric_value = finding.get("metric_value")
    unit = finding.get("unit")
    if metric_name or metric_value:
        evidence.append(
            "Metric: "
            f"{metric_name or 'value'} {metric_value or ''}{unit or ''}".strip()
        )
    return evidence


def _metric_from_document_finding(finding: Dict[str, Any]) -> Optional[MetricObservation]:
    """Preserve structured quantitative fields from a document finding."""
    metric = MetricObservation(
        metric_name=str(finding.get("metric_name") or "").strip(),
        metric_value=str(finding.get("metric_value") or "").strip(),
        unit=str(finding.get("unit") or "").strip(),
        period=str(finding.get("period") or "").strip(),
        segment=str(finding.get("segment") or "").strip(),
    )
    if any((metric.metric_name, metric.metric_value, metric.unit, metric.period, metric.segment)):
        return metric
    return None


def _table_from_document_finding(finding: Dict[str, Any]) -> Optional[ResearchTable]:
    """Normalize optional tabular payloads if a retriever returns them."""
    raw_table = finding.get("table")
    if raw_table is None and isinstance(finding.get("tables"), list) and finding["tables"]:
        raw_table = finding["tables"][0]
    if not isinstance(raw_table, dict):
        return None

    columns = [str(column).strip() for column in raw_table.get("columns", []) if str(column).strip()]
    raw_rows = raw_table.get("rows") or []
    rows: List[Dict[str, Any]] = []
    if isinstance(raw_rows, list):
        for raw_row in raw_rows:
            if isinstance(raw_row, dict):
                rows.append({str(key): value for key, value in raw_row.items()})
            elif isinstance(raw_row, list) and columns:
                rows.append(
                    {
                        columns[index]: value
                        for index, value in enumerate(raw_row[: len(columns)])
                    }
                )

    if not columns and rows:
        columns = list(rows[0].keys())
    if not columns and not rows:
        return None

    return ResearchTable(
        title=str(raw_table.get("title") or "").strip() or None,
        columns=columns,
        rows=rows,
        notes=str(raw_table.get("notes") or "").strip() or None,
    )


def _finding_support(finding: Dict[str, Any]) -> Dict[str, Any]:
    """Return compact source fields that help downstream synthesis."""
    support: Dict[str, Any] = {}
    for key in (
        "page",
        "location_detail",
        "source_ref_ids",
        "confidence",
        "period",
        "segment",
    ):
        value = finding.get(key)
        if value not in (None, "", []):
            support[key] = value
    return support


def _finding_type(
    finding: Dict[str, Any],
    metric: Optional[MetricObservation],
    table: Optional[ResearchTable],
) -> str:
    """Classify a finding for downstream formatting choices."""
    requested_type = str(finding.get("finding_type") or "").strip().lower()
    if requested_type in {"quantitative", "qualitative", "table", "summary", "detailed"}:
        return requested_type
    if table is not None:
        return "table"
    if metric is not None:
        return "quantitative"
    if str(finding.get("details") or "").strip():
        return "detailed"
    return "qualitative"


def _result_from_document_raw(
    source: str,
    raw: Dict[str, Any],
    unavailable: Sequence[BankPeriodCombination],
    dropdown_markdown: str,
    context: Optional[Dict[str, Any]] = None,
) -> ResearchResult:
    """Convert an Aegis processed-source retrieval result into agent result schema."""
    findings: List[Finding] = []
    citations: List[Citation] = []
    coverage: List[CoverageItem] = []
    gaps: List[Gap] = []
    source_label = _source_label(source)

    for combo_result in raw.get("combo_results", []):
        combo = combo_result.get("combo") or {}
        combo_model = BankPeriodCombination.model_validate(combo)
        combo_label = combo_model.label
        combo_findings = combo_result.get("findings") or []
        chunks = combo_result.get("expanded_chunks") or combo_result.get("reranked_chunks") or []
        sections = sorted(
            {
                str(chunk.get("name") or chunk.get("section_name") or chunk.get("chunk_id") or "")
                for chunk in chunks
                if chunk.get("name") or chunk.get("section_name") or chunk.get("chunk_id")
            }
        )[:8]

        coverage.append(
            CoverageItem(
                combo_label=combo_label,
                status="complete" if combo_findings else "incomplete",
                chunk_count=len(chunks),
                sections=sections,
                source=source,  # type: ignore[arg-type]
            )
        )

        if not combo_findings:
            gaps.append(
                Gap(
                    combo_label=f"{source_label}: {combo_label}",
                    reason=f"No structured {source_label} findings were extracted.",
                )
            )
            continue

        for item in combo_findings:
            summary = str(item.get("finding") or "").strip()
            if not summary:
                continue
            evidence_refs = _finding_evidence_refs(source, item, context)
            metric = _metric_from_document_finding(item)
            table = _table_from_document_finding(item)
            findings.append(
                Finding(
                    combo_label=f"{source_label}: {combo_label}",
                    summary=summary,
                    finding_type=_finding_type(item, metric, table),  # type: ignore[arg-type]
                    details=str(item.get("details") or "").strip() or None,
                    metric=metric,
                    table=table,
                    support=_finding_support(item),
                    evidence_refs=evidence_refs,
                    evidence=_format_metric_evidence(item),
                )
            )
            for evidence_ref in evidence_refs:
                citations.append(
                    Citation(
                        combo_label=f"{source_label}: {combo_label}",
                        evidence_id=evidence_ref.evidence_id,
                        source_id=evidence_ref.source_id,
                        section_name=evidence_ref.section_name or evidence_ref.location_label,
                        title=evidence_ref.display_label,
                        filename=evidence_ref.filename,
                        page_number=evidence_ref.page_number,
                        href=evidence_ref.href,
                        display_label=evidence_ref.display_label,
                        text_excerpt=summary,
                    )
                )

    for combo in unavailable:
        gaps.append(
            Gap(
                combo_label=f"{source_label}: {combo.label}",
                reason=f"No {source_label} data is available in aegis_data_availability.",
            )
        )

    if findings and gaps:
        status = "partial_success"
    elif findings:
        status = "success"
    elif gaps:
        status = "no_available_data"
    else:
        status = "no_available_data"

    return ResearchResult(
        status=status,
        quick_summary=(
            f"{source_label} research produced {len(findings)} finding(s), "
            f"{len(coverage)} researched combo(s), and {len(unavailable)} unavailable combo(s)."
        ),
        findings=findings,
        citations=citations,
        gaps=gaps,
        coverage=coverage,
        dropdown_markdown=dropdown_markdown,
    )


async def _run_document_source(
    source: str,
    request: ResearchRequest,
    context: Dict[str, Any],
    output_queue: Optional[asyncio.Queue],
    progress: ResearchProgressStore,
) -> ResearchResult:
    """Run one processed document source through the sibling Aegis retriever."""
    source_label = _source_label(source)
    await emit_event(
        output_queue,
        {
            "type": "subagent_start",
            "name": source,
            "content": f"Checking {source_label} availability.",
        },
    )
    available, unavailable = await check_source_availability(source, request.combinations, context)
    await _record_availability_progress(source, source_label, available, unavailable, progress)

    if not available:
        return ResearchResult(
            status="no_available_data",
            quick_summary=f"No requested bank-period combinations have {source_label} data.",
            gaps=[
                Gap(
                    combo_label=f"{source_label}: {combo.label}",
                    reason=f"No {source_label} data is available in aegis_data_availability.",
                )
                for combo in unavailable
            ],
        )

    for combo in available:
        await progress.add(
            source,
            "research_started",
            "started",
            f"{source_label} research started for {combo.label}.",
            combo_label=f"{source_label}: {combo.label}",
            visible=True,
        )

    retrieve, _format_response = _load_document_retriever(source)
    raw = await retrieve(
        query_text=request.question,
        latest_message=request.question,
        bank_period_combinations=[_combo_to_dict(combo) for combo in available],
        context=context,
    )
    strategy_message = _document_strategy_message(source_label, raw)
    if strategy_message:
        await progress.add(
            source,
            "research_strategy",
            "running",
            strategy_message,
            metadata={"summary": strategy_message},
            visible=True,
        )
    result = _result_from_document_raw(source, raw, unavailable, "", context)

    completed = {item.combo_label for item in result.coverage if item.status == "complete"}
    for combo in available:
        combo_label = f"{source_label}: {combo.label}"
        if combo.label in completed:
            await progress.add(
                source,
                "combo_complete",
                "complete",
                f"{source_label} research completed for {combo.label}.",
                combo_label=combo_label,
                visible=True,
            )
        else:
            await progress.add(
                source,
                "combo_incomplete",
                "complete",
                f"{source_label} research completed for {combo.label} with incomplete findings.",
                combo_label=combo_label,
                visible=True,
            )

    return result


async def _record_availability_progress(
    source: str,
    source_label: str,
    available: Sequence[BankPeriodCombination],
    unavailable: Sequence[BankPeriodCombination],
    progress: ResearchProgressStore,
) -> None:
    """Record availability progress for one source."""
    await progress.add(
        source,
        "availability_checked",
        "complete",
        (
            f"{source_label} availability checked: {len(available)} available, "
            f"{len(unavailable)} unavailable."
        ),
        metadata={
            "available": [combo.label for combo in available],
            "unavailable": [combo.label for combo in unavailable],
        },
        visible=True,
    )

    for combo in unavailable:
        await progress.add(
            source,
            "partial_availability",
            "complete",
            f"No {source_label} data is available for {combo.label}.",
            combo_label=f"{source_label}: {combo.label}",
            visible=True,
        )


def _evidence_key(reference: EvidenceReference) -> Tuple[str, str, str, str, str, str]:
    """Return a stable dedupe key for one evidence reference."""
    return (
        str(reference.source_id),
        reference.s3_key or reference.filename or "",
        str(reference.page_number or ""),
        reference.sheet_name or "",
        reference.section_name or "",
        reference.location_label or reference.display_label,
    )


def _assign_evidence_ids(findings: Sequence[Finding]) -> Dict[str, Dict[str, EvidenceReference]]:
    """Assign turn-local E# IDs and build the evidence registry."""
    registry: Dict[str, Dict[str, EvidenceReference]] = {}
    seen: Dict[Tuple[str, str, str, str, str, str], EvidenceReference] = {}
    next_id = 1

    for finding in findings:
        assigned_refs: List[EvidenceReference] = []
        for reference in finding.evidence_refs:
            key = _evidence_key(reference)
            assigned = seen.get(key)
            if assigned is None:
                evidence_id = f"E{next_id}"
                next_id += 1
                assigned = reference.model_copy(update={"evidence_id": evidence_id})
                seen[key] = assigned
                registry.setdefault(str(assigned.source_id), {})[evidence_id] = assigned
            assigned_refs.append(assigned)
        finding.evidence_refs = assigned_refs

    return registry


def _compact_text(value: Any, limit: int = 280) -> str:
    """Normalize long metadata text for progress payloads."""
    text_value = " ".join(str(value or "").split())
    if len(text_value) <= limit:
        return text_value
    return text_value[: limit - 3].rstrip() + "..."


def _compact_items(values: Any, limit: int = 4) -> List[str]:
    """Return a small list of clean strings from list-like metadata."""
    if not isinstance(values, list):
        return []
    items = []
    for value in values:
        text_value = _compact_text(value, 90)
        if text_value:
            items.append(text_value)
        if len(items) >= limit:
            break
    return items


def _strip_source_prefix(source_label: str, combo_label: str) -> str:
    """Remove a repeated source prefix from a displayed combo label."""
    prefix = f"{source_label}: "
    if combo_label.startswith(prefix):
        return combo_label[len(prefix) :]
    return combo_label


def _status_metric_text(metric: Optional[MetricObservation]) -> str:
    """Format metric context for compact live status summaries."""
    if metric is None:
        return ""
    parts: List[str] = []
    if metric.metric_name:
        parts.append(metric.metric_name)
    if metric.metric_value:
        value = f"{metric.metric_value}{metric.unit}" if metric.unit else metric.metric_value
        parts.append(value)
    if metric.period:
        parts.append(metric.period)
    if metric.segment:
        parts.append(metric.segment)
    return f" ({' | '.join(parts)})" if parts else ""


def _is_generic_source_summary(source_label: str, value: str) -> bool:
    """Return whether a source summary is only count/status metadata."""
    normalized = value.lower()
    source_prefix = source_label.lower()
    generic_fragments = (
        " research produced ",
        " research completed for ",
        " requested bank-period combinations",
        " source research was completed",
    )
    return normalized.startswith(source_prefix) and any(
        fragment in normalized for fragment in generic_fragments
    )


def _source_status_summary_text(
    source: str,
    result: ResearchResult,
    limit: int = 2,
) -> str:
    """Build one compact status summary for a completed source."""
    source_label = _source_label(source)
    summary_findings = [
        finding for finding in result.findings if finding.finding_type == "summary"
    ][:limit]
    if summary_findings:
        parts = []
        for finding in summary_findings:
            label = _strip_source_prefix(source_label, finding.combo_label)
            parts.append(f"{label}: {finding.summary}")
        return _compact_text(" ".join(parts), 420)

    if result.quick_summary and not _is_generic_source_summary(source_label, result.quick_summary):
        return _compact_text(result.quick_summary, 420)

    if result.findings:
        finding = result.findings[0]
        text = f"{finding.summary}{_status_metric_text(finding.metric)}"
        if finding.details:
            text = f"{text} Details: {finding.details}"
        if finding.table:
            table_label = finding.table.title or "table returned"
            text = f"{text} Includes {table_label}."
        return _compact_text(text, 420)

    if result.gaps:
        return _compact_text(result.gaps[0].reason, 420)

    return _compact_text(result.quick_summary, 420)


def _source_combo_summaries(source: str, result: ResearchResult) -> List[Dict[str, str]]:
    """Build one compact status summary per bank-period combination."""
    source_label = _source_label(source)
    combo_entries: Dict[str, Dict[str, Any]] = {}

    def ensure_entry(combo_label: str, status: str = "complete") -> Dict[str, Any]:
        label = _strip_source_prefix(source_label, combo_label)
        entry = combo_entries.setdefault(
            label,
            {
                "combo_label": label,
                "summary": "",
                "status": status,
                "_priority": 0,
            },
        )
        if status and entry.get("status") in {"pending", "complete"}:
            entry["status"] = status
        return entry

    for item in result.coverage:
        ensure_entry(item.combo_label, item.status)

    for finding in result.findings:
        entry = ensure_entry(finding.combo_label, "complete")
        text = f"{finding.summary}{_status_metric_text(finding.metric)}"
        if finding.details:
            text = f"{text} Details: {finding.details}"
        if finding.table:
            table_label = finding.table.title or "table returned"
            text = f"{text} Includes {table_label}."
        priority = 2 if finding.finding_type == "summary" else 1
        if priority > int(entry.get("_priority", 0)):
            entry["summary"] = _compact_text(text, 260)
            entry["_priority"] = priority

    for gap in result.gaps:
        entry = ensure_entry(gap.combo_label, "incomplete")
        if not entry.get("summary"):
            entry["summary"] = _compact_text(gap.reason, 260)

    for entry in combo_entries.values():
        if entry.get("summary"):
            continue
        status = str(entry.get("status") or "")
        if status == "complete":
            entry["summary"] = "Research completed."
        elif status == "error":
            entry["summary"] = "Research failed."
        elif status == "unavailable":
            entry["summary"] = "No data is available."
        else:
            entry["summary"] = "No structured findings were extracted."

    return [
        {
            "combo_label": str(entry["combo_label"]),
            "summary": _compact_text(entry.get("summary"), 260),
            "status": str(entry.get("status") or "complete"),
        }
        for entry in combo_entries.values()
        if entry.get("combo_label")
    ]


def _document_strategy_message(source_label: str, raw: Dict[str, Any]) -> Optional[str]:
    """Summarize document retriever query-prep decisions for progress UI."""
    prepared = raw.get("prepared_query") or {}
    if not isinstance(prepared, dict):
        return None

    parts: List[str] = []
    metrics = _compact_items(prepared.get("metrics"))
    keywords = _compact_items(prepared.get("keywords"))
    sub_queries = _compact_items(prepared.get("sub_queries"), limit=2)
    rewritten_query = _compact_text(prepared.get("rewritten_query"), 120)

    if metrics:
        parts.append(f"metrics: {', '.join(metrics)}")
    if keywords:
        parts.append(f"keywords: {', '.join(keywords)}")
    if sub_queries:
        parts.append(f"{len(sub_queries)} focused sub-query(s)")
    if not parts and rewritten_query:
        parts.append(f"rewritten query: {rewritten_query}")
    if not parts:
        return None
    return f"{source_label} search focus: {'; '.join(parts)}."


def _source_completion_metadata(source: str, result: ResearchResult) -> Dict[str, Any]:
    """Build compact metadata for interim source-completion summaries."""
    return {
        "status": result.status,
        "source_label": _source_label(source),
        "quick_summary": _compact_text(result.quick_summary, 320),
        "summary_text": _source_status_summary_text(source, result),
        "combo_summaries": _source_combo_summaries(source, result),
        "finding_count": len(result.findings),
        "gap_count": len(result.gaps),
        "findings": [
            {
                "combo_label": finding.combo_label,
                "summary": _compact_text(finding.summary),
                "finding_type": finding.finding_type,
                "metric": finding.metric.model_dump(mode="json") if finding.metric else None,
                "details": _compact_text(finding.details, 220) if finding.details else None,
                "has_table": finding.table is not None,
            }
            for finding in result.findings[:5]
        ],
        "gaps": [
            {
                "combo_label": gap.combo_label,
                "reason": _compact_text(gap.reason, 220),
            }
            for gap in result.gaps[:5]
        ],
        "coverage": [
            {
                "combo_label": item.combo_label,
                "status": item.status,
                "chunk_count": item.chunk_count,
                "sections": item.sections[:8],
                "source": item.source,
            }
            for item in result.coverage
        ],
    }


def _evidence_marker_text(evidence_refs: Sequence[EvidenceReference]) -> str:
    """Render assigned evidence refs as final-answer-compatible markers."""
    markers = [
        f"[[{reference.evidence_id}]]"
        for reference in evidence_refs
        if reference.evidence_id
    ]
    return "".join(markers)


def _metric_text(metric: Optional[MetricObservation]) -> str:
    """Format one metric payload for the inspectable source panel."""
    if metric is None:
        return ""
    parts = []
    if metric.metric_name:
        parts.append(f"metric={metric.metric_name}")
    if metric.metric_value:
        value = f"{metric.metric_value}{metric.unit}" if metric.unit else metric.metric_value
        parts.append(f"value={value}")
    elif metric.unit:
        parts.append(f"unit={metric.unit}")
    if metric.period:
        parts.append(f"period={metric.period}")
    if metric.segment:
        parts.append(f"segment={metric.segment}")
    return f" [{'; '.join(parts)}]" if parts else ""


def _format_table_markdown(table: ResearchTable) -> List[str]:
    """Format a structured table payload as portable markdown."""
    lines: List[str] = []
    if table.title:
        lines.append(f"  Table: {table.title}")
    if table.columns and table.rows:
        header = "| " + " | ".join(table.columns) + " |"
        separator = "| " + " | ".join("---" for _ in table.columns) + " |"
        lines.extend([f"  {header}", f"  {separator}"])
        for row in table.rows[:12]:
            values = [str(row.get(column, "")) for column in table.columns]
            lines.append("  | " + " | ".join(values) + " |")
    if table.notes:
        lines.append(f"  Notes: {table.notes}")
    return lines


def _combo_finding_labels(source_label: str, combo_label: str) -> set[str]:
    """Return possible finding combo labels for source-prefixed and plain combos."""
    if combo_label.startswith(f"{source_label}:"):
        return {combo_label, combo_label.removeprefix(f"{source_label}:").strip()}
    return {combo_label, f"{source_label}: {combo_label}"}


def _format_source_result_dropdown(source: str, result: ResearchResult) -> str:
    """Build source-panel markdown from structured findings and evidence IDs."""
    source_label = _source_label(source)
    lines = ["## Research Findings", ""]
    rendered_combos: set[str] = set()

    for coverage in result.coverage:
        display_combo = coverage.combo_label
        labels = _combo_finding_labels(source_label, display_combo)
        combo_findings = [finding for finding in result.findings if finding.combo_label in labels]
        combo_gaps = [gap for gap in result.gaps if gap.combo_label in labels]
        lines.extend([f"### {display_combo}", ""])
        if combo_findings:
            for finding in combo_findings:
                markers = _evidence_marker_text(finding.evidence_refs)
                marker_text = f" {markers}" if markers else ""
                lines.append(
                    f"- {finding.summary}{_metric_text(finding.metric)}{marker_text}"
                )
                if finding.details:
                    lines.append(f"  Details: {finding.details}")
                if finding.table:
                    lines.extend(_format_table_markdown(finding.table))
        elif combo_gaps:
            for gap in combo_gaps:
                lines.append(f"- {gap.reason}")
        elif coverage.status == "incomplete":
            lines.append(f"- No structured {source_label} findings were extracted.")
        else:
            lines.append("- No linked findings were returned.")
        if coverage.sections:
            lines.append(f"  Sections searched: {', '.join(coverage.sections[:8])}")
        lines.append("")
        rendered_combos.update(labels)

    remaining_findings = [
        finding for finding in result.findings if finding.combo_label not in rendered_combos
    ]
    for finding in remaining_findings:
        lines.extend([f"### {finding.combo_label}", ""])
        markers = _evidence_marker_text(finding.evidence_refs)
        marker_text = f" {markers}" if markers else ""
        lines.append(f"- {finding.summary}{_metric_text(finding.metric)}{marker_text}")
        if finding.details:
            lines.append(f"  Details: {finding.details}")
        if finding.table:
            lines.extend(_format_table_markdown(finding.table))
        lines.append("")

    remaining_gaps = [gap for gap in result.gaps if gap.combo_label not in rendered_combos]
    for gap in remaining_gaps:
        lines.extend([f"### {gap.combo_label}", "", f"- {gap.reason}", ""])

    if not result.coverage and not result.findings and not result.gaps:
        lines.append(f"No {source_label} research findings were returned.")

    return "\n".join(lines).strip()


def _evidence_registry_payload(
    evidence_registry: Dict[str, Dict[str, EvidenceReference]],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Serialize the evidence registry for websocket metadata."""
    return {
        source: {
            evidence_id: reference.model_dump(mode="json")
            for evidence_id, reference in references.items()
        }
        for source, references in evidence_registry.items()
    }


async def _record_source_complete(
    source: str,
    result: ResearchResult,
    progress: ResearchProgressStore,
) -> None:
    """Record one source result as soon as that source finishes."""
    metadata = _source_completion_metadata(source, result)
    source_label = metadata["source_label"]
    if result.findings:
        gap_text = f" and {len(result.gaps)} gap(s)" if result.gaps else ""
        message = (
            f"{source_label} research completed with {len(result.findings)} finding(s)"
            f"{gap_text}."
        )
    else:
        message = result.quick_summary
    await progress.add(
        source,
        "source_complete",
        "complete",
        message,
        metadata=metadata,
        visible=True,
    )


def _aggregate_results(results: Sequence[ResearchResult]) -> ResearchResult:
    """Merge source-level research results into one tool result."""
    findings: List[Finding] = []
    citations: List[Citation] = []
    gaps: List[Gap] = []
    coverage: List[CoverageItem] = []
    dropdowns: List[str] = []

    for result in results:
        findings.extend(result.findings)
        citations.extend(result.citations)
        gaps.extend(result.gaps)
        coverage.extend(result.coverage)
        if result.dropdown_markdown:
            dropdowns.append(result.dropdown_markdown)

    if findings and gaps:
        status = "partial_success"
    elif findings:
        status = "success"
    elif gaps:
        status = "no_available_data"
    else:
        status = "no_available_data"

    source_summaries = [result.quick_summary for result in results if result.quick_summary]
    quick_summary = " ".join(source_summaries) or "No source research was completed."
    evidence_registry = _assign_evidence_ids(findings)
    linked_citations = [
        Citation(
            combo_label=finding.combo_label,
            evidence_id=reference.evidence_id,
            source_id=reference.source_id,
            section_name=reference.section_name or reference.location_label,
            title=reference.display_label,
            filename=reference.filename,
            page_number=reference.page_number,
            href=reference.href,
            display_label=reference.display_label,
            text_excerpt=finding.summary,
        )
        for finding in findings
        for reference in finding.evidence_refs
    ]
    unlinked_citations = [
        citation for citation in citations if str(citation.source_id or "") not in SOURCE_LABELS
    ]

    aggregate = ResearchResult(
        status=status,
        quick_summary=quick_summary,
        findings=findings,
        citations=linked_citations + unlinked_citations,
        evidence_registry=evidence_registry,
        gaps=gaps,
        coverage=coverage,
        dropdown_markdown="\n\n".join(dropdowns),
    )
    aggregate.chart_options = [
        option.model_dump(mode="json") for option in build_chart_options(aggregate.findings)
    ]
    return aggregate


async def _run_one_source(
    source: SourceId,
    request: ResearchRequest,
    context: Dict[str, Any],
    output_queue: Optional[asyncio.Queue],
    progress: ResearchProgressStore,
) -> ResearchResult:
    """Run one requested source and return a structured source result."""
    return await _run_document_source(source, request, context, output_queue, progress)


async def _run_one_source_with_progress(
    source: SourceId,
    request: ResearchRequest,
    context: Dict[str, Any],
    output_queue: Optional[asyncio.Queue],
    progress: ResearchProgressStore,
) -> ResearchResult:
    """Run one source and record its completion before sibling sources finish."""
    try:
        result = await _run_one_source(source, request, context, output_queue, progress)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger = get_logger()
        logger.exception(
            "agent.source_research.failed",
            execution_id=context.get("execution_id"),
            source=source,
            error=str(exc),
        )
        result = _source_failure_result(source, request, exc)
        await progress.add(
            source,
            "source_error",
            "error",
            result.quick_summary,
            metadata={
                "source_label": _source_label(source),
                "error": result.gaps[0].reason if result.gaps else str(exc),
            },
            visible=True,
        )
    await _record_source_complete(source, result, progress)
    return result


def _source_error_reason(source_label: str, exc: Exception) -> str:
    """Return a concise source-level error without leaking SQL text to the model."""
    message = " ".join(str(exc).split())
    lowered = message.lower()
    if "undefinedtableerror" in lowered or (
        "relation" in lowered and "does not exist" in lowered
    ):
        if "embeddings" in lowered:
            return (
                f"{source_label} is unavailable because its required embeddings table "
                "is missing. Load or create that source store, then retry."
            )
        return (
            f"{source_label} is unavailable because a required source table is missing. "
            "Load or create that source store, then retry."
        )
    return f"{source_label} research failed: {_compact_text(message, 220)}"


def _source_failure_result(
    source: SourceId,
    request: ResearchRequest,
    exc: Exception,
) -> ResearchResult:
    """Convert one source exception into an aggregate-friendly research result."""
    source_label = _source_label(source)
    reason = _source_error_reason(source_label, exc)
    return ResearchResult(
        status="error",
        quick_summary=f"{source_label} research could not run. {reason}",
        gaps=[
            Gap(
                combo_label=f"{source_label}: {combo.label}",
                reason=reason,
            )
            for combo in request.combinations
        ],
        coverage=[
            CoverageItem(
                combo_label=f"{source_label}: {combo.label}",
                status="error",
                source=source,
            )
            for combo in request.combinations
        ],
    )


async def run_research_tool(
    arguments: Dict[str, Any],
    context: Dict[str, Any],
    output_queue: Optional[asyncio.Queue] = None,
    progress_store: Optional[ResearchProgressStore] = None,
) -> Dict[str, Any]:
    """
    Run document research with automatic availability preflight per source.

    If only some requested source/bank/period combinations are available, the
    function emits partial-availability status and continues researching the
    available source combinations.
    """
    logger = get_logger()
    request = ResearchRequest.model_validate(arguments)
    if not request.sources:
        request.sources = list(DEFAULT_DOCUMENT_SOURCES)

    progress = progress_store or ResearchProgressStore(output_queue)
    reporter_task: Optional[asyncio.Task] = None
    stage_start = datetime.now(timezone.utc)

    if output_queue is not None:
        reporter_task = asyncio.create_task(run_status_reporter(progress, output_queue))

    try:
        for source in request.sources:
            await progress.add(
                source,
                "source_queued",
                "pending",
                f"{_source_label(source)} queued for {len(request.combinations)} bank-period combination(s).",
                metadata={
                    "source_label": _source_label(source),
                    "combination_count": len(request.combinations),
                    "combinations": [combo.label for combo in request.combinations],
                },
                visible=False,
            )
        tasks = [
            asyncio.create_task(
                _run_one_source_with_progress(source, request, context, output_queue, progress)
            )
            for source in request.sources
        ]
        gathered_results = await asyncio.gather(*tasks, return_exceptions=True)
        source_results = [
            result
            if isinstance(result, ResearchResult)
            else _source_failure_result(source, request, result)
            for source, result in zip(request.sources, gathered_results)
        ]
        result = _aggregate_results(source_results)
        chart_options = build_chart_options(result.findings, request.question)
        result.chart_options = [
            option.model_dump(mode="json") for option in chart_options
        ]
        context["chart_instruction"] = chart_instruction_text(chart_options)
        publish_chart_artifacts(chart_options, context)
        source_dropdowns = [
            _format_source_result_dropdown(source, source_result)
            for source, source_result in zip(request.sources, source_results)
        ]
        result.dropdown_markdown = "\n\n".join(source_dropdowns)
        evidence_registry_payload = _evidence_registry_payload(result.evidence_registry)

        await emit_event(
            output_queue,
            {
                "type": "agent_status",
                "name": "aegis",
                "content": "Evidence registry ready.",
                "metadata": {
                    "evidence_registry": evidence_registry_payload,
                    "internal": True,
                    "research_result": True,
                },
            },
        )
        for source, dropdown_markdown in zip(request.sources, source_dropdowns):
            await emit_event(
                output_queue,
                {
                    "type": "subagent",
                    "name": source,
                    "content": dropdown_markdown,
                    "metadata": {
                        "format": "dropdown_markdown",
                        "evidence_registry": evidence_registry_payload,
                    },
                },
            )

        logger.info(
            "agent.research.complete",
            execution_id=context.get("execution_id"),
            status=result.status,
            requested=len(request.combinations),
            sources=list(request.sources),
            findings=len(result.findings),
            gaps=len(result.gaps),
        )
        return result.model_dump(mode="json")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception(
            "agent.research.failed",
            execution_id=context.get("execution_id"),
            error=str(exc),
        )
        await progress.add(
            "aegis",
            "source_complete",
            "error",
            f"Research failed: {exc}",
            metadata={"error": str(exc)},
            visible=True,
        )
        return ResearchResult(
            status="error",
            quick_summary=f"Research failed: {exc}",
            gaps=[Gap(combo_label="research", reason=str(exc))],
        ).model_dump(mode="json")
    finally:
        progress.mark_complete()
        if reporter_task:
            await reporter_task
        add_monitor_entry(
            stage_name="Document_Research",
            stage_start_time=stage_start,
            stage_end_time=datetime.now(timezone.utc),
            status="Success",
            decision_details="Document research tool completed",
        )
