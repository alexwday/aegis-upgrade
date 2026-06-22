#!/usr/bin/env python3
"""Create and seed the Aegis V2 catalog tables."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "aegis-agent" / "src"
if str(AGENT_SRC) not in sys.path:
    sys.path.insert(0, str(AGENT_SRC))

from aegis_agent.connections.postgres_connector import fetch_all, get_connection  # noqa: E402


CATALOG_DDL_PATH = REPO_ROOT / "aegis-table-schemas" / "catalog" / "001_catalog_tables.sql"
DEFAULT_MONITORED_INSTITUTIONS_PATH = Path(
    "/Users/alexwday/Projects/factset/database_refresh/monitored_institutions.yaml"
)

SOURCE_ROWS = [
    {
        "data_source_name": "rts",
        "data_source_display_name": "Reports to Shareholders",
        "data_source_description": (
            "Quarterly and annual shareholder reports and filed financial statements used as the official "
            "source for management discussion, financial statements, segment results, and filed banking "
            "disclosures in Canadian bank analysis."
        ),
    },
    {
        "data_source_name": "pillar3",
        "data_source_display_name": "Pillar 3",
        "data_source_description": (
            "Basel Pillar 3 regulatory disclosure packages covering capital adequacy, risk-weighted assets, "
            "leverage, liquidity, credit risk, market risk, and other risk disclosures for banking institutions."
        ),
    },
    {
        "data_source_name": "supplementary_financials",
        "data_source_display_name": "Supplementary Financials",
        "data_source_description": (
            "Quarterly supplemental financial packages containing detailed schedules, segment metrics, "
            "capital measures, credit quality, and other tabular data used for peer comparison and trend analysis."
        ),
    },
    {
        "data_source_name": "investor_slides",
        "data_source_display_name": "Investor Presentations",
        "data_source_description": (
            "Investor presentation slide decks, including quarterly earnings presentations and other investor "
            "materials that summarize performance, strategy, capital, credit, and management messaging."
        ),
    },
    {
        "data_source_name": "transcripts",
        "data_source_display_name": "Earnings Transcripts",
        "data_source_description": (
            "Quarterly earnings call transcripts with prepared remarks and analyst Q&A, used for management "
            "commentary, outlook, business drivers, and explanations behind reported financial results."
        ),
    },
    {
        "data_source_name": "event_transcripts",
        "data_source_display_name": "Event Transcripts",
        "data_source_description": (
            "Non-earnings event transcripts such as investor days, analyst days, conferences, and fireside chats "
            "that provide additional management commentary outside the regular quarterly earnings call."
        ),
    },
]

AVAILABILITY_PERIODS = [(2025, "Q4"), (2026, "Q1"), (2026, "Q2")]

BANK_NAME_OVERRIDES = {
    "RY-CA": "rbc",
    "BMO-CA": "bmo",
    "CM-CA": "cibc",
    "NA-CA": "national_bank",
    "BNS-CA": "scotiabank",
    "TD-CA": "td",
    "LB-CA": "laurentian_bank",
    "JPM-US": "jpmorgan",
    "BAC-US": "bank_of_america",
    "WFC-US": "wells_fargo",
    "C-US": "citigroup",
    "MS-US": "morgan_stanley",
    "GS-US": "goldman_sachs",
    "JEF-US": "jefferies",
}


def slugify(value: str) -> str:
    """Return a stable lowercase identifier."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "institution"


def monitored_institutions_path() -> Path:
    """Return configured monitored institutions YAML path."""
    configured = os.getenv("MONITORED_INSTITUTIONS_YAML", "").strip()
    return Path(configured) if configured else DEFAULT_MONITORED_INSTITUTIONS_PATH


