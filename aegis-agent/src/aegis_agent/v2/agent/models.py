"""Internal contracts for the first V2 agent slice."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...utils.settings import config
from ..sources import SOURCE_IDS, normalize_source_ids


SearchMode = Literal["quick", "deep"]
ModelMode = Literal["small", "large"]


@dataclass(frozen=True)
class ModelPlan:
    """Resolved model tiers for one UI model selection."""

    ui_model: ModelMode
    orchestrator_tier: Literal["medium", "large"]
    research_tier: Literal["small", "medium"]
    orchestrator_model: str
    research_model: str


@dataclass(frozen=True)
class NormalizedTurn:
    """One normalized websocket turn."""

    content: str
    user_id: str | None
    conversation_id: str | None
    run_uuid: str | None
    source_ids: list[str] = field(default_factory=list)
    bank_symbols: list[str] = field(default_factory=list)
    bank_categories: list[str] = field(default_factory=list)
    fiscal_years: list[int] = field(default_factory=list)
    quarters: list[str] = field(default_factory=list)
    keyword: str | None = None
    source_filter_explicit: bool = False
    optional_context_selected: bool = False
    search_mode: SearchMode = "quick"
    model_mode: ModelMode = "small"
    model_plan: ModelPlan | None = None


class EvidenceChunk(BaseModel):
    """Source evidence normalized across current V1 retrieval tables."""

    source_name: str
    source_display_name: str
    bank_ticker: str | None = None
    fiscal_year: int | None = None
    quarter: str | None = None
    file_id: str | None = None
    file_name: str | None = None
    page_number: int | None = None
    sheet_name: str | None = None
    section_name: str | None = None
    chunk_id: str
    chunk_content: str
    score: float | None = None
    reference_payload: dict[str, Any] = Field(default_factory=dict)


def evidence_id_for_chunk(chunk: EvidenceChunk) -> str:
    """Return the stable evidence id for a quick-search chunk."""
    return f"{chunk.source_name}:{chunk.chunk_id}"


class FinalResponseSummary(BaseModel):
    """Final response summary shell reused from the V1 response pattern."""

    headline: str
    dek: str | None = None
    eyebrow: str = "Aegis research brief"


class FinalResponseTile(BaseModel):
    """Metric tile shown above the streamed final answer."""

    label: str
    value: str
    context: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class FinalResponseShell(BaseModel):
    """Declared layout shell streamed before the final body."""

    render_mode: str = "custom"
    summary: FinalResponseSummary | None = None
    tiles: list[FinalResponseTile] = Field(default_factory=list, max_length=4)
    body_style: str = "user_requested_format"


BANK_ALIAS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("RY-CA", re.compile(r"\b(?:rbc|ry(?:-ca)?|royal bank(?: of canada)?)\b", re.I)),
    ("TD-CA", re.compile(r"\b(?:td(?:-ca)?|td bank|toronto[- ]dominion)\b", re.I)),
    ("BMO-CA", re.compile(r"\b(?:bmo(?:-ca)?|bank of montreal)\b", re.I)),
    ("BNS-CA", re.compile(r"\b(?:bns(?:-ca)?|scotia(?:bank)?|bank of nova scotia)\b", re.I)),
    ("CM-CA", re.compile(r"\b(?:cibc|cm-ca|canadian imperial)\b", re.I)),
    ("NA-CA", re.compile(r"\b(?:national bank(?: of canada)?|nbc|na-ca)\b", re.I)),
)
QUARTER_RE = re.compile(r"\bQ([1-4])\b", re.I)
FISCAL_YEAR_RE = re.compile(r"\b(?:FY|fiscal\s+year\s+|fiscal\s+)?(20\d{2})\b", re.I)


def resolve_model_plan(model_selection: str | None) -> ModelPlan:
    """Map the UI model setting onto orchestrator and research tiers."""
    normalized = str(model_selection or "small").strip().lower()
    ui_model: ModelMode = "large" if normalized == "large" else "small"
    if ui_model == "large":
        return ModelPlan(
            ui_model="large",
            orchestrator_tier="large",
            research_tier="medium",
            orchestrator_model=config.llm.large.model,
            research_model=config.llm.medium.model,
        )
    return ModelPlan(
        ui_model="small",
        orchestrator_tier="medium",
        research_tier="small",
        orchestrator_model=config.llm.medium.model,
        research_model=config.llm.small.model,
    )


def normalize_search_mode(value: Any) -> SearchMode:
    """Normalize old and new search mode names."""
    normalized = str(value or "").strip().lower()
    if normalized in {"deep", "long"}:
        return "deep"
    return "quick"


def _string_list(value: Any) -> list[str]:
    """Return a deduplicated list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    result: list[str] = []
    for item in values:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result


