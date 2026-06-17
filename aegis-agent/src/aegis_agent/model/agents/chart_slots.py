"""Inline chart slot parsing and asynchronous chart artifact generation."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ...connections.llm_connector import complete_with_tools
from ...utils.logging import get_logger
from .charts import (
    MAX_PLANNER_POINTS,
    MAX_PLANNER_SERIES,
    ChartArtifact,
    ChartOption,
    MetricFact,
    SUPPORTED_PLANNER_CHART_TYPES,
    _alt_text,
    _dedupe_facts,
    _facts_from_findings,
    _make_custom_option,
    _make_multi_series_option,
    _make_option,
    _metric_key,
    _period_sort_key,
    _sort_facts,
    build_chart_options,
    validate_planner_chart_plan,
)
from .schemas import Finding, ResearchResult


CHART_SLOT_RE = re.compile(r"\[\[CHART_SLOT:(.*?)\]\]", re.S)
CHART_SLOT_START = "[[CHART_SLOT:"
MAX_CHART_SLOT_WORKERS = 4
PERIOD_SLOT_RE = re.compile(r"\b(Q[1-4])\s+(20\d{2})\b", re.I)


class ChartSlot(BaseModel):
    """A chart placeholder authored by the final response model."""

    model_config = ConfigDict(extra="ignore")

    slot_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    chart_type: str = Field(..., min_length=1)
    intent: str = Field(..., min_length=1)
    subtitle: str = ""
    banks: List[str] = Field(default_factory=list)
    periods: List[str] = Field(default_factory=list)
    metrics: List[str] = Field(default_factory=list)
    source_ids: List[str] = Field(default_factory=list)

    @field_validator("slot_id")
    @classmethod
    def normalize_slot_id(cls, value: str) -> str:
        slot_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(value or "").strip())
        if not slot_id:
            raise ValueError("slot_id is required")
        if not slot_id.startswith("C"):
            slot_id = f"C{slot_id}"
        return slot_id

    @field_validator("chart_type")
    @classmethod
    def validate_chart_type(cls, value: str) -> str:
        chart_type = str(value or "").strip()
        if chart_type not in SUPPORTED_PLANNER_CHART_TYPES:
            raise ValueError(f"unsupported chart_type: {chart_type}")
        return chart_type

    @field_validator("banks", "periods", "metrics", "source_ids", mode="before")
    @classmethod
    def normalize_string_list(cls, value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()]


class ChartSlotStreamProcessor:
    """Replace streamed chart-slot markers and start backend chart workers."""

    def __init__(self, context: Dict[str, Any]) -> None:
        self.context = context
        self.buffer = ""
        existing_artifacts = context.get("chart_artifacts")
        self.used_ids: set[str] = (
            set(existing_artifacts.keys()) if isinstance(existing_artifacts, Mapping) else set()
        )

    def push(self, content_delta: str) -> List[Dict[str, Any]]:
        """Return websocket events produced from one final-answer text delta."""
        self.buffer += content_delta
        return self._process_buffer(final=False)

    def finish(self) -> List[Dict[str, Any]]:
        """Flush buffered text when the final-answer stream ends."""
        return self._process_buffer(final=True)

    def _process_buffer(self, *, final: bool) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        cursor = 0
        for match in CHART_SLOT_RE.finditer(self.buffer):
            if match.start() > cursor:
                _append_agent_event(events, self.buffer[cursor : match.start()])
            events.extend(self._events_for_raw_slot(match.group(1)))
            cursor = match.end()

        remainder = self.buffer[cursor:]
        partial_index = remainder.find(CHART_SLOT_START)
        if partial_index >= 0 and not final:
            if partial_index > 0:
                _append_agent_event(events, remainder[:partial_index])
            self.buffer = remainder[partial_index:]
            return events

        if partial_index >= 0 and final:
            _append_agent_event(events, remainder[:partial_index])
            self.buffer = ""
            get_logger().warning(
                "chart_slot.incomplete_marker",
                execution_id=self.context.get("execution_id"),
            )
            return events

        _append_agent_event(events, remainder)
        self.buffer = ""
        return events

    def _events_for_raw_slot(self, raw_slot: str) -> List[Dict[str, Any]]:
        slot = _parse_chart_slot(raw_slot)
        if slot is None:
            get_logger().warning(
                "chart_slot.invalid_marker",
                execution_id=self.context.get("execution_id"),
                marker_preview=raw_slot[:160],
            )
            return []

        slot.slot_id = self._unique_slot_id(slot.slot_id)
        if not _slot_is_approved(slot, self.context):
            get_logger().warning(
                "chart_slot.unaudited_or_blocked",
                execution_id=self.context.get("execution_id"),
                chart_id=slot.slot_id,
                chart_type=slot.chart_type,
            )
            return []
        pending = pending_chart_artifact(slot)
        _start_chart_slot_worker(slot, self.context)
        return [
            {"type": "chart_artifact", "name": "aegis", "content": pending},
            {"type": "agent", "name": "aegis", "content": f"[[CHART:{slot.slot_id}]]"},
        ]

    def _unique_slot_id(self, preferred: str) -> str:
        if preferred not in self.used_ids:
            self.used_ids.add(preferred)
            return preferred
        index = 1
        while f"C{index}" in self.used_ids:
            index += 1
        slot_id = f"C{index}"
        self.used_ids.add(slot_id)
        return slot_id


def chart_slot_instruction_text() -> str:
    """Return final-answer instructions for async chart slots."""
    chart_types = ", ".join(sorted(SUPPORTED_PLANNER_CHART_TYPES))
    return (
        "For new data-backed final answers after research, insert async chart slots "
        "when a chart would materially improve a quantitative comparison, trend, "
        "movement, distribution, bridge, or mix readout. Put each slot marker on its "
        "own line using exactly this shape, with no trailing punctuation: "
        '`[[CHART_SLOT:{"slot_id":"C1","title":"Short chart title","chart_type":"peer_rank_bar",'
        '"intent":"What the chart should show","banks":["RY-CA","TD-CA"],'
        '"periods":["Q2 2026"],"metrics":["CET1 ratio"]}]]`. '
        "Required slot fields are slot_id, title, chart_type, and intent. Optional "
        "fields are subtitle, banks, periods, metrics, and source_ids. Supported "
        f"chart_type values are: {chart_types}. Use small_multiple_panel for broad "
        "mixed-metric comparisons; do not ask for one shared-axis chart across "
        "different metrics or units. Before finalizing with any CHART_SLOT marker, "
        "call audit_chart_slots with every intended slot. If the audit reports missing "
        "values and backfill_allowed is true, call run_research once with the suggested "
        "research question and combinations, then call audit_chart_slots again. Only "
        "emit slots from the latest approved_slots list. Skip blocked slots. Do not "
        "invent chart data in the response body."
    )


def pending_chart_artifact(slot: ChartSlot) -> Dict[str, Any]:
    """Return a pending chart artifact payload for immediate UI rendering."""
    return {
        "chart_id": slot.slot_id,
        "chart_type": slot.chart_type,
        "title": slot.title,
        "subtitle": slot.subtitle or "Loading chart",
        "alt_text": slot.title,
        "evidence_ids": [],
        "rationale": slot.intent,
        "status": "pending",
    }


def hidden_chart_artifact(slot: ChartSlot, reason: str) -> Dict[str, Any]:
    """Return a hidden chart artifact payload for failed chart generation."""
    return {
        "chart_id": slot.slot_id,
        "chart_type": slot.chart_type,
        "title": slot.title,
        "subtitle": slot.subtitle,
        "alt_text": slot.title,
        "evidence_ids": [],
        "rationale": reason,
        "status": "hidden",
    }


def audit_chart_slots(arguments: Mapping[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Audit proposed chart slots against the latest cumulative research result."""
    raw_slots = arguments.get("slots") if isinstance(arguments, Mapping) else None
    if not isinstance(raw_slots, list):
        raw_slots = []

    result = _latest_research_result(context)
    approved_slots: List[Dict[str, Any]] = []
    blocked_slots: List[Dict[str, Any]] = []
    missing_values: List[Dict[str, Any]] = []

    for raw_slot in raw_slots:
        try:
            slot = ChartSlot.model_validate(raw_slot)
        except ValidationError as exc:
            blocked_slots.append(
                {
                    "slot": raw_slot if isinstance(raw_slot, Mapping) else {},
                    "reason": "invalid_slot",
                    "message": str(exc),
                }
            )
            continue

        audit = _audit_one_chart_slot(slot, result)
        if audit["approved"]:
            approved_slots.append(slot.model_dump(mode="json"))
        else:
            blocked_slots.append(
                {
                    "slot": slot.model_dump(mode="json"),
                    "reason": audit["reason"],
                    "missing_values": audit["missing_values"],
                }
            )
            missing_values.extend(audit["missing_values"])

    approved_by_id = {slot["slot_id"]: slot for slot in approved_slots}
    context["approved_chart_slots"] = approved_by_id
    context["latest_chart_audit"] = {
        "approved_slots": approved_slots,
        "blocked_slots": blocked_slots,
        "missing_values": missing_values,
    }

    backfill_allowed = bool(missing_values) and not bool(context.get("chart_backfill_used"))
    if backfill_allowed:
        context["chart_backfill_pending"] = True
    else:
        context["chart_backfill_pending"] = False

    return {
        "status": "success",
        "approved_slots": approved_slots,
        "blocked_slots": blocked_slots,
        "missing_values": missing_values,
        "suggested_research_question": _suggested_backfill_question(missing_values),
        "suggested_combinations": _suggested_backfill_combinations(missing_values),
        "backfill_allowed": backfill_allowed,
    }


