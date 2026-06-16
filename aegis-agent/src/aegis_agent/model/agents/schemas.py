"""
Typed contracts for the multi-source Aegis agent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


ResearchStatus = Literal["success", "partial_success", "no_available_data", "error"]
ComboStatus = Literal["available", "unavailable", "complete", "incomplete", "error"]
SourceId = Literal[
    "transcripts",
    "event_transcripts",
    "investor_slides",
    "supplementary_financials",
    "rts",
    "pillar3",
]
DEFAULT_DOCUMENT_SOURCES: List[SourceId] = [
    "transcripts",
    "event_transcripts",
    "investor_slides",
    "supplementary_financials",
    "rts",
    "pillar3",
]
FinalRenderMode = Literal["default_brief", "custom", "no_available_data"]
FinalBodyStyle = Literal["default_brief", "user_requested_format"]
FindingType = Literal["quantitative", "qualitative", "table", "summary", "detailed"]


class BankPeriodCombination(BaseModel):
    """A bank and fiscal period requested for transcript research."""

    bank_id: Optional[str] = None
    bank_name: Optional[str] = None
    bank_symbol: Optional[str] = None
    fiscal_year: int
    quarter: str

    @field_validator("quarter")
    @classmethod
    def normalize_quarter(cls, value: str) -> str:
        """Normalize quarters to Q1-Q4."""
        quarter = str(value).upper().strip()
        if quarter in {"1", "2", "3", "4"}:
            quarter = f"Q{quarter}"
        if quarter not in {"Q1", "Q2", "Q3", "Q4"}:
            raise ValueError("quarter must be Q1, Q2, Q3, or Q4")
        return quarter

    @property
    def bank_label(self) -> str:
        """Return the best human-readable bank label."""
        return self.bank_symbol or self.bank_name or self.bank_id or "Unknown bank"

    @property
    def label(self) -> str:
        """Return a stable display label for this combination."""
        return f"{self.bank_label} {self.quarter} {self.fiscal_year}"


class ResearchRequest(BaseModel):
    """Tool input for document research."""

    question: str = Field(..., min_length=1)
    combinations: List[BankPeriodCombination] = Field(..., min_length=1)
    sources: List[SourceId] = Field(default_factory=lambda: list(DEFAULT_DOCUMENT_SOURCES))

    @field_validator("sources")
    @classmethod
    def normalize_sources(cls, value: List[SourceId]) -> List[SourceId]:
        """Deduplicate sources while preserving order."""
        seen = set()
        normalized: List[SourceId] = []
        for source in value or DEFAULT_DOCUMENT_SOURCES:
            if source not in seen:
                seen.add(source)
                normalized.append(source)
        return normalized or list(DEFAULT_DOCUMENT_SOURCES)


class EvidenceReference(BaseModel):
    """Canonical source reference for linked findings and final citations."""

    evidence_id: Optional[str] = None
    source_id: SourceId
    source_label: str
    filename: Optional[str] = None
    page_number: Optional[int] = None
    location_label: Optional[str] = None
    sheet_name: Optional[str] = None
    section_name: Optional[str] = None
    s3_key: Optional[str] = None
    href: Optional[str] = None
    display_label: str


class Citation(BaseModel):
    """Backward-compatible citation metadata the final agent can reference."""

    combo_label: str
    evidence_id: Optional[str] = None
    source_id: Optional[str] = None
    section_name: Optional[str] = None
    title: Optional[str] = None
    filename: Optional[str] = None
    page_number: Optional[int] = None
    href: Optional[str] = None
    display_label: Optional[str] = None
    text_excerpt: str


class MetricObservation(BaseModel):
    """Structured numeric observation extracted from source evidence."""

    metric_name: str = ""
    metric_value: str = ""
    unit: str = ""
    period: str = ""
    segment: str = ""


class ResearchTable(BaseModel):
    """Tabular evidence extracted or reconstructed from source evidence."""

    title: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    notes: Optional[str] = None


class Finding(BaseModel):
    """A structured research finding for one bank-period combination."""

    combo_label: str
    summary: str
    finding_type: FindingType = "qualitative"
    details: Optional[str] = None
    metric: Optional[MetricObservation] = None
    table: Optional[ResearchTable] = None
    support: Dict[str, Any] = Field(default_factory=dict)
    evidence_refs: List[EvidenceReference] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)


class Gap(BaseModel):
    """Unavailable or incomplete research coverage."""

    combo_label: str
    reason: str


class CoverageItem(BaseModel):
    """Coverage for a requested bank-period combination."""

    combo_label: str
    status: ComboStatus
    chunk_count: int = 0
    sections: List[str] = Field(default_factory=list)
    source: SourceId = "transcripts"


class ResearchResult(BaseModel):
    """Structured result returned by the research tool."""

    status: ResearchStatus
    quick_summary: str
    findings: List[Finding] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)
    evidence_registry: Dict[str, Dict[str, EvidenceReference]] = Field(default_factory=dict)
    gaps: List[Gap] = Field(default_factory=list)
    coverage: List[CoverageItem] = Field(default_factory=list)
    dropdown_markdown: str = ""


class FinalResponseSummary(BaseModel):
    """Optional top summary declared before final body streaming starts."""

    headline: str = Field(..., min_length=1)
    dek: Optional[str] = None
    eyebrow: str = "Aegis research brief"


class FinalResponseTile(BaseModel):
    """Optional metric tile declared by the agent for the final response shell."""

    label: str = Field(..., min_length=1)
    value: str = Field(..., min_length=1)
    context: Optional[str] = None
    evidence_ids: List[str] = Field(default_factory=list)


class FinalResponseShell(BaseModel):
    """Declared layout shell for the streamed final response body."""

    render_mode: FinalRenderMode = "custom"
    summary: Optional[FinalResponseSummary] = None
    tiles: List[FinalResponseTile] = Field(default_factory=list, max_length=4)
    body_style: FinalBodyStyle = "user_requested_format"


class ProgressEvent(BaseModel):
    """Internal progress log entry for research status reporting."""

    source: str
    stage: str
    status: str
    message: str
    combo_label: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChoiceOption(BaseModel):
    """One selectable option in a choice card."""

    id: str
    label: str
    description: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ChoiceCard(BaseModel):
    """Structured UI card for simple clarifying questions."""

    card_id: str
    question: str
    options: List[ChoiceOption] = Field(..., min_length=2, max_length=4)
    allow_free_text: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)
