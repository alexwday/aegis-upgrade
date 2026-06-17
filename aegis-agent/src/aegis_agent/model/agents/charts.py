"""Chart option generation and JSON chart artifacts for Aegis answers."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from .schemas import Finding


MAX_CHART_OPTIONS = 3

QUARTER_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
SOURCE_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z 0-9&-]*:\s+")
COMBO_RE = re.compile(r"^(?P<bank>.+?)\s+(?P<quarter>Q[1-4])\s+(?P<year>\d{4})$")
NUMERIC_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


class MetricFact(BaseModel):
    """One normalized numeric observation that can be plotted."""

    bank_label: str
    period_label: str
    fiscal_year: int
    quarter: str
    metric_name: str
    value: float
    unit: str = ""
    segment: str = ""
    evidence_ids: List[str] = Field(default_factory=list)


class ChartSpec(BaseModel):
    """Renderer-neutral JSON spec consumed by the browser chart component."""

    chart_type: str
    metric_name: str
    unit: str = ""
    x_label: str
    y_label: str
    facts: List[MetricFact] = Field(default_factory=list)


class ChartOption(BaseModel):
    """A backend-approved chart the final model may place inline."""

    chart_id: str
    chart_type: str
    title: str
    subtitle: str
    metric_name: str
    unit: str = ""
    spec: ChartSpec
    evidence_ids: List[str] = Field(default_factory=list)
    status: str = "ready"


class ChartArtifact(BaseModel):
    """A chart JSON artifact ready for websocket/UI hydration."""

    chart_id: str
    chart_type: str
    title: str
    subtitle: str
    alt_text: str
    spec: ChartSpec
    evidence_ids: List[str] = Field(default_factory=list)
    status: str = "ready"


def chart_instruction_text(options: Sequence[ChartOption]) -> str:
    """Return compact instructions for the final-answer model."""
    if not options:
        return (
            "No backend-approved chart options are available for this turn. "
            "Do not include chart markers."
        )
    payload = [
        {
            "chart_id": option.chart_id,
            "chart_type": option.chart_type,
            "title": option.title,
            "subtitle": option.subtitle,
            "metric_name": option.metric_name,
            "unit": option.unit,
            "evidence_ids": option.evidence_ids,
        }
        for option in options
    ]
    return (
        "Backend-approved interactive chart options for this turn are listed below. "
        "You may insert a chart inline only by writing its exact marker, for example "
        "`[[CHART:C1]]`, between paragraphs. Use at most two charts, only when a chart "
        "materially improves comparison or trend comprehension. Never invent chart IDs "
        "or chart data.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def build_chart_options(findings: Sequence[Finding]) -> List[ChartOption]:
    """Build deterministic chart candidates from structured research findings."""
    facts = _dedupe_facts(_facts_from_findings(findings))
    groups = _group_facts(facts)
    candidates: List[ChartOption] = []

    for (_metric_key, unit), group in groups.items():
        if len(group) < 2:
            continue
        banks = sorted({fact.bank_label for fact in group})
        periods = sorted(
            {(fact.fiscal_year, fact.quarter, fact.period_label) for fact in group},
            key=lambda item: (item[0], QUARTER_ORDER.get(item[1], 0)),
        )
        if len(banks) >= 2 and len(periods) == 1:
            candidates.append(
                _make_option(
                    "peer_bar",
                    group,
                    title=f"{group[0].metric_name} across peers",
                    subtitle=f"{periods[0][2]} | {unit or 'reported value'}",
                    x_label="Bank",
                    y_label=unit or "Reported value",
                )
            )
        elif len(banks) == 1 and len(periods) >= 2:
            candidates.append(
                _make_option(
                    "trend_line",
                    group,
                    title=f"{banks[0]} {group[0].metric_name} trend",
                    subtitle=f"{periods[0][2]} to {periods[-1][2]} | {unit or 'reported value'}",
                    x_label="Period",
                    y_label=unit or "Reported value",
                )
            )
        elif len(banks) >= 2 and len(periods) >= 2:
            candidates.append(
                _make_option(
                    "heatmap",
                    group,
                    title=f"{group[0].metric_name} by bank and period",
                    subtitle=f"{len(banks)} banks x {len(periods)} periods | {unit or 'reported value'}",
                    x_label="Period",
                    y_label="Bank",
                )
            )

    filtered = [option for option in candidates if option.evidence_ids]
    for index, option in enumerate(filtered[:MAX_CHART_OPTIONS], start=1):
        option.chart_id = f"C{index}"
    return filtered[:MAX_CHART_OPTIONS]


def publish_chart_artifacts(
    options: Sequence[ChartOption],
    context: Dict[str, Any],
) -> None:
    """Queue chart JSON artifacts for websocket hydration."""
    if not options:
        return

    event_queue: Optional[asyncio.Queue] = context.get("background_event_queue")
    artifacts: Dict[str, Dict[str, Any]] = context.setdefault("chart_artifacts", {})

    for option in options:
        artifact = ChartArtifact(
            chart_id=option.chart_id,
            chart_type=option.chart_type,
            title=option.title,
            subtitle=option.subtitle,
            alt_text=_alt_text(option),
            spec=option.spec,
            evidence_ids=option.evidence_ids,
        )
        payload = artifact.model_dump(mode="json")
        artifacts[artifact.chart_id] = payload
        if event_queue is not None:
            event_queue.put_nowait(
                {
                    "type": "chart_artifact",
                    "name": "aegis",
                    "content": payload,
                }
            )


def _facts_from_findings(findings: Sequence[Finding]) -> List[MetricFact]:
    facts: List[MetricFact] = []
    for finding in findings:
        metric = finding.metric
        if metric is None or not metric.metric_name or not metric.metric_value:
            continue
        value = _parse_numeric(metric.metric_value)
        if value is None:
            continue
        evidence_ids = [
            str(ref.evidence_id)
            for ref in finding.evidence_refs
            if ref.evidence_id
        ]
        if not evidence_ids:
            continue
        combo = _parse_combo_label(finding.combo_label)
        if combo is None:
            continue
        bank_label, quarter, fiscal_year = combo
        period_label = metric.period.strip() or f"{quarter} {fiscal_year}"
        facts.append(
            MetricFact(
                bank_label=bank_label,
                period_label=period_label,
                fiscal_year=fiscal_year,
                quarter=quarter,
                metric_name=metric.metric_name.strip(),
                value=value,
                unit=metric.unit.strip(),
                segment=metric.segment.strip(),
                evidence_ids=evidence_ids,
            )
        )
    return facts


def _parse_numeric(value: str) -> Optional[float]:
    text = str(value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    match = NUMERIC_RE.search(text.replace("$", ""))
    if not match:
        return None
    parsed = float(match.group(0).replace(",", ""))
    return -parsed if negative else parsed


def _parse_combo_label(combo_label: str) -> Optional[Tuple[str, str, int]]:
    label = SOURCE_PREFIX_RE.sub("", str(combo_label or "").strip())
    match = COMBO_RE.match(label)
    if not match:
        return None
    return match.group("bank"), match.group("quarter"), int(match.group("year"))


def _dedupe_facts(facts: Iterable[MetricFact]) -> List[MetricFact]:
    seen: Dict[Tuple[str, str, int, str, str, str], MetricFact] = {}
    for fact in facts:
        key = (
            _metric_key(fact.metric_name),
            fact.unit.lower(),
            fact.fiscal_year,
            fact.quarter,
            fact.bank_label.lower(),
            fact.segment.lower(),
        )
        seen.setdefault(key, fact)
    return list(seen.values())


def _group_facts(facts: Iterable[MetricFact]) -> Dict[Tuple[str, str], List[MetricFact]]:
    groups: Dict[Tuple[str, str], List[MetricFact]] = {}
    for fact in facts:
        groups.setdefault((_metric_key(fact.metric_name), fact.unit), []).append(fact)
    return groups


def _make_option(
    chart_type: str,
    facts: Sequence[MetricFact],
    title: str,
    subtitle: str,
    x_label: str,
    y_label: str,
) -> ChartOption:
    evidence_ids = sorted({evidence_id for fact in facts for evidence_id in fact.evidence_ids})
    sorted_facts = sorted(
        facts,
        key=lambda fact: (fact.bank_label, fact.fiscal_year, QUARTER_ORDER.get(fact.quarter, 0)),
    )
    spec = ChartSpec(
        chart_type=chart_type,
        metric_name=facts[0].metric_name,
        unit=facts[0].unit,
        x_label=x_label,
        y_label=y_label,
        facts=list(sorted_facts),
    )
    return ChartOption(
        chart_id="C0",
        chart_type=chart_type,
        title=title,
        subtitle=subtitle,
        metric_name=facts[0].metric_name,
        unit=facts[0].unit,
        spec=spec,
        evidence_ids=evidence_ids,
    )


def _metric_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _alt_text(option: ChartOption) -> str:
    return (
        f"{option.title}. {option.chart_type.replace('_', ' ')} showing "
        f"{option.metric_name} for {len(option.spec.facts)} source-grounded observations."
    )
