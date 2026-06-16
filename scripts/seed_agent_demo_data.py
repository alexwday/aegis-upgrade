#!/usr/bin/env python
"""Seed transcript availability and optional fixture transcript data for Aegis Agent."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = PROJECT_ROOT / "aegis-agent"
SRC_DIR = AGENT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from aegis_agent.connections.postgres_connector import get_connection  # noqa: E402
from aegis_agent.utils.logging import get_logger, setup_logging  # noqa: E402


BANKS: Dict[str, Dict[str, Any]] = {
    "RY-CA": {
        "bank_id": 1,
        "bank_name": "Royal Bank of Canada",
        "aliases": ["RBC", "Royal Bank", "Royal Bank of Canada", "RY"],
        "tags": ["canadian_bank", "rbc"],
    },
    "BMO-CA": {
        "bank_id": 2,
        "bank_name": "Bank of Montreal",
        "aliases": ["BMO", "Bank of Montreal"],
        "tags": ["canadian_bank", "bmo"],
    },
    "TD-CA": {
        "bank_id": 6,
        "bank_name": "Toronto-Dominion Bank",
        "aliases": ["TD", "TD Bank", "Toronto-Dominion"],
        "tags": ["canadian_bank", "td"],
    },
}


FIXTURE_CHUNKS: List[Dict[str, Any]] = [
    {
        "ticker": "RY-CA",
        "institution_id": "1",
        "company_name": "Royal Bank of Canada",
        "fiscal_year": 2026,
        "fiscal_quarter": "Q1",
        "section_name": "MANAGEMENT DISCUSSION SECTION",
        "speaker_block_id": 1,
        "qa_group_id": None,
        "chunk_id": 1,
        "chunk_content": (
            "Management said credit quality remained resilient in Q1 2026. "
            "Provision for credit losses increased modestly as the bank built reserves "
            "for specific commercial accounts, while Canadian personal lending "
            "delinquencies were described as normalizing from unusually low levels."
        ),
        "block_summary": "RBC management discussed resilient credit quality and modest reserve build.",
    },
    {
        "ticker": "RY-CA",
        "institution_id": "1",
        "company_name": "Royal Bank of Canada",
        "fiscal_year": 2026,
        "fiscal_quarter": "Q1",
        "section_name": "Q&A",
        "speaker_block_id": None,
        "qa_group_id": 1,
        "chunk_id": 2,
        "chunk_content": (
            "In Q&A, RBC said impaired loan formation was concentrated in a small number "
            "of wholesale exposures. Management emphasized that consumer payment rates "
            "and employment trends still supported a constructive credit outlook."
        ),
        "block_summary": "RBC Q&A addressed impaired loan formation and consumer credit trends.",
    },
    {
        "ticker": "BMO-CA",
        "institution_id": "2",
        "company_name": "Bank of Montreal",
        "fiscal_year": 2026,
        "fiscal_quarter": "Q1",
        "section_name": "MANAGEMENT DISCUSSION SECTION",
        "speaker_block_id": 1,
        "qa_group_id": None,
        "chunk_id": 1,
        "chunk_content": (
            "BMO management characterized credit performance as manageable, with losses "
            "tracking within planning assumptions. The bank noted pockets of pressure "
            "in commercial real estate and unsecured consumer lending."
        ),
        "block_summary": "BMO management discussed manageable credit performance and pressure pockets.",
    },
    {
        "ticker": "BMO-CA",
        "institution_id": "2",
        "company_name": "Bank of Montreal",
        "fiscal_year": 2026,
        "fiscal_quarter": "Q1",
        "section_name": "Q&A",
        "speaker_block_id": None,
        "qa_group_id": 1,
        "chunk_id": 2,
        "chunk_content": (
            "In response to an analyst question, BMO said allowance levels reflected "
            "macroeconomic uncertainty. Management did not point to a broad-based "
            "deterioration, but said it was watching U.S. commercial portfolios closely."
        ),
        "block_summary": "BMO Q&A covered allowances, macro uncertainty, and U.S. commercial monitoring.",
    },
]


def _availability_record(ticker: str, fiscal_year: int, quarter: str) -> Dict[str, Any]:
    """Build one availability row for a known bank."""
    bank = BANKS.get(ticker) or {
        "bank_id": int(ticker),
        "bank_name": ticker,
        "aliases": [ticker],
        "tags": [],
    }
    return {
        "bank_id": bank["bank_id"],
        "bank_name": bank["bank_name"],
        "bank_symbol": ticker,
        "bank_aliases": bank["aliases"],
        "bank_tags": bank["tags"],
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "database_names": ["transcripts"],
        "last_updated_by": "aegis-agent-demo",
    }


async def _upsert_availability(records: Iterable[Dict[str, Any]]) -> int:
    """Upsert transcript availability records."""
    query = text(
        """
        INSERT INTO aegis_data_availability (
            bank_id,
            bank_name,
            bank_symbol,
            bank_aliases,
            bank_tags,
            fiscal_year,
            quarter,
            database_names,
            last_updated_by
        )
        VALUES (
            :bank_id,
            :bank_name,
            :bank_symbol,
            :bank_aliases,
            :bank_tags,
            :fiscal_year,
            :quarter,
            :database_names,
            :last_updated_by
        )
        ON CONFLICT (bank_id, fiscal_year, quarter)
        DO UPDATE SET
            bank_name = EXCLUDED.bank_name,
            bank_symbol = EXCLUDED.bank_symbol,
            bank_aliases = EXCLUDED.bank_aliases,
            bank_tags = EXCLUDED.bank_tags,
            database_names = ARRAY(
                SELECT DISTINCT unnest(
                    COALESCE(aegis_data_availability.database_names, ARRAY[]::text[])
                    || EXCLUDED.database_names
                )
            ),
            last_updated = CURRENT_TIMESTAMP,
            last_updated_by = EXCLUDED.last_updated_by
        """
    )

    count = 0
    async with get_connection("seed-demo-data") as conn:
        for record in records:
            await conn.execute(query, record)
            count += 1
    return count


async def derive_availability_from_transcripts() -> int:
    """Derive aegis_data_availability rows from distinct transcript rows."""
    async with get_connection("seed-demo-data") as conn:
        result = await conn.execute(
            text(
                """
                SELECT DISTINCT
                    institution_id,
                    ticker,
                    company_name,
                    fiscal_year,
                    fiscal_quarter
                FROM aegis_transcripts
                WHERE institution_id IS NOT NULL
                  AND ticker IS NOT NULL
                  AND fiscal_year IS NOT NULL
                  AND fiscal_quarter IS NOT NULL
                """
            )
        )
        rows = [dict(row._mapping) for row in result]  # pylint: disable=protected-access

    records: List[Dict[str, Any]] = []
    for row in rows:
        bank = BANKS.get(row["ticker"])
        try:
            bank_id = int(row["institution_id"])
        except (TypeError, ValueError):
            if not bank:
                continue
            bank_id = bank["bank_id"]

        records.append(
            {
                "bank_id": bank_id,
                "bank_name": row["company_name"] or (bank or {}).get("bank_name") or row["ticker"],
                "bank_symbol": row["ticker"],
                "bank_aliases": (bank or {}).get("aliases", [row["ticker"]]),
                "bank_tags": (bank or {}).get("tags", []),
                "fiscal_year": row["fiscal_year"],
                "quarter": row["fiscal_quarter"],
                "database_names": ["transcripts"],
                "last_updated_by": "aegis-agent-demo",
            }
        )

    return await _upsert_availability(records)


async def seed_fixture() -> int:
    """Insert fixture transcript chunks and matching availability records."""
    insert_query = text(
        """
        INSERT INTO aegis_transcripts (
            filename,
            title,
            transcript_type,
            event_id,
            version_id,
            fiscal_year,
            fiscal_quarter,
            institution_type,
            institution_id,
            ticker,
            company_name,
            section_name,
            speaker_block_id,
            qa_group_id,
            classification_ids,
            classification_names,
            block_summary,
            chunk_id,
            chunk_tokens,
            chunk_content,
            chunk_paragraph_ids
        )
        VALUES (
            :filename,
            :title,
            :transcript_type,
            :event_id,
            :version_id,
            :fiscal_year,
            :fiscal_quarter,
            :institution_type,
            :institution_id,
            :ticker,
            :company_name,
            :section_name,
            :speaker_block_id,
            :qa_group_id,
            :classification_ids,
            :classification_names,
            :block_summary,
            :chunk_id,
            :chunk_tokens,
            :chunk_content,
            :chunk_paragraph_ids
        )
        """
    )

    async with get_connection("seed-demo-data") as conn:
        await conn.execute(
            text("DELETE FROM aegis_transcripts WHERE event_id LIKE 'aegis-agent-demo-%'")
        )
        for chunk in FIXTURE_CHUNKS:
            ticker = chunk["ticker"]
            event_id = f"aegis-agent-demo-{ticker}-{chunk['fiscal_year']}-{chunk['fiscal_quarter']}"
            await conn.execute(
                insert_query,
                {
                    **chunk,
                    "filename": f"{event_id}.txt",
                    "title": f"{BANKS[ticker]['bank_name']} Q1 2026 Earnings Call",
                    "transcript_type": "earnings_call",
                    "event_id": event_id,
                    "version_id": "demo-v1",
                    "institution_type": "Canadian_Banks",
                    "classification_ids": ["credit_quality"],
                    "classification_names": ["Credit Quality"],
                    "chunk_tokens": len(chunk["chunk_content"].split()),
                    "chunk_paragraph_ids": [f"p-{chunk['chunk_id']}"],
                },
            )

    availability = {
        (chunk["ticker"], chunk["fiscal_year"], chunk["fiscal_quarter"]) for chunk in FIXTURE_CHUNKS
    }
    records = [
        _availability_record(ticker, fiscal_year, quarter)
        for ticker, fiscal_year, quarter in sorted(availability)
    ]
    await _upsert_availability(records)
    return len(FIXTURE_CHUNKS)


async def async_main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Insert the small demo transcript dataset before deriving availability.",
    )
    parser.add_argument(
        "--derive-only",
        action="store_true",
        help="Only derive availability from existing transcripts.",
    )
    args = parser.parse_args()

    setup_logging()
    logger = get_logger()

    inserted = 0
    if args.fixture and not args.derive_only:
        inserted = await seed_fixture()
        logger.info("seed.fixture.complete", chunks=inserted)

    availability_count = await derive_availability_from_transcripts()
    logger.info("seed.availability.complete", availability_rows=availability_count)
    print(
        "Seed complete: "
        f"{inserted} fixture chunks inserted, "
        f"{availability_count} availability rows upserted."
    )


if __name__ == "__main__":
    asyncio.run(async_main())
