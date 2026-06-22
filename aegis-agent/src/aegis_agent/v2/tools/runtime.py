"""Runtime table reads for V2 chat conversations and artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from ...connections.postgres_connector import execute_query, fetch_all, fetch_one
from ..agent.conversation import (
    WIDGET_MARKER_CLOSE,
    WIDGET_MARKER_OPEN,
    ConversationContext,
    build_conversation_context,
)
from ..schemas import (
    Artifact,
    ArtifactListResponse,
    BootstrapResponse,
    ChatHistoryItem,
    ChatHistoryMessageItem,
    ChatHistoryWidgetItem,
    ChatConversationSummary,
    ChatMessageRecord,
    ConversationDetailResponse,
    ConversationListResponse,
    HtmlWidget,
)


DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_MODEL_NAME = "aegis_v2"


def _as_text_list(value: Any) -> list[str]:
    """Normalize json/list/string references into display-safe strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def _artifact_references(value: Any) -> list[str]:
    """Extract compact reference labels for the frontend artifact contract."""
    if not isinstance(value, dict):
        return _as_text_list(value)

    references: list[str] = []
    references.extend(_as_text_list(value.get("sources")))
    references.extend(_as_text_list(value.get("periods")))
    for key in ("bank_ticker", "fiscal_year", "quarter"):
        if value.get(key) is not None:
            references.append(str(value[key]))
    return references


