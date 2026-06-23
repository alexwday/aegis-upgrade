"""Typed contracts for the V2 analyst workstation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .sources import SOURCE_IDS, SOURCE_LABELS, source_label


EventType = Literal[
    "session.ready",
    "chat.message",
    "chat.delta",
    "final_response.started",
    "tool.started",
    "tool.progress",
    "tool.completed",
    "tool.failed",
    "widget.created",
    "widget.updated",
    "widget.completed",
    "widget.failed",
    "preview.open",
    "artifact.created",
    "artifact.updated",
]
WidgetStatus = Literal["pending", "running", "complete", "failed"]
PreviewKind = Literal["source_document", "artifact", "html", "empty"]
ArtifactKind = Literal[
    "html_report",
    "availability_report",
    "research_report",
    "quick_search",
    "deep_search",
    "report",
]
ChatRole = Literal["system", "user", "assistant", "tool"]
ModelSelection = Literal["small", "large"]
SearchSelection = Literal["quick", "deep"]


class V2BaseModel(BaseModel):
    """Base model that rejects accidental wire-shape drift."""

    model_config = ConfigDict(extra="forbid")


class WidgetAction(V2BaseModel):
    """Structured client action exposed by trusted widgets."""

    id: str
    label: str
    action_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class HtmlWidget(V2BaseModel):
    """Trusted HTML widget rendered in the chat surface."""

    id: str = Field(default_factory=lambda: f"widget_{uuid4().hex}")
    kind: str
    title: str
    status: WidgetStatus = "pending"
    html: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)
    actions: List[WidgetAction] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PreviewTarget(V2BaseModel):
    """Document or artifact target shown in the preview pane."""

    kind: PreviewKind
    title: str
    url: Optional[str] = None
    source_id: Optional[str] = None
    file_id: Optional[str] = None
    artifact_id: Optional[str] = None
    location: Optional[str] = None


class V2QuerySelection(V2BaseModel):
    """Bank, period, and source selections sent with a V2 query."""

    data_sources: List[str] = Field(default_factory=list)
    bank_tickers: List[str] = Field(default_factory=list)
    bank_categories: List[str] = Field(default_factory=list)
    fiscal_years: List[int] = Field(default_factory=list)
    quarters: List[str] = Field(default_factory=list)
    keyword: Optional[str] = None

    @field_validator("quarters")
    @classmethod
    def normalize_quarters(cls, value: List[str]) -> List[str]:
        """Normalize quarter labels to Q1-Q4."""
        normalized: list[str] = []
        for item in value:
            quarter = str(item).strip().upper()
            if quarter in {"1", "2", "3", "4"}:
                quarter = f"Q{quarter}"
            if quarter not in {"Q1", "Q2", "Q3", "Q4"}:
                raise ValueError("quarters must contain only Q1, Q2, Q3, or Q4")
            if quarter not in normalized:
                normalized.append(quarter)
        return normalized


class V2QueryRequest(V2BaseModel):
    """Canonical client-to-agent request sent over /api/v2/ws."""

    user_id: str
    conversation_id: Optional[str] = None
    query: str
    filters: V2QuerySelection = Field(default_factory=V2QuerySelection)
    optional_context: V2QuerySelection = Field(default_factory=V2QuerySelection)
    model_selection: ModelSelection = "small"
    search_selection: SearchSelection = "quick"


class Artifact(V2BaseModel):
    """Session artifact available from the artifact pane."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    kind: ArtifactKind
    title: str
    html: str
    source_widget_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatConversationSummary(V2BaseModel):
    """One persisted chat conversation."""

    conversation_id: str
    user_id: str
    conversation_title: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ChatMessageRecord(V2BaseModel):
    """One persisted chat message compatible with the V2 chat surface."""

    id: str
    role: ChatRole
    content: str


class ClarificationResolutionRequest(V2BaseModel):
    """Request to replace a persisted clarification widget with its question."""

    widget_id: str
    question: str