def load_institutions(path: Path) -> list[dict[str, str]]:
    """Load monitored institutions into v2 catalog rows."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected monitored institutions YAML mapping at {path}")

    used_names: set[str] = set()
    rows: list[dict[str, str]] = []
    for bank_ticker, metadata in sorted(
        loaded.items(),
        key=lambda item: int((item[1] or {}).get("id", 999999)),
    ):
        if not isinstance(metadata, dict):
            raise ValueError(f"Expected metadata mapping for {bank_ticker}")

        display_name = str(metadata.get("name") or "").strip()
        if not display_name:
            raise ValueError(f"Missing institution name for {bank_ticker}")

        base_name = BANK_NAME_OVERRIDES.get(str(bank_ticker), slugify(display_name))
        bank_name = base_name
        if bank_name in used_names:
            bank_name = f"{base_name}_{slugify(str(bank_ticker))}"
        used_names.add(bank_name)

        rows.append(
            {
                "bank_ticker": str(bank_ticker),
                "bank_name": bank_name,
                "bank_display_name": display_name,
                "bank_category": str(metadata.get("type") or "Uncategorized"),
            }
        )

    return rows


def split_sql_statements(sql: str) -> list[str]:
    """Split SQL statements while preserving dollar-quoted function bodies."""
    statements: list[str] = []
    buffer: list[str] = []
    dollar_tag: str | None = None
    in_single_quote = False
    in_double_quote = False
    index = 0

    while index < len(sql):
        char = sql[index]

        if dollar_tag:
            if sql.startswith(dollar_tag, index):
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            buffer.append(char)
            index += 1
            continue

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

        if char == "$":
            match = re.match(r"\$[A-Za-z0-9_]*\$", sql[index:])
            if match:
                dollar_tag = match.group(0)
                buffer.append(dollar_tag)
                index += len(dollar_tag)
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


async def execute_sql_file(path: Path) -> None:
    """Execute a SQL file in one transaction."""
    sql = path.read_text(encoding="utf-8")
    async with get_connection("seed-v2-catalog-ddl") as conn:
        for statement in split_sql_statements(sql):
            await conn.execute(text(statement))


async def upsert_catalog_rows(institutions: list[dict[str, str]]) -> None:
    """Upsert source registry, monitored institutions, and mock availability rows."""
    source_names = [row["data_source_name"] for row in SOURCE_ROWS]
    availability_rows = [
        {
            "bank_ticker": institution["bank_ticker"],
            "fiscal_year": fiscal_year,
            "quarter": quarter,
            "data_source_list": source_names,
        }
        for institution in institutions
        for fiscal_year, quarter in AVAILABILITY_PERIODS
    ]

    async with get_connection("seed-v2-catalog-data") as conn:
        await conn.execute(
            text(
                """
                INSERT INTO public.data_source_registry (
                    data_source_name,
                    data_source_display_name,
                    data_source_description,
                    updated_at
                )
                VALUES (
                    :data_source_name,
                    :data_source_display_name,
                    :data_source_description,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (data_source_name) DO UPDATE SET
                    data_source_display_name = EXCLUDED.data_source_display_name,
                    data_source_description = EXCLUDED.data_source_description,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            SOURCE_ROWS,
        )

        await conn.execute(
            text(
                """
                INSERT INTO public.monitored_institutions (
                    bank_ticker,
                    bank_name,
                    bank_display_name,
                    bank_category,
                    updated_at
                )
                VALUES (
                    :bank_ticker,
                    :bank_name,
                    :bank_display_name,
                    :bank_category,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (bank_ticker) DO UPDATE SET
                    bank_name = EXCLUDED.bank_name,
                    bank_display_name = EXCLUDED.bank_display_name,
                    bank_category = EXCLUDED.bank_category,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            institutions,
        )

        await conn.execute(
            text(
                """
                INSERT INTO public.data_source_availability (
                    bank_ticker,
                    fiscal_year,
                    quarter,
                    data_source_list,
                    updated_at
                )
                VALUES (
                    :bank_ticker,
                    :fiscal_year,
                    :quarter,
                    :data_source_list,
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (bank_ticker, fiscal_year, quarter) DO UPDATE SET
                    data_source_list = EXCLUDED.data_source_list,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            availability_rows,
        )


async def verify_counts() -> dict[str, Any]:
    """Return compact verification counts."""
    rows = await fetch_all(
        """
        SELECT 'data_source_registry' AS table_name, count(*) AS row_count FROM public.data_source_registry
        UNION ALL
        SELECT 'monitored_institutions' AS table_name, count(*) AS row_count FROM public.monitored_institutions
        UNION ALL
        SELECT 'data_source_availability' AS table_name, count(*) AS row_count FROM public.data_source_availability
        ORDER BY table_name
        """
    )
    period_rows = await fetch_all(
        """
        SELECT fiscal_year, quarter, count(*) AS row_count
        FROM public.data_source_availability
        GROUP BY fiscal_year, quarter
        ORDER BY fiscal_year, quarter
        """
    )
    return {"tables": rows, "periods": period_rows}


async def main() -> None:
    institutions_path = monitored_institutions_path()
    institutions = load_institutions(institutions_path)
    await execute_sql_file(CATALOG_DDL_PATH)
    await upsert_catalog_rows(institutions)
    counts = await verify_counts()
    print(f"Seeded V2 catalog from {institutions_path}")
    for row in counts["tables"]:
        print(f"{row['table_name']}: {row['row_count']}")
    for row in counts["periods"]:
        print(f"{row['fiscal_year']} {row['quarter']}: {row['row_count']}")


if __name__ == "__main__":
    asyncio.run(main())
