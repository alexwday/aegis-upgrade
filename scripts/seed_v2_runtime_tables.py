#!/usr/bin/env python3
"""Create and seed the Aegis V2 chat/runtime tables."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import text


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "aegis-agent" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from aegis_agent.connections.postgres_connector import fetch_all, get_connection  # noqa: E402


DDL_PATH = REPO_ROOT / "aegis-table-schemas" / "runtime" / "001_chat_artifact_tables.sql"

DEMO_USER_ID = "00000000-0000-0000-0000-000000000001"
DEMO_CONVERSATION_ID = "10000000-0000-0000-0000-000000000001"
DEMO_RUN_UUID = "20000000-0000-0000-0000-000000000001"


def as_datetime(value: str) -> datetime:
    """Convert a UTC ISO timestamp into a datetime for asyncpg."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def split_sql_statements(sql: str) -> list[str]:
    """Split simple SQL files into executable statements."""
    statements: list[str] = []
    buffer: list[str] = []
    in_single_quote = False
    in_double_quote = False

    index = 0
    while index < len(sql):
        char = sql[index]
        if in_single_quote:
            buffer.append(char)
            if char == "'" and sql[index + 1 : index + 2] == "'":
                buffer.append("'")
                index += 2
                continue
            if char == "'":
                in_single_quote = False
            index += 1
            continue
        if in_double_quote:
            buffer.append(char)
            if char == '"':
                in_double_quote = False
            index += 1
            continue
        if char == "'":
            in_single_quote = True
            buffer.append(char)
            index += 1
            continue
        if char == '"':
            in_double_quote = True
            buffer.append(char)
            index += 1
            continue
        if char == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1

    statement = "".join(buffer).strip()
    if statement:
        statements.append(statement)
    return statements


