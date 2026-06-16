#!/usr/bin/env python3
"""Check/create Aegis PostgreSQL tables and refresh source availability."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import psycopg2
from psycopg2.extensions import connection as PsycopgConnection

from sync_env import first, read_env


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEMA_DIR = PROJECT_ROOT / "aegis-table-schemas"
CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(?:public\.)?\"?([^\"\s(]+)\"?",
    re.IGNORECASE,
)

SOURCE_TABLES = {
    "investor_slides": "aegis-investor-slides-data",
    "supplementary_financials": "aegis-financial-supp-data",
    "rts": "aegis-rts-data",
    "pillar3": "aegis-pillar3-data",
    "transcripts": "aegis-earnings-transcripts-data",
    "event_transcripts": "aegis-event-transcripts-data",
}

BANKS = {
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
    "CM-CA": {
        "bank_id": 3,
        "bank_name": "Canadian Imperial Bank of Commerce",
        "aliases": ["CIBC", "Canadian Imperial Bank of Commerce", "CM"],
        "tags": ["canadian_bank", "cibc"],
    },
    "NA-CA": {
        "bank_id": 4,
        "bank_name": "National Bank of Canada",
        "aliases": ["National Bank", "NBC", "NA"],
        "tags": ["canadian_bank", "national_bank"],
    },
    "BNS-CA": {
        "bank_id": 5,
        "bank_name": "Bank of Nova Scotia",
        "aliases": ["Scotiabank", "Scotia", "Bank of Nova Scotia", "BNS"],
        "tags": ["canadian_bank", "scotiabank"],
    },
    "TD-CA": {
        "bank_id": 6,
        "bank_name": "Toronto-Dominion Bank",
        "aliases": ["TD", "TD Bank", "Toronto-Dominion"],
        "tags": ["canadian_bank", "td"],
    },
}


@dataclass(frozen=True)
class SchemaFile:
    path: Path
    table_name: str


def main() -> int:
    args = parse_args()
    values = read_env(args.env_file.expanduser().resolve())
    with psycopg2.connect(**database_config(values), application_name="aegis-db-setup") as conn:
        if not args.skip_vector_extension and args.apply:
            ensure_vector_extension(conn)

        schema_files = discover_schema_files(args.schema_dir.expanduser().resolve())
        report_or_apply_schemas(conn, schema_files, apply=args.apply)

        if args.refresh_availability:
            upserted = refresh_availability(conn)
            print(f"availability rows upserted: {upserted}")

    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="Root dotenv file with DB or POSTGRES settings.",
    )
    parser.add_argument(
        "--schema-dir",
        type=Path,
        default=DEFAULT_SCHEMA_DIR,
        help="Directory containing *.sql schema files.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create missing tables. Without this flag, only report table status.",
    )
    parser.add_argument(
        "--skip-vector-extension",
        action="store_true",
        help="Do not run CREATE EXTENSION IF NOT EXISTS vector.",
    )
    parser.add_argument(
        "--refresh-availability",
        action="store_true",
        help="Derive aegis_data_availability rows from loaded source tables.",
    )
    return parser.parse_args()


def database_config(values: Mapping[str, str]) -> dict[str, str]:
    config = {
        "host": first(values, "DB_HOST", "POSTGRES_HOST", default="127.0.0.1"),
        "port": first(values, "DB_PORT", "POSTGRES_PORT", default="5432"),
        "dbname": first(values, "DB_NAME", "POSTGRES_DATABASE", default="postgres"),
        "user": first(values, "DB_USER", "POSTGRES_USER", default="postgres"),
        "password": first(values, "DB_PASSWORD", "POSTGRES_PASSWORD"),
    }
    return {key: value for key, value in config.items() if value}


def discover_schema_files(schema_dir: Path) -> list[SchemaFile]:
    if not schema_dir.is_dir():
        raise FileNotFoundError(f"Schema directory not found: {schema_dir}")

    schema_files = []
    for path in sorted(schema_dir.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        match = CREATE_TABLE_RE.search(text)
        if not match:
            raise ValueError(f"Could not find CREATE TABLE in {path}")
        schema_files.append(SchemaFile(path=path, table_name=match.group(1)))
    return schema_files


def ensure_vector_extension(conn: PsycopgConnection) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    conn.commit()
    print("vector extension checked")


def report_or_apply_schemas(
    conn: PsycopgConnection,
    schema_files: Iterable[SchemaFile],
    *,
    apply: bool,
) -> None:
    for schema_file in schema_files:
        exists = table_exists(conn, schema_file.table_name)
        if exists:
            print(f"exists  public.\"{schema_file.table_name}\"")
            continue
        if not apply:
            print(f"missing public.\"{schema_file.table_name}\"")
            continue
        apply_schema_file(conn, schema_file)
        print(f"created public.\"{schema_file.table_name}\" from {schema_file.path.name}")


def table_exists(conn: PsycopgConnection, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = %s
            )
            """,
            (table_name,),
        )
        return bool(cur.fetchone()[0])