class RuntimeResetResponse(V2BaseModel):
    """Result of clearing persisted runtime chat state for one user."""

    user_id: str
    deleted_conversations: int = 0


class ChatHistoryMessageItem(V2BaseModel):
    """One visible chat message item in a hydrated conversation transcript."""

    type: Literal["message"] = "message"
    message: ChatMessageRecord


class ChatHistoryWidgetItem(V2BaseModel):
    """One persisted widget item in a hydrated conversation transcript."""

    type: Literal["widget"] = "widget"
    widget: HtmlWidget


ChatHistoryItem = ChatHistoryMessageItem | ChatHistoryWidgetItem


class ConversationDetailResponse(V2BaseModel):
    """Conversation, messages, and artifacts loaded from runtime tables."""

    conversation: ChatConversationSummary
    messages: List[ChatMessageRecord] = Field(default_factory=list)
    chat_items: List[ChatHistoryItem] = Field(default_factory=list)
    artifacts: List[Artifact] = Field(default_factory=list)


class BootstrapResponse(V2BaseModel):
    """Initial runtime payload for the V2 UI."""

    user_id: str
    active_conversation: Optional[ChatConversationSummary] = None
    messages: List[ChatMessageRecord] = Field(default_factory=list)
    chat_items: List[ChatHistoryItem] = Field(default_factory=list)
    recent_artifacts: List[Artifact] = Field(default_factory=list)


class ConversationListResponse(V2BaseModel):
    """Persisted conversations for one user."""

    conversations: List[ChatConversationSummary] = Field(default_factory=list)


class ArtifactListResponse(V2BaseModel):
    """Persisted artifacts for a conversation."""

    artifacts: List[Artifact] = Field(default_factory=list)


class SourceSummary(V2BaseModel):
    """Source display metadata and availability counts."""

    id: str
    label: str
    available_rows: int = 0


class DataSourceRegistryItem(V2BaseModel):
    """One registered source available to the V2 UI."""

    data_source_name: str
    data_source_display_name: str
    data_source_description: str


class DataSourceRegistryResponse(V2BaseModel):
    """Source registry payload for filter controls."""

    data_sources: List[DataSourceRegistryItem] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DataAvailabilityRow(V2BaseModel):
    """One bank-period row from aegis_data_availability."""

    bank_id: int
    bank_name: str
    bank_symbol: str
    bank_category: str
    bank_category_id: str
    bank_tags: List[str] = Field(default_factory=list)
    fiscal_year: int
    quarter: str
    source_ids: List[str] = Field(default_factory=list)
    last_refreshed_at: Optional[datetime] = None

    @field_validator("quarter")
    @classmethod
    def normalize_quarter(cls, value: str) -> str:
        """Normalize quarter labels to Q1-Q4."""
        quarter = str(value).strip().upper()
        if quarter in {"1", "2", "3", "4"}:
            quarter = f"Q{quarter}"
        if quarter not in {"Q1", "Q2", "Q3", "Q4"}:
            raise ValueError("quarter must be Q1, Q2, Q3, or Q4")
        return quarter


class DataAvailabilityGap(V2BaseModel):
    """Missing source coverage for a known bank-period row."""

    bank_symbol: str
    bank_name: str
    fiscal_year: int
    quarter: str
    missing_source_ids: List[str]


class AvailabilityFilters(V2BaseModel):
    """Filter inputs for availability reads."""

    source_ids: List[str] = Field(default_factory=list)
    bank_symbols: List[str] = Field(default_factory=list)
    bank_categories: List[str] = Field(default_factory=list)
    fiscal_years: List[int] = Field(default_factory=list)
    quarters: List[str] = Field(default_factory=list)
    keyword: Optional[str] = None
    limit: int = Field(default=500, ge=1, le=2000)


