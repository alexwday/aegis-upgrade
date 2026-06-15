"""
Deterministic status summaries over research progress logs.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, Iterable, List, Optional, Set

from .progress import ResearchProgressStore, emit_event
from .schemas import ProgressEvent


SOURCE_LABELS: Dict[str, str] = {
    "transcripts": "Transcripts",
    "investor_slides": "Investor slides",
    "supplementary_financials": "Supplementary financials",
    "rts": "Reports to shareholders",
    "pillar3": "Pillar 3",
}


def _clean_text(value: Any, limit: int = 220) -> str:
    """Normalize text for compact progress summaries."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _source_title(source: str) -> str:
    """Return a readable source name from a source id."""
    return SOURCE_LABELS.get(source, source.replace("_", " ").title())


def _strip_source_prefix(source_label: str, value: Any) -> str:
    """Remove repeated source labels from compact combo labels."""
    text = _clean_text(value, 90)
    prefix = f"{source_label}: "
    if text.startswith(prefix):
        return text[len(prefix) :]
    return text


def _finding_label(source_label: str, finding: Dict[str, Any]) -> str:
    """Render a compact label for one completed-source finding."""
    label = _clean_text(finding.get("combo_label"), 90)
    prefix = f"{source_label}: "
    if label.startswith(prefix):
        label = label[len(prefix) :]
    summary = _clean_text(finding.get("summary"), 230)
    metric = finding.get("metric") or {}
    if isinstance(metric, dict):
        metric_name = _clean_text(metric.get("metric_name"), 80)
        metric_value = _clean_text(metric.get("metric_value"), 60)
        unit = _clean_text(metric.get("unit"), 20)
        period = _clean_text(metric.get("period"), 60)
        value_text = f"{metric_value}{unit}" if metric_value and unit else metric_value
        metric_parts = [part for part in (metric_name, value_text, period) if part]
        if metric_parts:
            summary = f"{summary} [{' | '.join(metric_parts)}]" if summary else " | ".join(metric_parts)
    if finding.get("has_table") and summary:
        summary = f"{summary} [table]"
    if label and summary:
        return f"{label}: {summary}"
    return summary or label


def _source_complete_digest(event: ProgressEvent) -> Optional[str]:
    """Summarize completed source metadata without exposing raw payloads."""
    metadata = event.metadata or {}
    source_label = _clean_text(metadata.get("source_label") or _source_title(event.source), 80)
    findings = metadata.get("findings") or []
    finding_texts: List[str] = []
    if isinstance(findings, list):
        for item in findings:
            if isinstance(item, dict):
                rendered = _finding_label(source_label, item)
            else:
                rendered = _clean_text(item, 230)
            if rendered:
                finding_texts.append(rendered)

    if finding_texts:
        visible = finding_texts[:2]
        extra_count = max(0, len(finding_texts) - len(visible))
        suffix = "; ".join(visible)
        if extra_count:
            suffix = f"{suffix}; +{extra_count} more"
        return f"{source_label} found {suffix}."

    gaps = metadata.get("gaps") or []
    if isinstance(gaps, list) and gaps:
        gap_text = _clean_text(gaps[0].get("reason") if isinstance(gaps[0], dict) else gaps[0], 180)
        if gap_text:
            return f"{source_label} completed with no findings yet: {gap_text}."

    quick_summary = _clean_text(metadata.get("quick_summary") or event.message, 220)
    if quick_summary:
        return f"{source_label} completed: {quick_summary}."
    return None


def _completed_source_summary_text(metadata: Dict[str, Any], source_label: str, fallback: str) -> str:
    """Choose one display summary for a completed source."""
    explicit = _clean_text(metadata.get("summary_text"), 420)
    if explicit:
        return explicit

    findings = metadata.get("findings") or []
    if isinstance(findings, list):
        summary_findings: List[str] = []
        fallback_findings: List[str] = []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            text = _clean_text(finding.get("summary"), 360)
            if not text:
                continue
            combo_label = _strip_source_prefix(source_label, finding.get("combo_label"))
            rendered = f"{combo_label}: {text}" if combo_label else text
            if finding.get("finding_type") == "summary":
                summary_findings.append(rendered)
            elif not fallback_findings:
                fallback_findings.append(text)
        if summary_findings:
            return _clean_text(" ".join(summary_findings[:2]), 420)
        if fallback_findings:
            return _clean_text(fallback_findings[0], 420)

    gaps = metadata.get("gaps") or []
    if isinstance(gaps, list):
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            text = _clean_text(gap.get("reason"), 420)
            if text:
                return text

    return fallback


