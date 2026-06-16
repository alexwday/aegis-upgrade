"""Compatibility adapter for event transcript research."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ...agents.schemas import (
    BankPeriodCombination,
    Citation,
    CoverageItem,
    EvidenceReference,
    Finding,
    Gap,
    MetricObservation,
    ResearchResult,
    ResearchTable,
)
from .pipeline import SEARCH_TOP_K, format_retrieval_response, run_retrieval_pipeline


def _combo_to_dict(combo: BankPeriodCombination) -> Dict[str, Any]:
    """Convert an agent combo into the dict shape expected by the retriever."""
    return {
        "bank_id": combo.bank_id,
        "bank_name": combo.bank_name,
        "bank_symbol": combo.bank_symbol,
        "fiscal_year": combo.fiscal_year,
        "quarter": combo.quarter,
    }


def _finding_type(raw: Dict[str, Any], metric: Optional[MetricObservation]) -> str:
    """Return a supported finding type value."""
    requested = str(raw.get("finding_type") or "").strip().lower()
    if requested in {"quantitative", "qualitative", "table", "summary", "detailed"}:
        return requested
    if raw.get("table"):
        return "table"
    if metric is not None:
        return "quantitative"
    return "qualitative"


def _metric_from_raw(raw: Dict[str, Any]) -> Optional[MetricObservation]:
    """Build optional metric metadata from a raw event transcript finding."""
    metric = MetricObservation(
        metric_name=str(raw.get("metric_name") or "").strip(),
        metric_value=str(raw.get("metric_value") or "").strip(),
        unit=str(raw.get("unit") or "").strip(),
        period=str(raw.get("period") or "").strip(),
        segment=str(raw.get("segment") or "").strip(),
    )
    if any((metric.metric_name, metric.metric_value, metric.unit, metric.period, metric.segment)):
        return metric
    return None


def _table_from_raw(raw: Dict[str, Any]) -> Optional[ResearchTable]:
    """Build optional table metadata from a raw event transcript finding."""
    table = raw.get("table")
    if not isinstance(table, dict):
        return None
    columns = [str(column) for column in table.get("columns", [])]
    rows = table.get("rows") if isinstance(table.get("rows"), list) else []
    if not columns and not rows:
        return None
    return ResearchTable(
        title=str(table.get("title") or "").strip() or None,
        columns=columns,
        rows=[row for row in rows if isinstance(row, dict)],
        notes=str(table.get("notes") or "").strip() or None,
    )


def _evidence_from_reference(reference: Dict[str, Any]) -> EvidenceReference:
    """Convert retriever source metadata into an evidence reference."""
    location = str(reference.get("location") or reference.get("location_detail") or "").strip()
    filename = str(reference.get("filename") or "").strip() or None
    page = reference.get("page") or reference.get("page_number")
    page_number = int(page) if isinstance(page, (int, float)) and page else None
    display = f"Event transcripts {location}".strip() if location else "Event transcripts"
    return EvidenceReference(
        source_id="event_transcripts",
        source_label="Event transcripts",
        filename=filename,
        page_number=page_number,
        location_label=location or None,
        s3_key=str(reference.get("s3_key") or filename or "").strip() or None,
        display_label=display,
    )


def _result_from_raw(raw: Dict[str, Any], dropdown_markdown: str) -> ResearchResult:
    """Convert raw event transcript pipeline output into the agent result schema."""
    findings: List[Finding] = []
    citations: List[Citation] = []
    coverage: List[CoverageItem] = []
    gaps: List[Gap] = []

    for combo_result in raw.get("combo_results", []):
        combo = BankPeriodCombination.model_validate(combo_result.get("combo") or {})
        combo_findings = combo_result.get("findings") or []
        chunks = combo_result.get("expanded_chunks") or combo_result.get("reranked_chunks") or []
        sections = sorted(
            {
                str(chunk.get("name") or chunk.get("chunk_id") or "")
                for chunk in chunks
                if chunk.get("name") or chunk.get("chunk_id")
            }
        )[:8]
        coverage.append(
            CoverageItem(
                combo_label=combo.label,
                status="complete" if combo_findings else "incomplete",
                chunk_count=len(chunks),
                sections=sections,
                source="event_transcripts",
            )
        )
        if not combo_findings:
            gaps.append(
                Gap(
                    combo_label=combo.label,
                    reason="No structured event transcript findings were extracted.",
                )
            )
            continue

        for raw_finding in combo_findings:
            summary = str(raw_finding.get("finding") or "").strip()
            if not summary:
                continue
            refs = [
                _evidence_from_reference(reference)
                for reference in raw_finding.get("references") or []
                if isinstance(reference, dict)
            ]
            metric = _metric_from_raw(raw_finding)
            table = _table_from_raw(raw_finding)
            finding = Finding(
                combo_label=combo.label,
                summary=summary,
                finding_type=_finding_type(raw_finding, metric),  # type: ignore[arg-type]
                details=str(raw_finding.get("details") or "").strip() or None,
                metric=metric,
                table=table,
                evidence_refs=refs,
            )
            findings.append(finding)
            for ref in refs:
                citations.append(
                    Citation(
                        combo_label=combo.label,
                        source_id="event_transcripts",
                        section_name=ref.location_label,
                        title=ref.display_label,
                        filename=ref.filename,
                        page_number=ref.page_number,
                        display_label=ref.display_label,
                        text_excerpt=summary,
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
            f"Event transcript research produced {len(findings)} finding(s) "
            f"across {len(coverage)} researched combo(s)."
        ),
        findings=findings,
        citations=citations,
        gaps=gaps,
        coverage=coverage,
        dropdown_markdown=dropdown_markdown,
    )


async def research_event_transcripts(
    question: str,
    combinations: Sequence[BankPeriodCombination],
    context: Dict[str, Any],
    output_queue: Optional[Any] = None,
    progress_store: Optional[Any] = None,
) -> ResearchResult:
    """Retrieve and research event transcript evidence using the processed-source pipeline."""
    _ = output_queue, progress_store
    raw = await run_retrieval_pipeline(
        query_text=question,
        latest_message=question,
        bank_period_combinations=[_combo_to_dict(combo) for combo in combinations],
        context=context,
        search_top_k=SEARCH_TOP_K,
    )
    return _result_from_raw(raw, format_retrieval_response(raw))
