"""Chart option generation and JSON chart artifacts for Aegis answers."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from ...connections.llm_connector import complete_with_tools
from ...utils.logging import get_logger
from ...utils.prompt_loader import load_prompt_from_db
from .schemas import Finding, ResearchTable


MAX_CHART_OPTIONS = 6
MAX_PLANNED_CHARTS = 4
MAX_PLANNER_SERIES = 6
MAX_PLANNER_POINTS = 40
CHART_PLANNER_LAYER = "aegis_agent"
CHART_PLANNER_NAME = "chart_planner"

QUARTER_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
SOURCE_PREFIX_RE = re.compile(r"^[A-Za-z][A-Za-z 0-9&-]*:\s+")
COMBO_RE = re.compile(r"^(?P<bank>.+?)\s+(?P<quarter>Q[1-4])\s+(?P<year>\d{4})$")
NUMERIC_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
PERIOD_RE = re.compile(r"\b(?P<quarter>Q[1-4])\s*(?:FY)?(?P<year>20\d{2})\b", re.I)
BANK_LABEL_RE = re.compile(r"\b(?:RY|RBC|TD|BMO|BNS|CM|CIBC|NA|NBC)(?:-CA)?\b", re.I)
MIX_LABEL_RE = re.compile(r"\b(?:mix|share|composition|percent|percentage|%)\b", re.I)
BRIDGE_LABEL_RE = re.compile(
    r"\b(?:bridge|walk|movement|reconciliation|rollforward|roll-forward)\b",
    re.I,
)
SUPPORTED_PLANNER_CHART_TYPES = {
    "peer_rank_bar",
    "trend_line",
    "trend_bar",
    "multi_series_line",
    "slopegraph",
    "delta_bar",
    "composition_stacked_bar",
    "composition_100_bar",
    "waterfall",
    "scatter_plot",
    "small_multiple_panel",
    "heatmap",
}
QUESTION_STOPWORDS = {
    "a",
    "about",
    "across",
    "all",
    "also",
    "and",
    "any",
    "are",
    "bmo",
    "bns",
    "bank",
    "banks",
    "between",
    "by",
    "can",
    "cibc",
    "cm",
    "chart",
    "compare",
    "comparison",
    "create",
    "data",
    "for",
    "from",
    "graph",
    "how",
    "in",
    "is",
    "me",
    "metric",
    "metrics",
    "na",
    "nbc",
    "of",
    "on",
    "performance",
    "period",
    "q1",
    "q2",
    "q3",
    "q4",
    "ratio",
    "rbc",
    "result",
    "results",
    "royal",
    "ry",
    "scotia",
    "scotiabank",
    "show",
    "summarize",
    "td",
    "tell",
    "the",
    "to",
    "trend",
    "us",
    "value",
    "values",
    "versus",
    "vs",
    "what",
    "with",
    "year",
}
QUESTION_ALIASES = {
    "capital": {"capital", "cet1", "rwa", "leverage", "tier", "basel"},
    "cet1": {"capital", "cet1", "tier"},
    "rwa": {"capital", "rwa", "risk", "weighted", "assets"},
    "credit": {"credit", "pcl", "provision", "provisions", "allowance", "impaired", "delinquency"},
    "quality": {"quality", "credit", "pcl", "impaired", "delinquency"},
    "pcl": {"pcl", "provision", "provisions", "credit", "loss", "losses", "quality"},
    "revenue": {"revenue", "sales", "income"},
    "earnings": {"earnings", "income", "profit", "profitability", "roe", "eps"},
    "roe": {"roe", "return", "equity", "profitability"},
    "roa": {"roa", "return", "assets", "profitability"},
    "eps": {"eps", "earnings", "share"},
    "profitability": {"profitability", "roe", "roa", "margin", "income"},
    "expense": {"expense", "expenses", "cost", "efficiency"},
    "expenses": {"expense", "expenses", "cost", "efficiency"},
    "efficiency": {"efficiency", "expense", "expenses", "cost"},
    "margin": {"margin", "nim", "spread"},
    "nim": {"nim", "net", "interest", "margin", "spread"},
    "deposit": {"deposit", "deposits", "funding"},
    "deposits": {"deposit", "deposits", "funding"},
    "loan": {"loan", "loans", "lending", "growth"},
    "loans": {"loan", "loans", "lending", "growth"},
}

DEFAULT_CHART_PLANNER_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "submit_chart_plan",
        "description": "Return planner-authored source-grounded chart specs for the final Aegis answer.",
        "parameters": {
            "type": "object",
            "properties": {
                "charts": {
                    "type": "array",
                    "maxItems": MAX_PLANNED_CHARTS,
                    "items": {
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
                    },
                }
            },
            "required": ["charts"],
            "additionalProperties": False,
        },
    },
}


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
    series: List[Dict[str, Any]] = Field(default_factory=list)
    points: List[Dict[str, Any]] = Field(default_factory=list)
    encoding: Dict[str, Any] = Field(default_factory=dict)
    annotations: List[Dict[str, Any]] = Field(default_factory=list)
    baseline: Optional[float] = None
    value_format: Dict[str, Any] = Field(default_factory=dict)
    source_kind: str = "metric_facts"


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
    rationale: str = ""
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
    rationale: str = ""
    status: str = "ready"


@dataclass
class ChartCandidate:
    """A scored chart option before display IDs are assigned."""

    score: float
    option: ChartOption


async def plan_chart_options(
    findings: Sequence[Finding],
    question: str,
    context: Dict[str, Any],
) -> List[ChartOption]:
    """Ask the chart planner LLM to author chart specs, then validate them."""
    logger = get_logger()
    if not findings:
        return []
    if not context.get("auth_config"):
        logger.info(
            "chart_planner.skipped_no_auth",
            execution_id=context.get("execution_id"),
        )
        return []

    try:
        prompt_data = load_prompt_from_db(
            CHART_PLANNER_LAYER,
            CHART_PLANNER_NAME,
            compose_with_globals=False,
            execution_id=context.get("execution_id"),
        )
        system_prompt = str(prompt_data.get("system_prompt") or "").strip()
        user_prompt = str(prompt_data.get("user_prompt") or "").strip()
        if not system_prompt or not user_prompt:
            raise ValueError("chart planner prompt requires system_prompt and user_prompt")
        tools = _planner_tools(prompt_data.get("tool_definition"))
        response = await complete_with_tools(
            messages=[
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": _render_planner_user_prompt(user_prompt, question, findings),
                },
            ],
            tools=tools,
            context=context,
            llm_params={
                "temperature": 0,
                "max_tokens": 2800,
                "tool_choice": {
                    "type": "function",
                    "function": {"name": "submit_chart_plan"},
                },
            },
        )
        plan = _extract_planner_tool_arguments(response)
        options = validate_planner_chart_plan(plan, findings, question)
        logger.info(
            "chart_planner.completed",
            execution_id=context.get("execution_id"),
            requested=len(plan.get("charts") or []),
            approved=len(options),
        )
        return options
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "chart_planner.failed",
            execution_id=context.get("execution_id"),
            error=str(exc),
        )
        return []


def validate_planner_chart_plan(
    plan: Mapping[str, Any],
    findings: Sequence[Finding],
    question: str,
) -> List[ChartOption]:
    """Validate planner-authored charts against current-turn evidence and numbers."""
    charts = plan.get("charts") if isinstance(plan, Mapping) else None
    if not isinstance(charts, list):
        return []

    valid_evidence_ids = _valid_evidence_ids(findings)
    trace_numbers = _trace_numbers_by_evidence(findings)
    options: List[ChartOption] = []

    for raw_chart in charts:
        if len(options) >= MAX_PLANNED_CHARTS:
            break
        if not isinstance(raw_chart, Mapping):
            continue
        option = _validated_planner_chart(
            raw_chart,
            question=question,
            valid_evidence_ids=valid_evidence_ids,
            trace_numbers=trace_numbers,
        )
        if option is not None:
            options.append(option)

    for index, option in enumerate(options, start=1):
        option.chart_id = f"C{index}"
    return options


def _planner_tools(raw_tool_definition: Any) -> List[Dict[str, Any]]:
    if isinstance(raw_tool_definition, Mapping):
        return [dict(raw_tool_definition)]
    if isinstance(raw_tool_definition, list) and raw_tool_definition:
        return [dict(tool) for tool in raw_tool_definition if isinstance(tool, Mapping)]
    return [DEFAULT_CHART_PLANNER_TOOL]


def _render_planner_user_prompt(
    template: str,
    question: str,
    findings: Sequence[Finding],
) -> str:
    payload = {
        "question": question,
        "available_chart_templates": sorted(SUPPORTED_PLANNER_CHART_TYPES),
        "research_findings": _planner_research_payload(findings),
    }
    rendered = template
    replacements = {
        "{{question}}": question,
        "{{chart_templates}}": json.dumps(payload["available_chart_templates"], ensure_ascii=False),
        "{{research_payload}}": json.dumps(payload["research_findings"], ensure_ascii=False),
        "{{planner_payload}}": json.dumps(payload, ensure_ascii=False),
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered


def _planner_research_payload(findings: Sequence[Finding]) -> List[Dict[str, Any]]:
    payload = []
    for index, finding in enumerate(findings, start=1):
        evidence_ids = _finding_evidence_ids(finding)
        if not evidence_ids:
            continue
        item: Dict[str, Any] = {
            "finding_id": f"F{index}",
            "combo_label": finding.combo_label,
            "finding_type": finding.finding_type,
            "summary": finding.summary,
            "details": finding.details,
            "evidence_ids": evidence_ids,
        }
        if finding.metric is not None:
            item["metric"] = finding.metric.model_dump(mode="json")
        if finding.table is not None:
            item["table"] = finding.table.model_dump(mode="json")
        payload.append(item)
    return payload


def _extract_planner_tool_arguments(response: Mapping[str, Any]) -> Dict[str, Any]:
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("chart planner returned no choices")
    message = choices[0].get("message") or {}
    for tool_call in message.get("tool_calls") or []:
        function = tool_call.get("function") or {}
        if function.get("name") != "submit_chart_plan":
            continue
        return _parse_json_object(function.get("arguments") or "{}")
    if message.get("content"):
        return _parse_json_object(message["content"])
    raise ValueError("chart planner did not call submit_chart_plan")


def _parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        raise ValueError("chart planner arguments must be JSON object text")
    stripped = value.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.I)
        stripped = re.sub(r"\s*```$", "", stripped)
    if not stripped.startswith("{"):
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            stripped = stripped[start : end + 1]
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("chart planner arguments must decode to an object")
    return parsed


def _validated_planner_chart(
    raw_chart: Mapping[str, Any],
    *,
    question: str,
    valid_evidence_ids: set[str],
    trace_numbers: Mapping[str, Sequence[float]],
) -> Optional[ChartOption]:
    chart_type = _clean_text_field(raw_chart.get("chart_type"), 60)
    if chart_type not in SUPPORTED_PLANNER_CHART_TYPES:
        return None

    title = _clean_text_field(raw_chart.get("title"), 120)
    subtitle = _clean_text_field(raw_chart.get("subtitle"), 160)
    metric_name = _clean_text_field(raw_chart.get("metric_name"), 100)
    x_label = _clean_text_field(raw_chart.get("x_label"), 60)
    y_label = _clean_text_field(raw_chart.get("y_label"), 60)
    rationale = _clean_text_field(raw_chart.get("rationale"), 260)
    if not all([title, subtitle, metric_name, x_label, y_label, rationale]):
        return None
    if not _chart_matches_question(raw_chart, question):
        return None

    points = _validated_points(
        raw_chart.get("points"),
        valid_evidence_ids=valid_evidence_ids,
        trace_numbers=trace_numbers,
    )
    series = _validated_series(
        raw_chart.get("series"),
        valid_evidence_ids=valid_evidence_ids,
        trace_numbers=trace_numbers,
    )
    points = points[:MAX_PLANNER_POINTS]
    series = series[:MAX_PLANNER_SERIES]
    if not _chart_has_minimum_data(chart_type, points, series):
        return None

    evidence_ids = _chart_evidence_ids(points, series)
    if not evidence_ids:
        return None

    unit = _clean_text_field(raw_chart.get("unit"), 30)
    spec = ChartSpec(
        chart_type=chart_type,
        metric_name=metric_name,
        unit=unit,
        x_label=x_label,
        y_label=y_label,
        points=points,
        series=series,
        annotations=_validated_annotations(raw_chart.get("annotations")),
        value_format=dict(raw_chart.get("value_format") or {})
        if isinstance(raw_chart.get("value_format"), Mapping)
        else {},
        source_kind="chart_planner",
    )
    return ChartOption(
        chart_id="C0",
        chart_type=chart_type,
        title=title,
        subtitle=subtitle,
        metric_name=metric_name,
        unit=unit,
        spec=spec,
        evidence_ids=evidence_ids,
        rationale=rationale,
    )


def _validated_points(
    raw_points: Any,
    *,
    valid_evidence_ids: set[str],
    trace_numbers: Mapping[str, Sequence[float]],
) -> List[Dict[str, Any]]:
    if not isinstance(raw_points, list):
        return []
    points = []
    for raw_point in raw_points:
        if not isinstance(raw_point, Mapping):
            continue
        point = _validated_point(
            raw_point,
            valid_evidence_ids=valid_evidence_ids,
            trace_numbers=trace_numbers,
        )
        if point is not None:
            points.append(point)
    return points


def _validated_series(
    raw_series: Any,
    *,
    valid_evidence_ids: set[str],
    trace_numbers: Mapping[str, Sequence[float]],
) -> List[Dict[str, Any]]:
    if not isinstance(raw_series, list):
        return []
    series = []
    for raw_item in raw_series:
        if not isinstance(raw_item, Mapping):
            continue
        name = _clean_text_field(raw_item.get("name"), 80)
        if not name:
            continue
        points = _validated_points(
            raw_item.get("points"),
            valid_evidence_ids=valid_evidence_ids,
            trace_numbers=trace_numbers,
        )
        if len(points) < 2:
            continue
        series.append(
            {
                "name": name,
                "unit": _clean_text_field(raw_item.get("unit"), 30),
                "points": points[:MAX_PLANNER_POINTS],
            }
        )
    return series


def _validated_point(
    raw_point: Mapping[str, Any],
    *,
    valid_evidence_ids: set[str],
    trace_numbers: Mapping[str, Sequence[float]],
) -> Optional[Dict[str, Any]]:
    evidence_ids = [
        str(evidence_id)
        for evidence_id in raw_point.get("evidence_ids") or []
        if str(evidence_id) in valid_evidence_ids
    ]
    if not evidence_ids:
        return None

    point: Dict[str, Any] = {"evidence_ids": sorted(set(evidence_ids))}
    for key in ("label", "group", "category", "period_label", "bank_label"):
        value = _clean_text_field(raw_point.get(key), 90)
        if value:
            point[key] = value
    if "is_total" in raw_point:
        point["is_total"] = bool(raw_point.get("is_total"))

    numeric_keys = ("value", "x", "y", "start", "end")
    numeric_values = {}
    for key in numeric_keys:
        if key not in raw_point:
            continue
        parsed = _coerce_float(raw_point.get(key))
        if parsed is None:
            continue
        numeric_values[key] = parsed
        point[key] = parsed

    if not numeric_values:
        return None
    if not _point_numbers_are_traceable(numeric_values, point["evidence_ids"], trace_numbers):
        return None
    if not any(key in point for key in ("label", "group", "category", "period_label", "bank_label")):
        point["label"] = "Observation"
    return point


def _point_numbers_are_traceable(
    numeric_values: Mapping[str, float],
    evidence_ids: Sequence[str],
    trace_numbers: Mapping[str, Sequence[float]],
) -> bool:
    for key, value in numeric_values.items():
        if key == "value" and _computed_delta_is_valid(numeric_values, evidence_ids, trace_numbers):
            continue
        if not _value_is_traceable(value, evidence_ids, trace_numbers):
            return False
    return True


def _computed_delta_is_valid(
    numeric_values: Mapping[str, float],
    evidence_ids: Sequence[str],
    trace_numbers: Mapping[str, Sequence[float]],
) -> bool:
    if not {"value", "start", "end"}.issubset(numeric_values):
        return False
    expected = numeric_values["end"] - numeric_values["start"]
    if abs(expected - numeric_values["value"]) > max(0.01, abs(expected) * 0.001):
        return False
    return _value_is_traceable(numeric_values["start"], evidence_ids, trace_numbers) and _value_is_traceable(
        numeric_values["end"], evidence_ids, trace_numbers
    )


def _value_is_traceable(
    value: float,
    evidence_ids: Sequence[str],
    trace_numbers: Mapping[str, Sequence[float]],
) -> bool:
    tolerance = max(0.01, abs(value) * 0.001)
    for evidence_id in evidence_ids:
        for traced in trace_numbers.get(evidence_id, []):
            if abs(traced - value) <= tolerance:
                return True
    return False


def _chart_has_minimum_data(
    chart_type: str,
    points: Sequence[Mapping[str, Any]],
    series: Sequence[Mapping[str, Any]],
) -> bool:
    series_point_count = sum(len(item.get("points") or []) for item in series)
    if chart_type in {"trend_line", "trend_bar", "peer_rank_bar"}:
        return len(points) >= 2 or series_point_count >= 2
    if chart_type in {"multi_series_line", "slopegraph", "small_multiple_panel"}:
        return len(series) >= 2 and series_point_count >= 4
    if chart_type == "delta_bar":
        return len(points) >= 1
    if chart_type in {"composition_stacked_bar", "composition_100_bar"}:
        return len(points) >= 2
    if chart_type == "waterfall":
        return len(points) >= 3
    if chart_type == "scatter_plot":
        return len(points) >= 4 and all("x" in point and "y" in point for point in points)
    if chart_type == "heatmap":
        return len(points) >= 4
    return False


def _chart_evidence_ids(
    points: Sequence[Mapping[str, Any]],
    series: Sequence[Mapping[str, Any]],
) -> List[str]:
    evidence_ids: set[str] = set()
    for point in points:
        evidence_ids.update(str(eid) for eid in point.get("evidence_ids") or [])
    for item in series:
        for point in item.get("points") or []:
            evidence_ids.update(str(eid) for eid in point.get("evidence_ids") or [])
    return sorted(evidence_ids)


def _validated_annotations(raw_annotations: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_annotations, list):
        return []
    annotations = []
    for item in raw_annotations[:6]:
        if not isinstance(item, Mapping):
            continue
        text = _clean_text_field(item.get("text"), 160)
        if text:
            annotations.append({"text": text})
    return annotations


def _chart_matches_question(raw_chart: Mapping[str, Any], question: str) -> bool:
    question_terms = _question_domain_terms(question)
    if not question_terms:
        return True
    chart_blob = " ".join(
        str(raw_chart.get(key) or "")
        for key in ("title", "subtitle", "metric_name", "x_label", "y_label", "rationale")
    ).lower()
    chart_terms = set(re.findall(r"[a-z][a-z0-9]+", chart_blob))
    return bool(question_terms.intersection(chart_terms))


def _question_domain_terms(question: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z][a-z0-9]+", str(question or "").lower())
        if token not in QUESTION_STOPWORDS and not re.fullmatch(r"20\d{2}", token)
    }
    expanded = set(tokens)
    for token in tokens:
        expanded.update(QUESTION_ALIASES.get(token, set()))
    return expanded


def _valid_evidence_ids(findings: Sequence[Finding]) -> set[str]:
    return {evidence_id for finding in findings for evidence_id in _finding_evidence_ids(finding)}


def _trace_numbers_by_evidence(findings: Sequence[Finding]) -> Dict[str, List[float]]:
    trace: Dict[str, List[float]] = {}
    for finding in findings:
        evidence_ids = _finding_evidence_ids(finding)
        if not evidence_ids:
            continue
        values = _traceable_numbers_for_finding(finding)
        for evidence_id in evidence_ids:
            trace.setdefault(evidence_id, []).extend(values)
    return trace


def _traceable_numbers_for_finding(finding: Finding) -> List[float]:
    values: List[float] = []
    texts = [finding.summary, finding.details or ""]
    if finding.metric is not None:
        texts.append(finding.metric.metric_value)
    if finding.table is not None:
        texts.append(finding.table.notes or "")
        for row in finding.table.rows:
            texts.extend(str(value or "") for value in row.values())
    for text in texts:
        values.extend(_parse_all_numeric(text))
    return values


def _parse_all_numeric(value: Any) -> List[float]:
    text = str(value or "").replace("$", "")
    values = []
    for match in NUMERIC_RE.finditer(text):
        if match.start() > 0 and text[match.start() - 1].lower() == "q":
            continue
        if re.fullmatch(r"20\d{2}", match.group(0)):
            continue
        parsed = _coerce_float(match.group(0))
        if parsed is not None:
            values.append(parsed)
    return values


def _coerce_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    parsed = _parse_numeric(value)
    return parsed


def _clean_text_field(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def chart_instruction_text(options: Sequence[ChartOption]) -> str:
    """Return compact instructions for the final-answer model."""
    if not options:
        return (
            "No chart-planner-approved chart options are available for this turn. "
            "Do not include chart markers and do not create ad hoc markdown charts."
        )
    payload = [
        {
            "chart_id": option.chart_id,
            "marker": f"[[CHART:{option.chart_id}]]",
            "chart_type": option.chart_type,
            "title": option.title,
            "subtitle": option.subtitle,
            "metric_name": option.metric_name,
            "unit": option.unit,
            "source_kind": option.spec.source_kind,
            "rationale": option.rationale,
            "evidence_ids": option.evidence_ids,
        }
        for option in options
    ]
    return (
        "Chart-planner-approved interactive chart options for this turn are listed below. "
        "The chart data is already fixed and validated; do not create or modify chart data. "
        "For quantitative research answers, insert relevant chart markers by default. "
        "Use every directly relevant chart when there are one to three options; when "
        "there are four or more options, choose the strongest two to four based on the user's "
        "question. Place each marker near the prose, bullets, or table it supports. "
        "Insert a chart inline only by writing its exact marker, for example "
        "`[[CHART:C1]]`, on its own line between paragraphs. Never invent chart IDs, "
        "chart data, or markdown/ascii chart substitutes. Do not suggest follow-up "
        "charts unless their chart_id is listed below.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def chart_instruction_text_from_artifacts(artifacts: Mapping[str, Any]) -> str:
    """Return chart instructions for previously approved chart artifacts."""
    payload = []
    for chart_id in sorted(artifacts):
        artifact = artifacts.get(chart_id)
        if not isinstance(artifact, Mapping):
            continue
        if str(artifact.get("status") or "ready") != "ready":
            continue
        spec = artifact.get("spec") if isinstance(artifact.get("spec"), Mapping) else {}
        payload.append(
            {
                "chart_id": str(artifact.get("chart_id") or chart_id),
                "marker": f"[[CHART:{artifact.get('chart_id') or chart_id}]]",
                "chart_type": str(artifact.get("chart_type") or spec.get("chart_type") or ""),
                "title": str(artifact.get("title") or ""),
                "subtitle": str(artifact.get("subtitle") or ""),
                "metric_name": str(spec.get("metric_name") or ""),
                "unit": str(spec.get("unit") or ""),
                "source_kind": str(spec.get("source_kind") or ""),
                "evidence_ids": list(artifact.get("evidence_ids") or []),
            }
        )

    if not payload:
        return (
            "No chart-planner-approved chart options are available for this turn. "
            "Do not include chart markers and do not create ad hoc markdown charts."
        )

    return (
        "Previously approved interactive chart options from recent research are listed "
        "below. If the user asks to create, show, switch to, or add a graph/chart based "
        "on the previous answer, satisfy that request by inserting the exact listed "
        "marker on its own line. For other follow-up synthesis, include these charts "
        "when they directly improve trend or comparison comprehension. Never invent "
        "chart IDs, chart data, or markdown/ascii chart substitutes.\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )


def build_chart_options(findings: Sequence[Finding], question: str = "") -> List[ChartOption]:
    """Build deterministic source-grounded chart candidates from structured findings."""
    facts = _dedupe_facts(_facts_from_findings(findings))
    groups = _group_facts(facts)
    candidates: List[ChartCandidate] = []

    for (_metric_key_value, unit), group in groups.items():
        _ = _metric_key_value
        if len(group) < 2:
            continue
        banks = sorted({fact.bank_label for fact in group})
        periods = sorted(
            {(fact.fiscal_year, fact.quarter, fact.period_label) for fact in group},
            key=lambda item: (item[0], QUARTER_ORDER.get(item[1], 0)),
        )
        if len(banks) >= 2 and len(periods) == 1:
            candidates.append(
                ChartCandidate(
                    _score_chart("peer_rank_bar", group, question),
                    _make_option(
                        "peer_rank_bar",
                        group,
                        title=f"{group[0].metric_name} peer ranking",
                        subtitle=f"{periods[0][2]} | {unit or 'reported value'}",
                        x_label=unit or "Reported value",
                        y_label="Bank",
                    ),
                )
            )
        elif len(banks) == 1 and len(periods) >= 2:
            candidates.append(
                ChartCandidate(
                    _score_chart("trend_line", group, question),
                    _make_option(
                        "trend_line",
                        group,
                        title=f"{banks[0]} {group[0].metric_name} trend",
                        subtitle=f"{periods[0][2]} to {periods[-1][2]} | {unit or 'reported value'}",
                        x_label="Period",
                        y_label=unit or "Reported value",
                    ),
                )
            )
            candidates.append(
                ChartCandidate(
                    _score_chart("trend_bar", group, question) - 12,
                    _make_option(
                        "trend_bar",
                        group,
                        title=f"{banks[0]} {group[0].metric_name} by period",
                        subtitle=f"{periods[0][2]} to {periods[-1][2]} | {unit or 'reported value'}",
                        x_label="Period",
                        y_label=unit or "Reported value",
                    ),
                )
            )
            if len(periods) == 2:
                candidates.append(
                    ChartCandidate(
                        _score_chart("delta_bar", group, question),
                        _make_delta_option(group, f"{banks[0]} {group[0].metric_name} change"),
                    )
                )
        elif len(banks) >= 2 and len(periods) >= 2:
            if len(periods) == 2:
                candidates.append(
                    ChartCandidate(
                        _score_chart("slopegraph", group, question),
                        _make_option(
                            "slopegraph",
                            group,
                            title=f"{group[0].metric_name} peer movement",
                            subtitle=f"{periods[0][2]} to {periods[-1][2]} | {unit or 'reported value'}",
                            x_label="Period",
                            y_label=unit or "Reported value",
                        ),
                    )
                )
                candidates.append(
                    ChartCandidate(
                        _score_chart("delta_bar", group, question) - 6,
                        _make_delta_option(group, f"{group[0].metric_name} change across peers"),
                    )
                )
            if len(periods) >= 3:
                candidates.append(
                    ChartCandidate(
                        _score_chart("multi_series_line", group, question),
                        _make_multi_series_option(group),
                    )
                )
            candidates.append(
                ChartCandidate(
                    _score_chart("heatmap", group, question)
                    + (12 if len(banks) * len(periods) >= 12 else -8),
                    _make_option(
                        "heatmap",
                        group,
                        title=f"{group[0].metric_name} by bank and period",
                        subtitle=f"{len(banks)} banks x {len(periods)} periods | {unit or 'reported value'}",
                        x_label="Period",
                        y_label="Bank",
                    ),
                )
            )
            latest_year, latest_quarter, latest_label = periods[-1]
            latest_group = [
                fact
                for fact in group
                if fact.fiscal_year == latest_year and fact.quarter == latest_quarter
            ]
            if len({fact.bank_label for fact in latest_group}) >= 2:
                candidates.append(
                    ChartCandidate(
                        _score_chart("peer_rank_bar", latest_group, question) - 10,
                        _make_option(
                            "peer_rank_bar",
                            latest_group,
                            title=f"{group[0].metric_name} latest peer ranking",
                            subtitle=f"{latest_label} | {unit or 'reported value'}",
                            x_label=unit or "Reported value",
                            y_label="Bank",
                        ),
                    )
                )

    candidates.extend(_scatter_candidates_from_facts(facts, question))
    candidates.extend(_small_multiple_candidates_from_facts(facts, question))
    candidates.extend(_table_chart_candidates(findings, question))

    filtered = _dedupe_candidates(
        candidate for candidate in candidates if candidate.option.evidence_ids
    )
    filtered = sorted(filtered, key=lambda candidate: candidate.score, reverse=True)
    options = [candidate.option for candidate in filtered[:MAX_CHART_OPTIONS]]
    for index, option in enumerate(options, start=1):
        option.chart_id = f"C{index}"
    return options


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
            rationale=option.rationale,
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


def _make_custom_option(
    chart_type: str,
    title: str,
    subtitle: str,
    metric_name: str,
    unit: str,
    x_label: str,
    y_label: str,
    evidence_ids: Sequence[str],
    *,
    facts: Optional[Sequence[MetricFact]] = None,
    series: Optional[Sequence[Dict[str, Any]]] = None,
    points: Optional[Sequence[Dict[str, Any]]] = None,
    encoding: Optional[Dict[str, Any]] = None,
    annotations: Optional[Sequence[Dict[str, Any]]] = None,
    baseline: Optional[float] = None,
    value_format: Optional[Dict[str, Any]] = None,
    source_kind: str = "metric_facts",
) -> ChartOption:
    """Build a chart option from richer chart-spec payloads."""
    spec = ChartSpec(
        chart_type=chart_type,
        metric_name=metric_name,
        unit=unit,
        x_label=x_label,
        y_label=y_label,
        facts=list(facts or []),
        series=list(series or []),
        points=list(points or []),
        encoding=encoding or {},
        annotations=list(annotations or []),
        baseline=baseline,
        value_format=value_format or {},
        source_kind=source_kind,
    )
    return ChartOption(
        chart_id="C0",
        chart_type=chart_type,
        title=title,
        subtitle=subtitle,
        metric_name=metric_name,
        unit=unit,
        spec=spec,
        evidence_ids=sorted(set(evidence_ids)),
    )


def _period_sort_key(label: str) -> Tuple[int, int, str]:
    match = PERIOD_RE.search(str(label or ""))
    if match:
        return (
            int(match.group("year")),
            QUARTER_ORDER.get(match.group("quarter").upper(), 0),
            str(label),
        )
    year_match = re.search(r"\b(20\d{2})\b", str(label or ""))
    if year_match:
        return (int(year_match.group(1)), 0, str(label))
    return (0, 0, str(label))


def _sort_facts(facts: Sequence[MetricFact]) -> List[MetricFact]:
    return sorted(
        facts,
        key=lambda fact: (fact.bank_label, fact.fiscal_year, QUARTER_ORDER.get(fact.quarter, 0)),
    )


def _series_from_facts(
    facts: Sequence[MetricFact],
    group_field: str,
) -> List[Dict[str, Any]]:
    groups: Dict[str, List[MetricFact]] = {}
    for fact in facts:
        label = str(getattr(fact, group_field) or "").strip() or "Series"
        groups.setdefault(label, []).append(fact)
    series = []
    for label, group in sorted(groups.items()):
        ordered = _sort_facts(group)
        series.append(
            {
                "name": label,
                "unit": ordered[0].unit if ordered else "",
                "points": [
                    {
                        "label": fact.period_label,
                        "period_label": fact.period_label,
                        "bank_label": fact.bank_label,
                        "value": fact.value,
                        "evidence_ids": fact.evidence_ids,
                    }
                    for fact in ordered
                ],
            }
        )
    return series


def _make_multi_series_option(facts: Sequence[MetricFact]) -> ChartOption:
    banks = sorted({fact.bank_label for fact in facts})
    periods = sorted({fact.period_label for fact in facts}, key=_period_sort_key)
    evidence_ids = sorted({evidence_id for fact in facts for evidence_id in fact.evidence_ids})
    return _make_custom_option(
        "multi_series_line",
        f"{facts[0].metric_name} peer trend",
        f"{len(banks)} banks across {len(periods)} periods | {facts[0].unit or 'reported value'}",
        facts[0].metric_name,
        facts[0].unit,
        "Period",
        facts[0].unit or "Reported value",
        evidence_ids,
        facts=_sort_facts(facts),
        series=_series_from_facts(facts, "bank_label"),
    )


def _make_delta_option(facts: Sequence[MetricFact], title: str) -> ChartOption:
    groups: Dict[str, List[MetricFact]] = {}
    for fact in facts:
        groups.setdefault(fact.bank_label, []).append(fact)
    points = []
    used_evidence_ids = set()
    for label, group in sorted(groups.items()):
        ordered = _sort_facts(group)
        if len(ordered) < 2:
            continue
        first = ordered[0]
        last = ordered[-1]
        used_evidence_ids.update(first.evidence_ids)
        used_evidence_ids.update(last.evidence_ids)
        points.append(
            {
                "label": label,
                "value": last.value - first.value,
                "start": first.value,
                "end": last.value,
                "start_label": first.period_label,
                "end_label": last.period_label,
                "evidence_ids": sorted(set(first.evidence_ids + last.evidence_ids)),
            }
        )
    subtitle = ""
    if points:
        subtitle = f"{points[0]['start_label']} to {points[0]['end_label']} | {facts[0].unit or 'reported value'}"
    return _make_custom_option(
        "delta_bar",
        title,
        subtitle,
        facts[0].metric_name,
        facts[0].unit,
        "Change",
        "Bank",
        sorted(used_evidence_ids),
        facts=_sort_facts(facts),
        points=points,
    )


def _score_chart(chart_type: str, facts: Sequence[MetricFact], question: str = "") -> float:
    """Score chart usefulness using data fit plus light query-intent hints."""
    banks = {fact.bank_label for fact in facts}
    periods = {fact.period_label for fact in facts}
    evidence_count = len({evidence_id for fact in facts for evidence_id in fact.evidence_ids})
    base = {
        "trend_line": 78,
        "trend_bar": 62,
        "peer_rank_bar": 82,
        "multi_series_line": 86,
        "slopegraph": 86,
        "delta_bar": 74,
        "heatmap": 70,
        "scatter_plot": 72,
        "small_multiple_panel": 68,
        "composition_stacked_bar": 76,
        "composition_100_bar": 76,
        "waterfall": 84,
    }.get(chart_type, 50)
    score = base + min(12, len(facts)) + min(8, evidence_count)
    if len(banks) >= 2:
        score += 4
    if len(periods) >= 3:
        score += 5
    question_lower = question.lower()
    intent_boosts = {
        "trend": ("trend", "over time", "qoq", "yoy", "trajectory"),
        "peer": ("peer", "compare", "comparison", "rank", "ranking"),
        "change": ("change", "delta", "increase", "decrease", "movement"),
        "mix": ("mix", "share", "composition", "segment"),
        "bridge": ("bridge", "walk", "movement", "reconciliation"),
    }
    if chart_type in {"trend_line", "trend_bar", "multi_series_line"} and any(
        term in question_lower for term in intent_boosts["trend"]
    ):
        score += 10
    if chart_type in {"peer_rank_bar", "slopegraph", "heatmap"} and any(
        term in question_lower for term in intent_boosts["peer"]
    ):
        score += 10
    if chart_type == "delta_bar" and any(
        term in question_lower for term in intent_boosts["change"]
    ):
        score += 10
    if chart_type.startswith("composition") and any(
        term in question_lower for term in intent_boosts["mix"]
    ):
        score += 10
    if chart_type == "waterfall" and any(
        term in question_lower for term in intent_boosts["bridge"]
    ):
        score += 10
    return score


def _scatter_candidates_from_facts(
    facts: Sequence[MetricFact],
    question: str,
) -> List[ChartCandidate]:
    metric_groups = _group_facts(facts)
    keys = list(metric_groups)
    candidates: List[ChartCandidate] = []
    for left_index, left_key in enumerate(keys):
        for right_key in keys[left_index + 1 :]:
            left_group = metric_groups[left_key]
            right_group = metric_groups[right_key]
            left_by_period_bank = {
                (fact.period_label, fact.bank_label): fact for fact in left_group
            }
            right_by_period_bank = {
                (fact.period_label, fact.bank_label): fact for fact in right_group
            }
            common_keys = sorted(set(left_by_period_bank).intersection(right_by_period_bank))
            periods = sorted({period for period, _bank in common_keys}, key=_period_sort_key)
            for period in periods:
                period_keys = [(p, bank) for p, bank in common_keys if p == period]
                if len(period_keys) < 4:
                    continue
                points = []
                evidence_ids = set()
                for period_key in period_keys:
                    left_fact = left_by_period_bank[period_key]
                    right_fact = right_by_period_bank[period_key]
                    evidence_ids.update(left_fact.evidence_ids)
                    evidence_ids.update(right_fact.evidence_ids)
                    points.append(
                        {
                            "label": left_fact.bank_label,
                            "x": left_fact.value,
                            "y": right_fact.value,
                            "x_unit": left_fact.unit,
                            "y_unit": right_fact.unit,
                            "evidence_ids": sorted(
                                set(left_fact.evidence_ids + right_fact.evidence_ids)
                            ),
                        }
                    )
                option = _make_custom_option(
                    "scatter_plot",
                    f"{left_group[0].metric_name} vs {right_group[0].metric_name}",
                    f"{period} | {len(points)} peer observations",
                    f"{left_group[0].metric_name} vs {right_group[0].metric_name}",
                    "",
                    left_group[0].metric_name,
                    right_group[0].metric_name,
                    sorted(evidence_ids),
                    points=points,
                    encoding={
                        "x_metric": left_group[0].metric_name,
                        "y_metric": right_group[0].metric_name,
                        "x_unit": left_group[0].unit,
                        "y_unit": right_group[0].unit,
                    },
                )
                candidates.append(
                    ChartCandidate(_score_chart("scatter_plot", left_group + right_group, question), option)
                )
                return candidates[:1]
    return candidates


def _small_multiple_candidates_from_facts(
    facts: Sequence[MetricFact],
    question: str,
) -> List[ChartCandidate]:
    by_bank: Dict[str, List[MetricFact]] = {}
    for fact in facts:
        by_bank.setdefault(fact.bank_label, []).append(fact)
    candidates = []
    for bank, group in sorted(by_bank.items()):
        by_metric = _group_facts(group)
        if len(by_metric) < 2:
            continue
        units = {unit for _metric, unit in by_metric}
        if len(units) < 2:
            continue
        series = []
        evidence_ids = set()
        for (_metric_key, unit), metric_group in list(by_metric.items())[:4]:
            ordered = _sort_facts(metric_group)
            if len(ordered) < 2:
                continue
            evidence_ids.update(eid for fact in ordered for eid in fact.evidence_ids)
            series.append(
                {
                    "name": ordered[0].metric_name,
                    "unit": unit,
                    "points": [
                        {
                            "label": fact.period_label,
                            "value": fact.value,
                            "evidence_ids": fact.evidence_ids,
                        }
                        for fact in ordered
                    ],
                }
            )
        if len(series) < 2:
            continue
        option = _make_custom_option(
            "small_multiple_panel",
            f"{bank} multi-metric trend",
            f"{len(series)} metrics with separate scales",
            "Multiple metrics",
            "",
            "Period",
            "Reported value",
            sorted(evidence_ids),
            series=series,
            source_kind="metric_facts",
        )
        candidates.append(ChartCandidate(_score_chart("small_multiple_panel", group, question), option))
        break
    return candidates


def _dedupe_candidates(candidates: Iterable[ChartCandidate]) -> List[ChartCandidate]:
    seen: set[Tuple[str, str, str, str]] = set()
    result = []
    for candidate in candidates:
        option = candidate.option
        key = (
            option.chart_type,
            _metric_key(option.metric_name),
            option.unit.lower(),
            _metric_key(option.title),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _finding_evidence_ids(finding: Finding) -> List[str]:
    return sorted(
        {
            str(reference.evidence_id)
            for reference in finding.evidence_refs
            if reference.evidence_id
        }
    )


def _table_chart_candidates(findings: Sequence[Finding], question: str) -> List[ChartCandidate]:
    candidates: List[ChartCandidate] = []
    for finding in findings:
        if finding.table is None:
            continue
        evidence_ids = _finding_evidence_ids(finding)
        if not evidence_ids:
            continue
        candidates.extend(_waterfall_candidate(finding, finding.table, evidence_ids, question))
        candidates.extend(_composition_candidates(finding, finding.table, evidence_ids, question))
        candidates.extend(_period_table_candidates(finding, finding.table, evidence_ids, question))
        candidates.extend(_bank_column_candidates(finding, finding.table, evidence_ids, question))
    return candidates


def _row_value(row: Mapping[str, Any], column: str) -> Any:
    if column in row:
        return row[column]
    for key, value in row.items():
        if str(key).strip().lower() == column.strip().lower():
            return value
    return None


def _numeric_columns(table: ResearchTable) -> List[str]:
    columns = table.columns
    numeric = []
    for column in columns:
        count = sum(
            1
            for row in table.rows
            if _parse_numeric(str(_row_value(row, column) or "")) is not None
        )
        if count >= max(1, min(2, len(table.rows))):
            numeric.append(column)
    return numeric


def _label_column(table: ResearchTable) -> Optional[str]:
    for column in table.columns:
        non_numeric = 0
        non_empty = 0
        for row in table.rows:
            value = str(_row_value(row, column) or "").strip()
            if value:
                non_empty += 1
                if _parse_numeric(value) is None:
                    non_numeric += 1
        if non_empty and non_numeric >= max(1, non_empty // 2):
            return column
    return table.columns[0] if table.columns else None


def _looks_period(value: str) -> bool:
    text = str(value or "")
    return bool(PERIOD_RE.search(text) or re.search(r"\b20\d{2}\b", text))


def _looks_bank(value: str) -> bool:
    return bool(BANK_LABEL_RE.search(str(value or "")))


def _unit_from_values(values: Sequence[Any]) -> str:
    text = " ".join(str(value or "") for value in values).lower()
    if "%" in text or "percent" in text:
        return "%"
    if "bps" in text or "basis point" in text:
        return "bps"
    if "$" in text:
        return "$"
    if re.search(r"\b\d+(?:\.\d+)?x\b", text):
        return "x"
    return ""


def _table_context(finding: Finding, table: ResearchTable) -> Tuple[str, str, str]:
    title = table.title or finding.summary or "Source table"
    combo = SOURCE_PREFIX_RE.sub("", finding.combo_label).strip()
    bank_label = combo.rsplit(" ", 2)[0] if combo else "Bank"
    return title, combo, bank_label


def _series_from_table_rows(
    table: ResearchTable,
    label_column: str,
    value_columns: Sequence[str],
) -> List[Dict[str, Any]]:
    series = []
    for row in table.rows:
        name = str(_row_value(row, label_column) or "").strip()
        if not name:
            continue
        points = []
        raw_values = []
        for column in value_columns:
            raw_value = _row_value(row, column)
            parsed = _parse_numeric(str(raw_value or ""))
            if parsed is None:
                continue
            raw_values.append(raw_value)
            points.append({"label": column, "value": parsed})
        if len(points) >= 2:
            series.append({"name": name, "unit": _unit_from_values(raw_values), "points": points})
    return series


def _period_table_candidates(
    finding: Finding,
    table: ResearchTable,
    evidence_ids: Sequence[str],
    question: str,
) -> List[ChartCandidate]:
    label_column = _label_column(table)
    if not label_column:
        return []
    period_columns = [column for column in table.columns if column != label_column and _looks_period(column)]
    if len(period_columns) < 2:
        return []
    title, combo, bank_label = _table_context(finding, table)
    ordered_periods = sorted(period_columns, key=_period_sort_key)
    series = _series_from_table_rows(table, label_column, ordered_periods)
    if not series:
        return []
    units = {item.get("unit", "") for item in series}
    source_kind = "table"
    if len(series) == 1:
        option = _make_custom_option(
            "trend_line",
            f"{bank_label} {series[0]['name']} trend",
            f"{ordered_periods[0]} to {ordered_periods[-1]} | table-derived",
            str(series[0]["name"]),
            str(series[0].get("unit") or ""),
            "Period",
            str(series[0].get("unit") or "Reported value"),
            evidence_ids,
            series=series,
            source_kind=source_kind,
        )
        return [ChartCandidate(_score_chart("trend_line", [], question) + 10, option)]
    chart_type = "small_multiple_panel" if len(units) > 1 else "multi_series_line"
    option = _make_custom_option(
        chart_type,
        f"{title} trend",
        f"{len(series)} series from {combo or 'source table'}",
        title,
        "" if len(units) > 1 else str(next(iter(units)) or ""),
        "Period",
        "Reported value",
        evidence_ids,
        series=series[:6],
        source_kind=source_kind,
    )
    return [ChartCandidate(_score_chart(chart_type, [], question) + 12, option)]


def _bank_column_candidates(
    finding: Finding,
    table: ResearchTable,
    evidence_ids: Sequence[str],
    question: str,
) -> List[ChartCandidate]:
    label_column = _label_column(table)
    if not label_column:
        return []
    bank_columns = [
        column
        for column in _numeric_columns(table)
        if column != label_column and _looks_bank(column)
    ]
    if len(bank_columns) < 2:
        return []
    title, combo, _bank_label = _table_context(finding, table)
    series = _series_from_table_rows(table, label_column, bank_columns)
    if not series:
        return []
    if len(series) == 1:
        raw_values = [_row_value(table.rows[0], column) for column in bank_columns]
        unit = _unit_from_values(raw_values)
        points = [
            {
                "label": column,
                "value": _parse_numeric(str(_row_value(table.rows[0], column) or "")),
            }
            for column in bank_columns
        ]
        points = [point for point in points if point["value"] is not None]
        if len(points) < 2:
            return []
        option = _make_custom_option(
            "peer_rank_bar",
            f"{series[0]['name']} peer ranking",
            combo or title,
            str(series[0]["name"]),
            unit,
            unit or "Reported value",
            "Bank",
            evidence_ids,
            points=points,
            source_kind="table",
        )
        return [ChartCandidate(_score_chart("peer_rank_bar", [], question) + 10, option)]
    option = _make_custom_option(
        "small_multiple_panel",
        f"{title} peer metrics",
        f"{len(series)} metrics with separate scales",
        title,
        "",
        "Bank",
        "Reported value",
        evidence_ids,
        series=series[:6],
        source_kind="table",
    )
    return [ChartCandidate(_score_chart("small_multiple_panel", [], question) + 8, option)]


def _composition_candidates(
    finding: Finding,
    table: ResearchTable,
    evidence_ids: Sequence[str],
    question: str,
) -> List[ChartCandidate]:
    label_column = _label_column(table)
    if not label_column:
        return []
    title, combo, _bank_label = _table_context(finding, table)
    label_text = f"{label_column} {title}"
    if not re.search(r"\b(segment|category|business|division|geography|portfolio|mix|share)\b", label_text, re.I):
        return []
    numeric_columns = [column for column in _numeric_columns(table) if column != label_column]
    if not numeric_columns or len(table.rows) < 2:
        return []
    points = []
    raw_values = []
    for column in numeric_columns[:4]:
        for row in table.rows:
            category = str(_row_value(row, label_column) or "").strip()
            raw_value = _row_value(row, column)
            value = _parse_numeric(str(raw_value or ""))
            if not category or value is None or value < 0:
                continue
            raw_values.append(raw_value)
            points.append({"group": column, "category": category, "value": value})
    if len(points) < 2:
        return []
    grouped_totals: Dict[str, float] = {}
    for point in points:
        grouped_totals[point["group"]] = grouped_totals.get(point["group"], 0.0) + point["value"]
    unit = _unit_from_values(raw_values)
    normalize = unit == "%" or MIX_LABEL_RE.search(label_text) or all(
        95 <= total <= 105 for total in grouped_totals.values()
    )
    chart_type = "composition_100_bar" if normalize else "composition_stacked_bar"
    option = _make_custom_option(
        chart_type,
        f"{title} composition",
        combo or f"{len(grouped_totals)} group(s)",
        title,
        unit,
        "Group",
        unit or "Reported value",
        evidence_ids,
        points=points,
        source_kind="table",
    )
    return [ChartCandidate(_score_chart(chart_type, [], question) + 14, option)]


def _waterfall_candidate(
    finding: Finding,
    table: ResearchTable,
    evidence_ids: Sequence[str],
    question: str,
) -> List[ChartCandidate]:
    label_column = _label_column(table)
    if not label_column:
        return []
    title, combo, _bank_label = _table_context(finding, table)
    labels = [str(_row_value(row, label_column) or "").strip() for row in table.rows]
    label_blob = " ".join([title] + labels)
    bridge_like = BRIDGE_LABEL_RE.search(label_blob) or re.search(
        r"\b(beginning|ending|opening|closing|total|net|movement|change|impact)\b",
        label_blob,
        re.I,
    )
    numeric_columns = [column for column in _numeric_columns(table) if column != label_column]
    if not bridge_like or not numeric_columns or len(table.rows) < 3:
        return []
    value_column = numeric_columns[0]
    points = []
    raw_values = []
    for index, row in enumerate(table.rows):
        label = str(_row_value(row, label_column) or "").strip()
        raw_value = _row_value(row, value_column)
        value = _parse_numeric(str(raw_value or ""))
        if not label or value is None:
            continue
        raw_values.append(raw_value)
        is_total = bool(
            index == 0
            or index == len(table.rows) - 1
            or re.search(r"\b(total|ending|closing|balance)\b", label, re.I)
        )
        points.append({"label": label, "value": value, "is_total": is_total})
    if len(points) < 3:
        return []
    unit = _unit_from_values(raw_values)
    option = _make_custom_option(
        "waterfall",
        f"{title} bridge",
        combo or value_column,
        title,
        unit,
        "Driver",
        unit or "Reported value",
        evidence_ids,
        points=points,
        source_kind="table",
    )
    return [ChartCandidate(_score_chart("waterfall", [], question) + 16, option)]


def _metric_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _alt_text(option: ChartOption) -> str:
    observation_count = (
        len(option.spec.facts)
        or len(option.spec.points)
        or sum(len(series.get("points", [])) for series in option.spec.series)
    )
    return (
        f"{option.title}. {option.chart_type.replace('_', ' ')} showing "
        f"{option.metric_name} for {observation_count} source-grounded observations."
    )