def _conversation_from_record(record: dict[str, Any]) -> ChatConversationSummary:
    """Map a database row to the public conversation contract."""
    return ChatConversationSummary(
        conversation_id=str(record["conversation_id"]),
        user_id=str(record["user_id"]),
        conversation_title=record.get("conversation_title"),
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


def _message_from_record(record: dict[str, Any]) -> ChatMessageRecord:
    """Map a database row to the public chat message contract."""
    return ChatMessageRecord(
        id=str(record["message_id"]),
        role=record["role"],
        content=record["content"],
    )


def _widget_from_message_content(content: str) -> HtmlWidget | None:
    """Parse a persisted hidden widget message into the public widget contract."""
    start = content.find(WIDGET_MARKER_OPEN)
    end = content.find(WIDGET_MARKER_CLOSE)
    if start < 0 or end <= start:
        return None
    payload = content[start + len(WIDGET_MARKER_OPEN) : end].strip()
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return HtmlWidget(**parsed)
    except ValueError:
        return None


def chat_history_items_from_messages(
    messages: list[ChatMessageRecord],
) -> list[ChatHistoryItem]:
    """Convert persisted messages into the UI's hydrated chat stream items."""
    items: list[ChatHistoryItem] = []
    for message in messages:
        widget = _widget_from_message_content(message.content)
        if widget is not None:
            items.append(ChatHistoryWidgetItem(widget=widget))
            continue
        if message.role == "tool":
            continue
        items.append(ChatHistoryMessageItem(message=message))
    return items


def _artifact_from_record(record: dict[str, Any]) -> Artifact:
    """Map a database row to the existing V2 artifact contract."""
    return Artifact(
        id=str(record["artifact_id"]),
        session_id=str(record["conversation_id"]),
        kind=str(record["artifact_type"]),
        title=str(record["artifact_title"]),
        html=str(record["artifact_content"]),
        source_widget_ids=[],
        evidence_ids=_artifact_references(record.get("artifact_references")),
        created_at=(
            record["created_at"]
            if isinstance(record["created_at"], datetime)
            else datetime.now()
        ),
    )


def _uuid_or_new(value: str | None) -> str:
    """Return a valid UUID string, generating one when needed."""
    if value:
        try:
            return str(UUID(str(value)))
        except ValueError:
            pass
    return str(uuid4())


def _json_param(value: Any) -> str | None:
    """Serialize optional JSONB parameters for text SQL statements."""
    if value is None:
        return None
    return json.dumps(value, default=str)


async def list_conversations(
    user_id: str = DEFAULT_USER_ID, limit: int = 25
) -> ConversationListResponse:
    """Return recent conversations for one user."""
    records = await fetch_all(
        """
        SELECT
            conversation_id,
            user_id,
            conversation_title,
            created_at,
            updated_at
        FROM public.chat_conversations
        WHERE user_id = CAST(:user_id AS uuid)
        ORDER BY updated_at DESC, created_at DESC
        LIMIT :limit
        """,
        {"user_id": user_id, "limit": limit},
        execution_id="v2-list-conversations",
    )
    return ConversationListResponse(
        conversations=[_conversation_from_record(record) for record in records]
    )


async def ensure_conversation(
    user_id: str = DEFAULT_USER_ID,
    conversation_id: str | None = None,
    title: str | None = None,
) -> ChatConversationSummary:
    """Return an existing conversation or create a new one for the user."""
    if conversation_id:
        record = await fetch_one(
            """
            SELECT
                conversation_id,
                user_id,
                conversation_title,
                created_at,
                updated_at
            FROM public.chat_conversations
            WHERE conversation_id = CAST(:conversation_id AS uuid)
              AND user_id = CAST(:user_id AS uuid)
            """,
            {"conversation_id": conversation_id, "user_id": user_id},
            execution_id="v2-ensure-conversation-select",
        )
        if record is not None:
            return _conversation_from_record(record)

    next_conversation_id = _uuid_or_new(conversation_id)
    record = await fetch_one(
        """
        INSERT INTO public.chat_conversations (
            conversation_id,
            user_id,
            conversation_title,
            created_at,
            updated_at
        )
        VALUES (
            CAST(:conversation_id AS uuid),
            CAST(:user_id AS uuid),
            :conversation_title,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (conversation_id) DO UPDATE SET
            conversation_title = COALESCE(
                public.chat_conversations.conversation_title,
                EXCLUDED.conversation_title
            ),
            updated_at = CURRENT_TIMESTAMP
        RETURNING
            conversation_id,
            user_id,
            conversation_title,
            created_at,
            updated_at
        """,
        {
            "conversation_id": next_conversation_id,
            "user_id": user_id,
            "conversation_title": title or "New conversation",
        },
        execution_id="v2-ensure-conversation-insert",
    )
    if record is None:
        raise RuntimeError("Conversation insert did not return a row")
    return _conversation_from_record(record)


async def latest_conversation(
    user_id: str = DEFAULT_USER_ID,
) -> ChatConversationSummary | None:
    """Return the most recently updated conversation for one user."""
    conversations = await list_conversations(user_id=user_id, limit=1)
    return conversations.conversations[0] if conversations.conversations else None


async def list_conversation_messages(conversation_id: str) -> list[ChatMessageRecord]:
    """Return messages for a conversation in display order."""
    records = await fetch_all(
        """
        SELECT
            message_id,
            role,
            content,
            created_at
        FROM public.chat_messages
        WHERE conversation_id = CAST(:conversation_id AS uuid)
        ORDER BY created_at ASC, message_id ASC
        """,
        {"conversation_id": conversation_id},
        execution_id="v2-list-chat-messages",
    )
    return [_message_from_record(record) for record in records]


async def append_chat_message(
    conversation_id: str,
    role: str,
    content: str,
    run_uuid: str | None = None,
    message_id: str | None = None,
) -> ChatMessageRecord:
    """Persist one chat message and bump the conversation update timestamp."""
    record = await fetch_one(
        """
        INSERT INTO public.chat_messages (
            message_id,
            conversation_id,
            run_uuid,
            role,
            content,
            created_at
        )
        VALUES (
            CAST(:message_id AS uuid),
            CAST(:conversation_id AS uuid),
            CAST(:run_uuid AS uuid),
            :role,
            :content,
            CURRENT_TIMESTAMP
        )
        RETURNING
            message_id,
            role,
            content
        """,
        {
            "message_id": _uuid_or_new(message_id),
            "conversation_id": conversation_id,
            "run_uuid": run_uuid,
            "role": role,
            "content": content,
        },
        execution_id="v2-append-chat-message",
    )
    await execute_query(
        """
        UPDATE public.chat_conversations
        SET updated_at = CURRENT_TIMESTAMP
        WHERE conversation_id = CAST(:conversation_id AS uuid)
        """,
        {"conversation_id": conversation_id},
        execution_id="v2-touch-conversation",
    )
    if record is None:
        raise RuntimeError("Chat message insert did not return a row")
    return _message_from_record(record)


async def list_conversation_artifacts(conversation_id: str) -> ArtifactListResponse:
    """Return artifacts for a conversation, newest first."""
    records = await fetch_all(
        """
        SELECT
            artifact_id,
            conversation_id,
            artifact_title,
            artifact_type,
            artifact_content,
            artifact_references,
            created_at
        FROM public.artifacts
        WHERE conversation_id = CAST(:conversation_id AS uuid)
        ORDER BY created_at DESC, artifact_id ASC
        """,
        {"conversation_id": conversation_id},
        execution_id="v2-list-artifacts",
    )
    return ArtifactListResponse(
        artifacts=[_artifact_from_record(record) for record in records]
    )


async def load_conversation_context(
    conversation_id: str | None,
    *,
    current_user_content: str | None = None,
    message_limit: int = 12,
    artifact_limit: int = 6,
) -> ConversationContext:
    """Return structured prior conversation context for agent follow-up turns."""
    if not conversation_id:
        return ConversationContext()
    messages = await list_conversation_messages(conversation_id)
    artifacts = await list_conversation_artifacts(conversation_id)
    return build_conversation_context(
        messages=messages,
        artifacts=artifacts.artifacts,
        current_user_content=current_user_content,
        message_limit=message_limit,
        artifact_limit=artifact_limit,
    )


async def persist_artifact(
    conversation_id: str,
    artifact: Artifact,
    run_uuid: str | None = None,
) -> Artifact:
    """Persist one generated artifact and return the stored public contract."""
    artifact_id = _uuid_or_new(artifact.id)
    references = {
        "source_widget_ids": artifact.source_widget_ids,
        "evidence_ids": artifact.evidence_ids,
    }
    record = await fetch_one(
        """
        INSERT INTO public.artifacts (
            artifact_id,
            conversation_id,
            run_uuid,
            artifact_title,
            artifact_type,
            artifact_content,
            artifact_references,
            created_at,
            updated_at
        )
        VALUES (
            CAST(:artifact_id AS uuid),
            CAST(:conversation_id AS uuid),
            CAST(:run_uuid AS uuid),
            :artifact_title,
            :artifact_type,
            :artifact_content,
            CAST(:artifact_references AS jsonb),
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (artifact_id) DO UPDATE SET
            run_uuid = EXCLUDED.run_uuid,
            artifact_title = EXCLUDED.artifact_title,
            artifact_type = EXCLUDED.artifact_type,
            artifact_content = EXCLUDED.artifact_content,
            artifact_references = EXCLUDED.artifact_references,
            updated_at = CURRENT_TIMESTAMP
        RETURNING
            artifact_id,
            conversation_id,
            artifact_title,
            artifact_type,
            artifact_content,
            artifact_references,
            created_at
        """,
        {
            "artifact_id": artifact_id,
            "conversation_id": conversation_id,
            "run_uuid": run_uuid,
            "artifact_title": artifact.title,
            "artifact_type": artifact.kind,
            "artifact_content": artifact.html,
            "artifact_references": _json_param(references),
        },
        execution_id="v2-persist-artifact",
    )
    await execute_query(
        """
        UPDATE public.chat_conversations
        SET updated_at = CURRENT_TIMESTAMP
        WHERE conversation_id = CAST(:conversation_id AS uuid)
        """,
        {"conversation_id": conversation_id},
        execution_id="v2-touch-conversation",
    )
    if record is None:
        raise RuntimeError("Artifact insert did not return a row")
    return _artifact_from_record(record)


async def log_process_monitor_stage(
    run_uuid: str,
    stage_name: str,
    status: str,
    user_id: str | None = None,
    model_name: str = DEFAULT_MODEL_NAME,
    decision_details: str | None = None,
    error_message: str | None = None,
    custom_metadata: dict[str, Any] | None = None,
    stage_start_time: datetime | None = None,
    stage_end_time: datetime | None = None,
) -> None:
    """Insert one compact process monitor row using the preserved V1 schema."""
    started = stage_start_time or datetime.now(timezone.utc)
    ended = stage_end_time or datetime.now(timezone.utc)
    duration_ms = max(0, int((ended - started).total_seconds() * 1000))
    await execute_query(
        """
        INSERT INTO public.process_monitor_logs (
            run_uuid,
            model_name,
            stage_name,
            stage_start_time,
            stage_end_time,
            duration_ms,
            status,
            decision_details,
            error_message,
            user_id,
            environment,
            custom_metadata
        )
        VALUES (
            CAST(:run_uuid AS uuid),
            :model_name,
            :stage_name,
            :stage_start_time,
            :stage_end_time,
            :duration_ms,
            :status,
            :decision_details,
            :error_message,
            :user_id,
            :environment,
            CAST(:custom_metadata AS jsonb)
        )
        """,
        {
            "run_uuid": run_uuid,
            "model_name": model_name,
            "stage_name": stage_name[:100],
            "stage_start_time": started,
            "stage_end_time": ended,
            "duration_ms": duration_ms,
            "status": status[:255],
            "decision_details": decision_details,
            "error_message": error_message,
            "user_id": user_id,
            "environment": "local",
            "custom_metadata": _json_param(custom_metadata),
        },
        execution_id="v2-log-process-stage",
    )


async def get_conversation_detail(
    conversation_id: str,
    user_id: str = DEFAULT_USER_ID,
) -> ConversationDetailResponse | None:
    """Return one conversation and its child runtime rows."""
    record = await fetch_one(
        """
        SELECT
            conversation_id,
            user_id,
            conversation_title,
            created_at,
            updated_at
        FROM public.chat_conversations
        WHERE conversation_id = CAST(:conversation_id AS uuid)
          AND user_id = CAST(:user_id AS uuid)
        """,
        {"conversation_id": conversation_id, "user_id": user_id},
        execution_id="v2-get-conversation",
    )
    if record is None:
        return None

    messages = await list_conversation_messages(conversation_id)
    artifacts = await list_conversation_artifacts(conversation_id)
    return ConversationDetailResponse(
        conversation=_conversation_from_record(record),
        messages=messages,
        chat_items=chat_history_items_from_messages(messages),
        artifacts=artifacts.artifacts,
    )


async def bootstrap_runtime(
    user_id: str = DEFAULT_USER_ID,
    conversation_id: str | None = None,
) -> BootstrapResponse:
    """Return initial persisted chat state for the V2 UI."""
    conversation = (
        await get_conversation_detail(conversation_id, user_id=user_id)
        if conversation_id
        else None
    )
    if conversation is not None:
        return BootstrapResponse(
            user_id=user_id,
            active_conversation=conversation.conversation,
            messages=conversation.messages,
            chat_items=conversation.chat_items,
            recent_artifacts=conversation.artifacts,
        )

    active_conversation = await latest_conversation(user_id=user_id)
    if active_conversation is None:
        return BootstrapResponse(user_id=user_id)

    detail = await get_conversation_detail(
        active_conversation.conversation_id, user_id=user_id
    )
    if detail is None:
        return BootstrapResponse(user_id=user_id)
    return BootstrapResponse(
        user_id=user_id,
        active_conversation=detail.conversation,
        messages=detail.messages,
        chat_items=detail.chat_items,
        recent_artifacts=detail.artifacts,
    )


async def get_artifact(
    artifact_id: str, user_id: str = DEFAULT_USER_ID
) -> Artifact | None:
    """Return one artifact for the current user."""
    record = await fetch_one(
        """
        SELECT
            artifact.artifact_id,
            artifact.conversation_id,
            artifact.artifact_title,
            artifact.artifact_type,
            artifact.artifact_content,
            artifact.artifact_references,
            artifact.created_at
        FROM public.artifacts artifact
        JOIN public.chat_conversations conversation
          ON conversation.conversation_id = artifact.conversation_id
        WHERE artifact.artifact_id = CAST(:artifact_id AS uuid)
          AND conversation.user_id = CAST(:user_id AS uuid)
        """,
        {"artifact_id": artifact_id, "user_id": user_id},
        execution_id="v2-get-artifact",
    )
    return _artifact_from_record(record) if record else None