def _audit_one_chart_slot(
    slot: ChartSlot,
    result: Optional[ResearchResult],
) -> Dict[str, Any]:
    if result is None:
        return {
            "approved": False,
            "reason": "no_research_result",
            "missing_values": _slot_missing_values(slot) or [{"slot_id": slot.slot_id}],
        }

    if len(slot.metrics) > 1 and slot.chart_type != "small_multiple_panel":
        return {
            "approved": False,
            "reason": "mixed_metric_requires_small_multiple_panel",
            "missing_values": [],
        }

    if not slot.banks or not slot.periods or not slot.metrics:
        option = _deterministic_option_for_slot(slot, result.findings)
        if option is not None and _artifact_is_supported(option, result.evidence_registry):
            return {"approved": True, "reason": "deterministic_option_available", "missing_values": []}
        return {"approved": False, "reason": "needs_slot_detail", "missing_values": []}

    facts = _dedupe_facts(_facts_from_findings(result.findings))
    valid_evidence_ids = _valid_registry_evidence_ids(result.evidence_registry)
    missing = []
    for bank in slot.banks:
        for period in slot.periods:
            for metric in slot.metrics:
                if not _has_grounded_metric_fact(facts, valid_evidence_ids, bank, period, metric):
                    missing.append(
                        {
                            "slot_id": slot.slot_id,
                            "title": slot.title,
                            "bank": bank,
                            "period": period,
                            "metric": metric,
                        }
                    )

    if missing:
        return {"approved": False, "reason": "missing_values", "missing_values": missing}
    return {"approved": True, "reason": "complete_coverage", "missing_values": []}