def apply_schema_file(conn: PsycopgConnection, schema_file: SchemaFile) -> None:
    sql_text = schema_file.path.read_text(encoding="utf-8")
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def refresh_availability(conn: PsycopgConnection) -> int:
    if not table_exists(conn, "aegis_data_availability"):
        raise RuntimeError("aegis_data_availability table does not exist. Run --apply first.")

    total = 0
    for source_name, table_name in SOURCE_TABLES.items():
        if not table_exists(conn, table_name):
            print(f"skip    public.\"{table_name}\" is missing")
            continue
        rows = source_period_rows(conn, table_name)
        records = [availability_record(row, source_name) for row in rows]
        records = [record for record in records if record is not None]
        total += upsert_availability_records(conn, records)
        print(f"source  {source_name}: {len(records)} availability row(s)")
    conn.commit()
    return total


def source_period_rows(conn: PsycopgConnection, table_name: str) -> list[tuple[str, str, str]]:
    query = f"""
        SELECT DISTINCT bank, fiscal_year, quarter
        FROM public."{table_name}"
        WHERE bank IS NOT NULL
          AND fiscal_year IS NOT NULL
          AND quarter IS NOT NULL
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return [(str(bank), str(year), str(quarter)) for bank, year, quarter in cur.fetchall()]


def availability_record(row: tuple[str, str, str], source_name: str) -> dict[str, object] | None:
    bank_symbol, fiscal_year_raw, quarter_raw = row
    bank_symbol = normalize_bank_symbol(bank_symbol)
    bank = BANKS.get(bank_symbol)
    if not bank:
        print(f"warn    unknown bank symbol for availability: {bank_symbol}")
        return None

    fiscal_year = parse_fiscal_year(fiscal_year_raw)
    quarter = normalize_quarter(quarter_raw)
    if fiscal_year is None or quarter is None:
        print(f"warn    invalid period for availability: {bank_symbol} {fiscal_year_raw} {quarter_raw}")
        return None

    return {
        "bank_id": bank["bank_id"],
        "bank_name": bank["bank_name"],
        "bank_symbol": bank_symbol,
        "bank_aliases": bank["aliases"],
        "bank_tags": bank["tags"],
        "fiscal_year": fiscal_year,
        "quarter": quarter,
        "database_names": [source_name],
        "last_updated_by": "aegis-db-setup",
    }


def normalize_bank_symbol(value: str) -> str:
    text = value.strip().upper()
    aliases = {
        "RY": "RY-CA",
        "RBC": "RY-CA",
        "BMO": "BMO-CA",
        "CM": "CM-CA",
        "CIBC": "CM-CA",
        "NA": "NA-CA",
        "NBC": "NA-CA",
        "BNS": "BNS-CA",
        "SCOTIA": "BNS-CA",
        "TD": "TD-CA",
    }
    return aliases.get(text, text)


def parse_fiscal_year(value: str) -> int | None:
    match = re.search(r"20\d{2}", value)
    if not match:
        return None
    return int(match.group(0))


def normalize_quarter(value: str) -> str | None:
    text = value.strip().upper()
    if text in {"1", "2", "3", "4"}:
        text = f"Q{text}"
    return text if text in {"Q1", "Q2", "Q3", "Q4"} else None


def upsert_availability_records(
    conn: PsycopgConnection,
    records: Iterable[dict[str, object]],
) -> int:
    query = """
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
            %(bank_id)s,
            %(bank_name)s,
            %(bank_symbol)s,
            %(bank_aliases)s,
            %(bank_tags)s,
            %(fiscal_year)s,
            %(quarter)s,
            %(database_names)s,
            %(last_updated_by)s
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
    count = 0
    with conn.cursor() as cur:
        for record in records:
            cur.execute(query, record)
            count += 1
    return count


if __name__ == "__main__":
    raise SystemExit(main())