def summarize_progress(events: Iterable[ProgressEvent]) -> Optional[str]:
    """Summarize observed progress without inventing unobserved stages."""
    event_list = list(events)
    if not event_list:
        return None

    unavailable: Set[str] = set()
    active: Set[str] = set()
    completed: Set[str] = set()
    incomplete: Set[str] = set()
    source_digests: List[str] = []
    strategy_digests: List[str] = []

    for event in event_list:
        label = event.combo_label
        if event.stage == "source_complete":
            digest = _source_complete_digest(event)
            if digest:
                source_digests.append(digest)
        elif event.stage == "research_strategy":
            strategy = _clean_text(event.message, 260)
            if strategy:
                strategy_digests.append(strategy)
        if not label:
            continue
        if event.stage in {"partial_availability", "no_available_data"}:
            unavailable.add(label)
        elif event.stage == "combo_complete":
            completed.add(label)
            active.discard(label)
        elif event.stage == "combo_incomplete":
            incomplete.add(label)
            active.discard(label)
        elif event.status in {"started", "running"}:
            active.add(label)

    parts: List[str] = []
    if unavailable:
        parts.append(f"Unavailable data: {', '.join(sorted(unavailable))}.")
    if active:
        parts.append(f"Research in progress for {', '.join(sorted(active))}.")
    if completed:
        parts.append(f"Completed research for {', '.join(sorted(completed))}.")
    if incomplete:
        parts.append(f"Incomplete findings for {', '.join(sorted(incomplete))}.")
    if strategy_digests:
        parts.append(f"Approach: {' '.join(strategy_digests[-2:])}")
    if source_digests:
        parts.append(f"Findings so far: {' '.join(source_digests)}")

    if parts:
        return " ".join(parts)

    return event_list[-1].message


def build_completed_source_summaries(events: Iterable[ProgressEvent]) -> List[Dict[str, Any]]:
    """Capture each completed subagent's own summary for the live status board."""
    summaries: List[Dict[str, Any]] = []
    for event in events:
        if event.stage != "source_complete":
            continue
        metadata = event.metadata or {}
        source_label = _clean_text(metadata.get("source_label") or _source_title(event.source), 80)
        quick_summary = _clean_text(metadata.get("quick_summary") or event.message, 360)
        summary_text = _completed_source_summary_text(metadata, source_label, quick_summary)
        summaries.append(
            {
                "source_id": event.source,
                "source_label": source_label,
                "status": metadata.get("status") or event.status,
                "status_label": _source_status_label(_row_status_from_source_complete(event)),
                "quick_summary": quick_summary,
                "summary_text": summary_text,
                "finding_count": int(metadata.get("finding_count", 0) or 0),
                "gap_count": int(metadata.get("gap_count", 0) or 0),
                "completed_at": event.timestamp.isoformat(),
            }
        )
    return summaries


def _source_status_label(status: str) -> str:
    """Map internal row status to a user-facing label."""
    return {
        "pending": "Pending",
        "checking": "Checking availability",
        "in_progress": "Researching",
        "complete": "Complete",
        "partial": "Partial",
        "unavailable": "No data",
        "incomplete": "Incomplete",
        "error": "Error",
    }.get(status, status.replace("_", " ").title())


def _row_status_from_source_complete(event: ProgressEvent) -> str:
    """Translate source completion metadata to one table status."""
    status = str((event.metadata or {}).get("status") or event.status)
    if event.status == "error" or status == "error":
        return "error"
    if status == "no_available_data":
        return "unavailable"
    if status == "partial_success":
        return "partial"
    if status == "success":
        return "complete"
    return "complete"