def _has_grounded_metric_fact(
    facts: Sequence[MetricFact],
    valid_evidence_ids: set[str],
    bank: str,
    period: str,
    metric: str,
) -> bool:
    for fact in facts:
        if not _matches_bank_filter(fact, [bank]):
            continue
        if not _matches_period_filter(fact, [period]):
            continue
        if not _matches_metric_filter(fact, [metric]):
            continue
        if set(fact.evidence_ids).issubset(valid_evidence_ids):
            return True
    return False


def _slot_missing_values(slot: ChartSlot) -> List[Dict[str, Any]]:
    if not slot.banks or not slot.periods or not slot.metrics:
        return []
    return [
        {
            "slot_id": slot.slot_id,
            "title": slot.title,
            "bank": bank,
            "period": period,
            "metric": metric,
        }
        for bank in slot.banks
        for period in slot.periods
        for metric in slot.metrics
    ]


def _suggested_backfill_question(missing_values: Sequence[Mapping[str, Any]]) -> str:
    if not missing_values:
        return ""
    banks = _ordered_unique(str(item.get("bank") or "") for item in missing_values)
    periods = _ordered_unique(str(item.get("period") or "") for item in missing_values)
    metrics = _ordered_unique(str(item.get("metric") or "") for item in missing_values)
    return (
        "Find the exact numeric values and source evidence for "
        f"{', '.join(metrics)} for {', '.join(banks)} in {', '.join(periods)}. "
        "Return only values needed to complete approved chart comparisons."
    )