def demo_artifact_html(title: str, summary: str, bullets: list[str]) -> str:
    """Return a small self-contained demo artifact."""
    bullet_html = "".join(f"<li>{bullet}</li>" for bullet in bullets)
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{title}</title>
    <style>
      body {{ font-family: Inter, Arial, sans-serif; margin: 32px; color: #182230; line-height: 1.45; }}
      h1 {{ font-size: 24px; margin: 0 0 8px; }}
      p {{ color: #4f5f72; }}
      li {{ margin: 8px 0; }}
      .tag {{ display: inline-block; margin-bottom: 12px; padding: 4px 7px; background: #e8f5f8; color: #084f6b; font-size: 11px; font-weight: 700; }}
    </style>
  </head>
  <body>
    <span class="tag">Aegis demo artifact</span>
    <h1>{title}</h1>
    <p>{summary}</p>
    <ul>{bullet_html}</ul>
  </body>
</html>"""


async def execute_ddl() -> None:
    """Create runtime tables."""
    sql = DDL_PATH.read_text(encoding="utf-8")
    async with get_connection("seed-v2-runtime-ddl") as conn:
        for statement in split_sql_statements(sql):
            await conn.execute(text(statement))


async def seed_data() -> None:
    """Seed one deterministic demo conversation with messages and artifacts."""
    messages = [
        {
            "message_id": "11000000-0000-0000-0000-000000000001",
            "conversation_id": DEMO_CONVERSATION_ID,
            "run_uuid": None,
            "role": "user",
            "content": "What data do we have available for RBC Q1 2026?",
            "created_at": "2026-06-22T15:00:00Z",
        },
        {
            "message_id": "11000000-0000-0000-0000-000000000002",
            "conversation_id": DEMO_CONVERSATION_ID,
            "run_uuid": DEMO_RUN_UUID,
            "role": "assistant",
            "content": (
                "The seeded catalog shows all six sources available for RBC in Q1 2026: "
                "reports to shareholders, Pillar 3, supplementary financials, investor presentations, "
                "earnings transcripts, and event transcripts."
            ),
            "created_at": "2026-06-22T15:00:08Z",
        },
        {
            "message_id": "11000000-0000-0000-0000-000000000003",
            "conversation_id": DEMO_CONVERSATION_ID,
            "run_uuid": None,
            "role": "user",
            "content": "Create a short source coverage artifact.",
            "created_at": "2026-06-22T15:01:00Z",
        },
        {
            "message_id": "11000000-0000-0000-0000-000000000004",
            "conversation_id": DEMO_CONVERSATION_ID,
            "run_uuid": DEMO_RUN_UUID,
            "role": "assistant",
            "content": "I created two demo artifacts from the runtime tables for UI testing.",
            "created_at": "2026-06-22T15:01:12Z",
        },
    ]
    artifacts = [
        {
            "artifact_id": "12000000-0000-0000-0000-000000000001",
            "conversation_id": DEMO_CONVERSATION_ID,
            "run_uuid": DEMO_RUN_UUID,
            "artifact_title": "RBC Q1 2026 Source Coverage",
            "artifact_type": "quick_search",
            "artifact_content": demo_artifact_html(
                "RBC Q1 2026 Source Coverage",
                "Demo artifact loaded from public.artifacts for the V2 viewer.",
                [
                    "All six seeded data sources are available for RY-CA in 2026 Q1.",
                    "The artifact tile, metadata, and preview content are database-backed.",
                    "This artifact can be replaced by generated Aegis output once query streaming is wired.",
                ],
            ),
            "artifact_references": json.dumps(
                {
                    "sources": ["rts", "pillar3", "supplementary_financials", "investor_slides", "transcripts", "event_transcripts"],
                    "bank_ticker": "RY-CA",
                    "fiscal_year": 2026,
                    "quarter": "Q1",
                }
            ),
            "created_at": "2026-06-22T15:01:10Z",
            "updated_at": "2026-06-22T15:01:10Z",
        },
        {
            "artifact_id": "12000000-0000-0000-0000-000000000002",
            "conversation_id": DEMO_CONVERSATION_ID,
            "run_uuid": DEMO_RUN_UUID,
            "artifact_title": "Canadian Bank Coverage Snapshot",
            "artifact_type": "report",
            "artifact_content": demo_artifact_html(
                "Canadian Bank Coverage Snapshot",
                "Demo report artifact for validating the V2 artifact scroller and viewer.",
                [
                    "The catalog seed includes 91 monitored institutions across 14 categories.",
                    "Mock availability currently covers 2025 Q4, 2026 Q1, and 2026 Q2.",
                    "Each seeded bank-period row has all six registered source names.",
                ],
            ),
            "artifact_references": json.dumps(
                {
                    "sources": ["data_source_registry", "monitored_institutions", "data_source_availability"],
                    "periods": ["2025 Q4", "2026 Q1", "2026 Q2"],
                }
            ),
            "created_at": "2026-06-22T15:02:00Z",
            "updated_at": "2026-06-22T15:02:00Z",
        },
    ]
    for message in messages:
        message["created_at"] = as_datetime(str(message["created_at"]))
    for artifact in artifacts:
        artifact["created_at"] = as_datetime(str(artifact["created_at"]))
        artifact["updated_at"] = as_datetime(str(artifact["updated_at"]))

    async with get_connection("seed-v2-runtime-data") as conn:
        await conn.execute(
            text(
                """
                INSERT INTO public.chat_conversations (
                    conversation_id,
                    user_id,
                    conversation_title,
                    created_at,
                    updated_at
                )
                VALUES (
                    :conversation_id,
                    :user_id,
                    :conversation_title,
                    :created_at,
                    :updated_at
                )
                ON CONFLICT (conversation_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    conversation_title = EXCLUDED.conversation_title,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "conversation_id": DEMO_CONVERSATION_ID,
                "user_id": DEMO_USER_ID,
                "conversation_title": "Demo catalog runtime conversation",
                "created_at": as_datetime("2026-06-22T15:00:00Z"),
                "updated_at": as_datetime("2026-06-22T15:02:00Z"),
            },
        )
        await conn.execute(
            text(
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
                    :message_id,
                    :conversation_id,
                    :run_uuid,
                    :role,
                    :content,
                    :created_at
                )
                ON CONFLICT (message_id) DO UPDATE SET
                    run_uuid = EXCLUDED.run_uuid,
                    role = EXCLUDED.role,
                    content = EXCLUDED.content,
                    created_at = EXCLUDED.created_at
                """
            ),
            messages,
        )
        await conn.execute(
            text(
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
                    :artifact_id,
                    :conversation_id,
                    :run_uuid,
                    :artifact_title,
                    :artifact_type,
                    :artifact_content,
                    CAST(:artifact_references AS jsonb),
                    :created_at,
                    :updated_at
                )
                ON CONFLICT (artifact_id) DO UPDATE SET
                    run_uuid = EXCLUDED.run_uuid,
                    artifact_title = EXCLUDED.artifact_title,
                    artifact_type = EXCLUDED.artifact_type,
                    artifact_content = EXCLUDED.artifact_content,
                    artifact_references = EXCLUDED.artifact_references,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            artifacts,
        )


async def verify_counts() -> None:
    """Print compact row counts."""
    rows = await fetch_all(
        """
        SELECT 'chat_conversations' AS table_name, count(*) AS row_count FROM public.chat_conversations
        UNION ALL
        SELECT 'chat_messages' AS table_name, count(*) AS row_count FROM public.chat_messages
        UNION ALL
        SELECT 'artifacts' AS table_name, count(*) AS row_count FROM public.artifacts
        ORDER BY table_name
        """
    )
    for row in rows:
        print(f"{row['table_name']}: {row['row_count']}")


async def main() -> None:
    await execute_ddl()
    await seed_data()
    await verify_counts()


if __name__ == "__main__":
    asyncio.run(main())