def build_source_status_rows(events: Iterable[ProgressEvent]) -> List[Dict[str, Any]]:
    """Build one current-status row per source from progress events."""
    rows: Dict[str, Dict[str, Any]] = {}
    source_order: List[str] = []

    def ensure_row(source: str, event: ProgressEvent) -> Dict[str, Any]:
        if source not in rows:
            source_order.append(source)
            rows[source] = {
                "source_id": source,
                "source_label": _clean_text(
                    (event.metadata or {}).get("source_label") or _source_title(source),
                    80,
                ),
                "status": "pending",
                "status_label": "Pending",
                "detail": "Queued.",
                "finding_count": 0,
                "gap_count": 0,
                "combination_count": int((event.metadata or {}).get("combination_count", 0) or 0),
                "updated_at": event.timestamp.isoformat(),
            }
        return rows[source]

    for event in events:
        row = ensure_row(event.source, event)
        metadata = event.metadata or {}
        if metadata.get("source_label"):
            row["source_label"] = _clean_text(metadata["source_label"], 80)
        if metadata.get("combination_count"):
            row["combination_count"] = int(metadata.get("combination_count") or 0)
        row["updated_at"] = event.timestamp.isoformat()

        if event.stage == "source_queued":
            row["status"] = "pending"
            row["detail"] = _clean_text(event.message, 180) or "Queued."
        elif event.stage == "availability_checked":
            row["status"] = "checking"
            row["detail"] = _clean_text(event.message, 180)
        elif event.stage == "partial_availability":
            if row["status"] not in {"complete", "partial", "unavailable", "error"}:
                row["status"] = "checking"
            row["detail"] = _clean_text(event.message, 180)
        elif event.stage in {"research_started", "research_strategy"}:
            row["status"] = "in_progress"
            row["detail"] = _clean_text(event.message, 180)
        elif event.stage == "combo_complete":
            if row["status"] not in {"complete", "partial", "unavailable", "error"}:
                row["status"] = "in_progress"
            row["detail"] = _clean_text(event.message, 180)
        elif event.stage == "combo_incomplete":
            if row["status"] not in {"complete", "partial", "unavailable", "error"}:
                row["status"] = "incomplete"
            row["detail"] = _clean_text(event.message, 180)
        elif event.stage == "source_complete":
            row["status"] = _row_status_from_source_complete(event)
            row["detail"] = _clean_text(event.message, 180)
            row["finding_count"] = int(metadata.get("finding_count", 0) or 0)
            row["gap_count"] = int(metadata.get("gap_count", 0) or 0)
        elif event.status == "error":
            row["status"] = "error"
            row["detail"] = _clean_text(event.message, 180)

        row["status_label"] = _source_status_label(str(row["status"]))

    return [rows[source] for source in source_order]


def build_research_status_snapshot(events: Iterable[ProgressEvent]) -> Dict[str, Any]:
    """Build the single live research status payload consumed by the UI."""
    event_list = list(events)
    rows = build_source_status_rows(event_list)
    completed_count = sum(
        1 for row in rows if row["status"] in {"complete", "partial", "unavailable", "error"}
    )
    generated_at = event_list[-1].timestamp.isoformat() if event_list else None
    return {
        "rows": rows,
        "completed_summaries": build_completed_source_summaries(event_list),
        "completed_source_count": completed_count,
        "total_source_count": len(rows),
        "generated_at": generated_at,
    }


async def run_status_reporter(
    progress_store: ResearchProgressStore,
    output_queue: asyncio.Queue,
    interval_seconds: float = 15.0,
) -> None:
    """Emit one refreshable status snapshot when research progress changes."""
    _ = interval_seconds
    last_payload: Optional[Dict[str, Any]] = None

    while not progress_store.is_complete:
        await progress_store.wait_changed()
        snapshot = await progress_store.snapshot()
        payload = build_research_status_snapshot(snapshot)
        if payload == last_payload:
            continue
        last_payload = payload
        await emit_event(
            output_queue,
            {
                "type": "research_status_snapshot",
                "name": "aegis",
                "content": payload,
            },
        )

    snapshot = await progress_store.snapshot()
    payload = build_research_status_snapshot(snapshot)
    if payload != last_payload:
        await emit_event(
            output_queue,
            {
                "type": "research_status_snapshot",
                "name": "aegis",
                "content": payload,
            },
        )