class DataAvailabilityResponse(V2BaseModel):
    """Complete availability payload for the V2 UI."""

    rows: List[DataAvailabilityRow] = Field(default_factory=list)
    missing: List[DataAvailabilityGap] = Field(default_factory=list)
    sources: List[SourceSummary] = Field(default_factory=list)
    bank_categories: List[str] = Field(default_factory=list)
    fiscal_years: List[int] = Field(default_factory=list)
    quarters: List[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DocumentFilters(V2BaseModel):
    """Filter inputs for source document browsing and search."""

    source_ids: List[str] = Field(default_factory=list)
    bank_symbols: List[str] = Field(default_factory=list)
    bank_categories: List[str] = Field(default_factory=list)
    fiscal_years: List[int] = Field(default_factory=list)
    quarters: List[str] = Field(default_factory=list)
    keyword: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=500)


class DocumentSummary(V2BaseModel):
    """One previewable source document."""

    source_id: str
    source_label: str
    file_id: str
    bank_symbol: str
    bank_category: str
    fiscal_year: str
    quarter: str
    filename: str
    file_type: str
    preview_url: str
    download_url: str
    preview_status: Literal["ready", "missing", "error"]
    preview_error: Optional[str] = None
    updated_at: Optional[datetime] = None


class DocumentSearchResponse(V2BaseModel):
    """Document browser/search payload."""

    documents: List[DocumentSummary] = Field(default_factory=list)
    total: int = 0
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReleaseEvent(V2BaseModel):
    """Calendar event surfaced in the release calendar widget."""

    id: str
    event_type: str
    title: str
    event_date: datetime
    bank_symbol: Optional[str] = None
    bank_name: Optional[str] = None
    bank_category: str = "Uncategorized"
    fiscal_year: Optional[int] = None
    quarter: Optional[str] = None
    source_id: Optional[str] = None


class ReleaseCalendarResponse(V2BaseModel):
    """Monthly release calendar payload."""

    month: str
    events: List[ReleaseEvent] = Field(default_factory=list)
    event_types: List[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReportSummary(V2BaseModel):
    """One pre-generated report from aegis_reports."""

    id: int
    title: str
    description: str
    report_type: str
    bank_id: int
    bank_name: str
    bank_symbol: str
    bank_category: str
    fiscal_year: int
    quarter: str
    generated_at: datetime
    preview_url: str
    download_url: str


class ReportSearchResponse(V2BaseModel):
    """Report downloader payload."""

    reports: List[ReportSummary] = Field(default_factory=list)
    report_types: List[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReportSubscription(V2BaseModel):
    """Report scheduler subscription placeholder until durable email workflow exists."""

    id: str = Field(default_factory=lambda: f"subscription_{uuid4().hex}")
    report_type: str
    scope_type: Literal["all_banks", "category", "bank"]
    bank_category: Optional[str] = None
    bank_symbol: Optional[str] = None
    delivery: Literal["email"] = "email"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ReportSubscriptionRequest(V2BaseModel):
    """Create/update request for one report subscription."""

    report_type: str
    scope_type: Literal["all_banks", "category", "bank"]
    bank_category: Optional[str] = None
    bank_symbol: Optional[str] = None


class ReportSubscriptionResponse(V2BaseModel):
    """Scheduler state payload."""

    report_types: List[str] = Field(default_factory=list)
    subscriptions: List[ReportSubscription] = Field(default_factory=list)


class ChatMessagePayload(V2BaseModel):
    """Payload for chat.message events."""

    role: ChatRole
    content: str


class V2Event(V2BaseModel):
    """Envelope streamed over the V2 websocket."""

    type: EventType
    session_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    event_id: str = Field(default_factory=lambda: f"event_{uuid4().hex}")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def source_summaries(rows: List[DataAvailabilityRow]) -> List[SourceSummary]:
    """Build source summaries in stable source order."""
    counts = {source_id: 0 for source_id in SOURCE_IDS}
    for row in rows:
        for source_id in row.source_ids:
            if source_id in counts:
                counts[source_id] += 1
    return [
        SourceSummary(
            id=source_id,
            label=source_label(source_id),
            available_rows=counts[source_id],
        )
        for source_id in SOURCE_LABELS
    ]