def _int_list(value: Any) -> list[int]:
    """Return a deduplicated list of integer values."""
    result: list[int] = []
    for item in _string_list(value):
        try:
            parsed = int(item)
        except ValueError:
            continue
        if parsed not in result:
            result.append(parsed)
    return result


def _first_dict(*values: Any) -> dict[str, Any]:
    """Return the first dictionary among candidate payload fields."""
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def _has_any_scope_value(context: dict[str, Any], filters: dict[str, Any]) -> bool:
    """Return whether the user supplied optional bank/period context."""
    return bool(
        _string_list(
            context.get("bank_tickers")
            or context.get("bank_symbols")
            or filters.get("bank_tickers")
            or filters.get("bank_symbols")
        )
        or _string_list(context.get("bank_categories") or filters.get("bank_categories"))
        or _int_list(context.get("fiscal_years") or filters.get("fiscal_years"))
        or _string_list(context.get("quarters") or filters.get("quarters"))
    )


def _append_unique(values: list[Any], item: Any) -> None:
    """Append an item while preserving the original order."""
    if item not in values:
        values.append(item)


def _infer_bank_symbols(content: str) -> list[str]:
    """Infer canonical bank tickers from explicit text aliases."""
    symbols: list[str] = []
    for symbol, pattern in BANK_ALIAS_PATTERNS:
        if pattern.search(content):
            _append_unique(symbols, symbol)
    return symbols


def _infer_fiscal_years(content: str) -> list[int]:
    """Infer explicit fiscal years from text."""
    years: list[int] = []
    for match in FISCAL_YEAR_RE.finditer(content):
        _append_unique(years, int(match.group(1)))
    return years


def _infer_quarters(content: str) -> list[str]:
    """Infer explicit fiscal quarters from text."""
    quarters: list[str] = []
    for match in QUARTER_RE.finditer(content):
        _append_unique(quarters, f"Q{match.group(1)}")
    return quarters


def normalize_turn(payload: dict[str, Any]) -> NormalizedTurn:
    """Normalize the websocket payload while accepting the transition contract."""
    content = str(payload.get("query") or payload.get("content") or "").strip()
    filters = _first_dict(payload.get("filters"))
    optional_context = _first_dict(
        payload.get("optional_context"), payload.get("context")
    )
    preferences = _first_dict(payload.get("preferences"))

    source_values = (
        filters.get("data_sources")
        or filters.get("source_ids")
        or optional_context.get("data_sources")
        or optional_context.get("sources")
    )
    source_ids = normalize_source_ids(source_values)
    source_filter_explicit = source_values is not None and source_ids != list(SOURCE_IDS)
    optional_context_selected = _has_any_scope_value(optional_context, filters)
    bank_symbols = _string_list(
        optional_context.get("bank_tickers")
        or optional_context.get("bank_symbols")
        or filters.get("bank_tickers")
        or filters.get("bank_symbols")
    )
    if not bank_symbols:
        bank_symbols = _infer_bank_symbols(content)
    bank_categories = _string_list(
        optional_context.get("bank_categories") or filters.get("bank_categories")
    )
    fiscal_years = _int_list(
        optional_context.get("fiscal_years") or filters.get("fiscal_years")
    )
    if not fiscal_years:
        fiscal_years = _infer_fiscal_years(content)
    quarters = [
        quarter.upper()
        for quarter in _string_list(
            optional_context.get("quarters") or filters.get("quarters")
        )
    ]
    if not quarters:
        quarters = _infer_quarters(content)
    search_mode = normalize_search_mode(
        payload.get("search_selection")
        or payload.get("search_mode")
        or preferences.get("search_mode")
        or preferences.get("research_depth")
    )
    model_selection = (
        payload.get("model_selection")
        or payload.get("model_mode")
        or preferences.get("model_mode")
        or ("small" if preferences.get("fast_mode") else None)
    )
    model_plan = resolve_model_plan(str(model_selection or "small"))
    return NormalizedTurn(
        content=content,
        user_id=str(payload.get("user_id")) if payload.get("user_id") else None,
        conversation_id=(
            str(payload.get("conversation_id"))
            if payload.get("conversation_id")
            else None
        ),
        run_uuid=str(payload.get("run_uuid")) if payload.get("run_uuid") else None,
        source_ids=source_ids,
        bank_symbols=[symbol.upper() for symbol in bank_symbols],
        bank_categories=bank_categories,
        fiscal_years=fiscal_years,
        quarters=quarters,
        keyword=str(filters.get("keyword") or "").strip() or None,
        source_filter_explicit=source_filter_explicit,
        optional_context_selected=optional_context_selected,
        search_mode=search_mode,
        model_mode=model_plan.ui_model,
        model_plan=model_plan,
    )