def _suggested_backfill_combinations(missing_values: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    combinations = []
    seen = set()
    for item in missing_values:
        parsed = _period_to_combo_fields(str(item.get("period") or ""))
        bank = str(item.get("bank") or "").strip()
        if not bank or parsed is None:
            continue
        quarter, fiscal_year = parsed
        key = (bank, fiscal_year, quarter)
        if key in seen:
            continue
        seen.add(key)
        combinations.append(
            {"bank_symbol": bank, "fiscal_year": fiscal_year, "quarter": quarter}
        )
    return combinations


def _period_to_combo_fields(period: str) -> Optional[Tuple[str, int]]:
    match = PERIOD_SLOT_RE.search(period)
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def _ordered_unique(values: Iterable[str]) -> List[str]:
    result = []
    seen = set()
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in seen:
            seen.add(clean)
            result.append(clean)
    return result


async def build_chart_artifact_for_slot(
    slot: ChartSlot,
    context: Mapping[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build and validate a chart artifact for a slot using deterministic then LLM paths."""
    result = _latest_research_result(context)
    if result is None:
        return None
    option = _deterministic_option_for_slot(slot, result.findings)
    if option is None:
        option = await _llm_option_for_slot(slot, result.findings, context)
    if option is None:
        return None
    option.chart_id = slot.slot_id
    option.title = slot.title
    option.subtitle = slot.subtitle or option.subtitle
    option.chart_type = option.spec.chart_type
    option.rationale = option.rationale or slot.intent
    if not _artifact_is_supported(option, result.evidence_registry):
        return None
    artifact = ChartArtifact(
        chart_id=option.chart_id,
        chart_type=option.chart_type,
        title=option.title,
        subtitle=option.subtitle,
        alt_text=_alt_text(option),
        spec=option.spec,
        evidence_ids=option.evidence_ids,
        rationale=option.rationale,
    )
    return artifact.model_dump(mode="json")


def _append_agent_event(events: List[Dict[str, Any]], content: str) -> None:
    if content:
        events.append({"type": "agent", "name": "aegis", "content": content})


def _slot_is_approved(slot: ChartSlot, context: Mapping[str, Any]) -> bool:
    approved_slots = context.get("approved_chart_slots")
    if not isinstance(approved_slots, Mapping):
        return False
    approved = approved_slots.get(slot.slot_id)
    if not isinstance(approved, Mapping):
        return False
    try:
        approved_slot = ChartSlot.model_validate(approved)
    except ValidationError:
        return False
    return slot.model_dump(mode="json") == approved_slot.model_dump(mode="json")


def _parse_chart_slot(raw_slot: str) -> Optional[ChartSlot]:
    try:
        payload = json.loads(raw_slot)
        return ChartSlot.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return None


def _start_chart_slot_worker(slot: ChartSlot, context: Dict[str, Any]) -> None:
    tasks: List[asyncio.Task] = context.setdefault("chart_worker_tasks", [])
    if len([task for task in tasks if not task.done()]) >= MAX_CHART_SLOT_WORKERS:
        _emit_chart_event(context, hidden_chart_artifact(slot, "Too many chart workers are already running."))
        return
    tasks.append(asyncio.create_task(_run_chart_slot_worker(slot, context)))


async def _run_chart_slot_worker(slot: ChartSlot, context: Dict[str, Any]) -> None:
    try:
        artifact = await build_chart_artifact_for_slot(slot, context)
        if artifact is None:
            artifact = hidden_chart_artifact(slot, "No source-grounded chart could be built.")
        _emit_chart_event(context, artifact)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        get_logger().warning(
            "chart_slot.worker_failed",
            execution_id=context.get("execution_id"),
            chart_id=slot.slot_id,
            error=str(exc),
        )
        _emit_chart_event(context, hidden_chart_artifact(slot, "Chart generation failed."))


def _emit_chart_event(context: Mapping[str, Any], artifact: Mapping[str, Any]) -> None:
    queue: Optional[asyncio.Queue] = context.get("background_event_queue")  # type: ignore[assignment]
    if queue is not None:
        queue.put_nowait({"type": "chart_artifact", "name": "aegis", "content": dict(artifact)})


def _latest_research_result(context: Mapping[str, Any]) -> Optional[ResearchResult]:
    result = context.get("latest_research_result")
    if isinstance(result, ResearchResult):
        return result
    if isinstance(result, Mapping):
        try:
            return ResearchResult.model_validate(result)
        except ValidationError:
            return None
    return None


def _deterministic_option_for_slot(
    slot: ChartSlot,
    findings: Sequence[Finding],
) -> Optional[ChartOption]:
    facts = _filter_slot_facts(slot, _dedupe_facts(_facts_from_findings(findings)))
    option = _small_multiple_option_for_slot(slot, facts)
    if option is not None:
        return option

    candidates = build_chart_options(findings, slot.intent)
    candidates = [
        candidate
        for candidate in candidates
        if candidate.chart_type == slot.chart_type and _slot_option_matches(slot, candidate)
    ]
    if candidates:
        return candidates[0]

    if slot.chart_type == "peer_rank_bar":
        return _peer_rank_option_for_slot(slot, facts)
    if slot.chart_type in {"trend_line", "trend_bar"}:
        return _trend_option_for_slot(slot, facts)
    if slot.chart_type == "multi_series_line":
        return _multi_series_option_for_slot(slot, facts)
    return None


def _small_multiple_option_for_slot(slot: ChartSlot, facts: Sequence[MetricFact]) -> Optional[ChartOption]:
    if slot.chart_type != "small_multiple_panel":
        return None
    grouped: Dict[Tuple[str, str], List[MetricFact]] = {}
    for fact in facts:
        grouped.setdefault((_metric_key(fact.metric_name), fact.unit), []).append(fact)
    series = []
    evidence_ids = set()
    for (_metric, unit), group in sorted(grouped.items())[:MAX_PLANNER_SERIES]:
        ordered = _sort_facts(group)
        if len(ordered) < 2:
            continue
        evidence_ids.update(eid for fact in ordered for eid in fact.evidence_ids)
        label_mode = "Period" if len({fact.period_label for fact in ordered}) > 1 else "Bank"
        series.append(
            {
                "name": ordered[0].metric_name,
                "unit": unit,
                "points": [
                    {
                        "label": fact.period_label if label_mode == "Period" else fact.bank_label,
                        "period_label": fact.period_label,
                        "bank_label": fact.bank_label,
                        "value": fact.value,
                        "evidence_ids": fact.evidence_ids,
                    }
                    for fact in ordered[:MAX_PLANNER_POINTS]
                ],
            }
        )
    if len(series) < 2:
        return None
    all_periods = {
        point["period_label"]
        for item in series
        for point in item["points"]
        if point.get("period_label")
    }
    all_banks = {
        point["bank_label"]
        for item in series
        for point in item["points"]
        if point.get("bank_label")
    }
    x_label = "Period" if len(all_periods) > 1 and len(all_banks) <= 1 else "Bank"
    return _make_custom_option(
        "small_multiple_panel",
        slot.title,
        slot.subtitle or f"{len(series)} metrics with separate scales",
        "Multiple metrics",
        "",
        x_label,
        "Reported value",
        sorted(evidence_ids),
        series=series,
        source_kind="chart_slot",
    )


def _peer_rank_option_for_slot(slot: ChartSlot, facts: Sequence[MetricFact]) -> Optional[ChartOption]:
    grouped = _facts_by_metric_unit(facts)
    for (_metric, _unit), group in grouped.items():
        periods = {fact.period_label for fact in group}
        banks = {fact.bank_label for fact in group}
        if len(group) >= 2 and len(periods) == 1 and len(banks) >= 2:
            return _make_option(
                "peer_rank_bar",
                group,
                title=slot.title,
                subtitle=slot.subtitle or f"{next(iter(periods))} | {group[0].unit or 'reported value'}",
                x_label=group[0].unit or "Reported value",
                y_label="Bank",
            )
    return None


def _trend_option_for_slot(slot: ChartSlot, facts: Sequence[MetricFact]) -> Optional[ChartOption]:
    grouped = _facts_by_metric_unit(facts)
    for (_metric, _unit), group in grouped.items():
        banks = {fact.bank_label for fact in group}
        periods = sorted({fact.period_label for fact in group}, key=_period_sort_key)
        if len(group) >= 2 and len(banks) == 1 and len(periods) >= 2:
            return _make_option(
                slot.chart_type,
                group,
                title=slot.title,
                subtitle=slot.subtitle or f"{periods[0]} to {periods[-1]} | {group[0].unit or 'reported value'}",
                x_label="Period",
                y_label=group[0].unit or "Reported value",
            )
    return None


def _multi_series_option_for_slot(slot: ChartSlot, facts: Sequence[MetricFact]) -> Optional[ChartOption]:
    grouped = _facts_by_metric_unit(facts)
    for (_metric, _unit), group in grouped.items():
        banks = {fact.bank_label for fact in group}
        periods = {fact.period_label for fact in group}
        if len(banks) >= 2 and len(periods) >= 2:
            return _make_multi_series_option(group)
    return None


def _facts_by_metric_unit(facts: Sequence[MetricFact]) -> Dict[Tuple[str, str], List[MetricFact]]:
    grouped: Dict[Tuple[str, str], List[MetricFact]] = {}
    for fact in facts:
        grouped.setdefault((_metric_key(fact.metric_name), fact.unit), []).append(fact)
    return grouped


def _filter_slot_facts(slot: ChartSlot, facts: Sequence[MetricFact]) -> List[MetricFact]:
    return [
        fact
        for fact in facts
        if _matches_bank_filter(fact, slot.banks)
        and _matches_period_filter(fact, slot.periods)
        and _matches_metric_filter(fact, slot.metrics)
    ]


def _slot_option_matches(slot: ChartSlot, option: ChartOption) -> bool:
    facts = option.spec.facts
    if facts:
        return bool(_filter_slot_facts(slot, facts))
    option_blob = " ".join([option.title, option.metric_name, option.subtitle]).lower()
    return not slot.metrics or any(_metric_key(metric) in option_blob for metric in slot.metrics)


def _matches_bank_filter(fact: MetricFact, banks: Sequence[str]) -> bool:
    if not banks:
        return True
    fact_bank = _normalize_bank(fact.bank_label)
    return any(_normalize_bank(bank) == fact_bank for bank in banks)


def _matches_period_filter(fact: MetricFact, periods: Sequence[str]) -> bool:
    if not periods:
        return True
    fact_period = _metric_key(fact.period_label)
    return any(_metric_key(period) == fact_period for period in periods)


def _matches_metric_filter(fact: MetricFact, metrics: Sequence[str]) -> bool:
    if not metrics:
        return True
    fact_metric = _metric_key(fact.metric_name)
    return any(metric_key and (metric_key in fact_metric or fact_metric in metric_key) for metric_key in map(_metric_key, metrics))


def _normalize_bank(value: str) -> str:
    key = _metric_key(value)
    aliases = {
        "rbc": "ry ca",
        "royal bank": "ry ca",
        "ry": "ry ca",
        "td": "td ca",
        "bmo": "bmo ca",
        "bns": "bns ca",
        "scotia": "bns ca",
        "scotiabank": "bns ca",
        "cibc": "cm ca",
        "cm": "cm ca",
        "nbc": "na ca",
        "national bank": "na ca",
        "na": "na ca",
    }
    return aliases.get(key, key)


async def _llm_option_for_slot(
    slot: ChartSlot,
    findings: Sequence[Finding],
    context: Mapping[str, Any],
) -> Optional[ChartOption]:
    if not context.get("auth_config"):
        return None
    llm_context = {
        "execution_id": context.get("execution_id"),
        "auth_config": context.get("auth_config"),
        "ssl_config": context.get("ssl_config"),
    }
    try:
        response = await complete_with_tools(
            messages=[
                {"role": "system", "content": _chart_slot_worker_system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "slot": slot.model_dump(mode="json"),
                            "research_payload": _research_payload(findings),
                            "chart_schema": _chart_slot_tool()["function"]["parameters"],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            tools=[_chart_slot_tool()],
            context=llm_context,
            llm_params={
                "temperature": 0,
                "max_tokens": 1800,
                "tool_choice": {"type": "function", "function": {"name": "submit_chart_slot"}},
            },
        )
        chart = _extract_slot_chart_arguments(response)
        options = validate_planner_chart_plan({"charts": [chart]}, findings, slot.intent)
        if not options:
            return None
        option = options[0]
        if option.chart_type != slot.chart_type:
            return None
        return option
    except Exception as exc:  # pylint: disable=broad-exception-caught
        get_logger().warning(
            "chart_slot.llm_failed",
            execution_id=context.get("execution_id"),
            chart_id=slot.slot_id,
            error=str(exc),
        )
        return None


def _chart_slot_worker_system_prompt() -> str:
    return (
        "You build one source-grounded chart JSON spec for Aegis. Use only the provided "
        "research payload. Every point must cite existing evidence_ids. Do not invent "
        "values, labels, banks, periods, or units. If the slot asks for mixed metrics, "
        "use small_multiple_panel only. Call submit_chart_slot exactly once."
    )


def _chart_slot_tool() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "submit_chart_slot",
            "description": "Return one source-grounded chart spec for a chart slot.",
            "parameters": {
                "type": "object",
                "properties": {
                    "chart": {
                        "type": "object",
                        "properties": {
                            "chart_type": {"type": "string"},
                            "title": {"type": "string"},
                            "subtitle": {"type": "string"},
                            "metric_name": {"type": "string"},
                            "unit": {"type": "string"},
                            "x_label": {"type": "string"},
                            "y_label": {"type": "string"},
                            "value_format": {"type": "object"},
                            "points": {"type": "array", "items": {"type": "object"}},
                            "series": {"type": "array", "items": {"type": "object"}},
                            "annotations": {"type": "array", "items": {"type": "object"}},
                            "rationale": {"type": "string"},
                        },
                        "required": [
                            "chart_type",
                            "title",
                            "subtitle",
                            "metric_name",
                            "x_label",
                            "y_label",
                            "rationale",
                        ],
                        "additionalProperties": False,
                    }
                },
                "required": ["chart"],
                "additionalProperties": False,
            },
        },
    }


def _extract_slot_chart_arguments(response: Mapping[str, Any]) -> Dict[str, Any]:
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("chart slot worker returned no choices")
    message = choices[0].get("message") or {}
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        if function.get("name") != "submit_chart_slot":
            continue
        raw_args = function.get("arguments") or "{}"
        parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("chart"), Mapping):
            raise ValueError("chart slot worker returned invalid arguments")
        return dict(parsed["chart"])
    raise ValueError("chart slot worker did not call submit_chart_slot")


def _research_payload(findings: Sequence[Finding]) -> List[Dict[str, Any]]:
    payload = []
    for index, finding in enumerate(findings, start=1):
        item: Dict[str, Any] = {
            "finding_id": f"F{index}",
            "combo_label": finding.combo_label,
            "finding_type": finding.finding_type,
            "summary": finding.summary,
            "details": finding.details,
            "evidence_ids": [ref.evidence_id for ref in finding.evidence_refs if ref.evidence_id],
        }
        if finding.metric is not None:
            item["metric"] = finding.metric.model_dump(mode="json")
        if finding.table is not None:
            item["table"] = finding.table.model_dump(mode="json")
        payload.append(item)
    return payload


def _artifact_is_supported(option: ChartOption, evidence_registry: Mapping[str, Any]) -> bool:
    valid_evidence_ids = _valid_registry_evidence_ids(evidence_registry)
    return bool(option.evidence_ids) and set(option.evidence_ids).issubset(valid_evidence_ids)


def _valid_registry_evidence_ids(evidence_registry: Mapping[str, Any]) -> set[str]:
    evidence_ids: set[str] = set()
    for source_group in evidence_registry.values():
        if isinstance(source_group, Mapping):
            evidence_ids.update(str(eid) for eid in source_group.keys())
    return evidence_ids
