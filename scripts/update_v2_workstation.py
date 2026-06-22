#!/usr/bin/env python3
"""Install/update the Aegis V2 workstation on a local work machine.

The script is intentionally idempotent:
- syncs root .env into aegis-agent/.env
- creates V2 catalog/runtime tables when missing
- upserts source registry and monitored institutions
- refreshes data_source_availability from live processed source tables
- pushes prompt YAML files when present
- optionally builds the frontend, starts FastAPI, and opens /v2
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

import psycopg2
import yaml
from psycopg2 import sql
from psycopg2.extensions import connection as PsycopgConnection

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sync_env import first, read_env


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_ROOT = REPO_ROOT / "aegis-agent"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_PROMPTS_DIR = REPO_ROOT / "aegis-prompts"
DEFAULT_WORK_INSTITUTIONS = Path(
    "~/Projects/factset/database_refresh/monitored_institutions.yaml"
).expanduser()
DEFAULT_WORK_INSTITUTIONS_SPACE = Path(
    "~/Projects/factset/database refresh/monitored_institutions.yaml"
).expanduser()
FALLBACK_INSTITUTIONS = REPO_ROOT / "scripts" / "agent_monitored_institutions.yaml"

SOURCE_TABLES = {
    "rts": "aegis-rts-data",
    "pillar3": "aegis-pillar3-data",
    "supplementary_financials": "aegis-financial-supp-data",
    "investor_slides": "aegis-investor-slides-data",
    "transcripts": "aegis-earnings-transcripts-data",
    "event_transcripts": "aegis-event-transcripts-data",
}
SOURCE_ORDER = {source_name: index for index, source_name in enumerate(SOURCE_TABLES)}

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

SCHEMA_FILES = [
    (
        "catalog",
        REPO_ROOT / "aegis-table-schemas" / "catalog" / "001_catalog_tables.sql",
        ("data_source_registry", "monitored_institutions", "data_source_availability"),
    ),
    (
        "prompts",
        REPO_ROOT / "aegis-table-schemas" / "runtime" / "prompts.sql",
        ("prompts",),
    ),
    (
        "process_monitor_logs",
        REPO_ROOT / "aegis-table-schemas" / "runtime" / "process_monitor_logs.sql",
        ("process_monitor_logs",),
    ),
    (
        "chat_runtime",
        REPO_ROOT / "aegis-table-schemas" / "runtime" / "001_chat_artifact_tables.sql",
        ("chat_conversations", "chat_messages", "artifacts"),
    ),
]

MEDIUM_ENV_DEFAULTS = {
    "LLM_MODEL_MEDIUM": "gpt-5-mini",
    "AGENT_LLM_TEMPERATURE_MEDIUM": "0.5",
    "AGENT_LLM_REASONING_EFFORT_MEDIUM": "low",
    "LLM_TEMPERATURE_MEDIUM": "",
    "LLM_MAX_TOKENS_MEDIUM": "2000",
    "LLM_TIMEOUT_MEDIUM": "60",
    "LLM_MAX_RETRIES_MEDIUM": "3",
    "LLM_COST_INPUT_MEDIUM": "0.0003",
    "LLM_COST_OUTPUT_MEDIUM": "0.0006",
    "LLM_REASONING_EFFORT_MEDIUM": "low",
}

BANK_ALIAS_OVERRIDES = {
    "RY": "RY-CA",
    "RBC": "RY-CA",
    "ROYAL BANK": "RY-CA",
    "BMO": "BMO-CA",
    "CM": "CM-CA",
    "CIBC": "CM-CA",
    "NA": "NA-CA",
    "NBC": "NA-CA",
    "BNS": "BNS-CA",
    "SCOTIA": "BNS-CA",
    "SCOTIABANK": "BNS-CA",
    "TD": "TD-CA",
}


@dataclass(frozen=True)
class AvailabilityRefresh:
    """Summary of refreshed V2 availability rows."""

    rows: int
    source_counts: dict[str, int]
    warnings: list[str]


def main(argv: list[str] | None = None) -> int:
    """Run the workstation update flow."""
    args = parse_args(argv)
    env_file = args.env_file.expanduser().resolve()
    if (
        env_file == DEFAULT_ENV_FILE.resolve()
        and not env_file.exists()
        and (AGENT_ROOT / ".env").exists()
    ):
        env_file = (AGENT_ROOT / ".env").resolve()
        args.skip_env_sync = True

    if not env_file.exists():
        raise FileNotFoundError(
            f"Env file not found: {env_file}. Copy .env.example to .env and fill local values first."
        )

    env_values = read_env(env_file)
    print("Aegis V2 workstation update")
    print(f"repo: {REPO_ROOT}")
    print(f"env:  {env_file}")

    ensure_medium_env(
        env_file, env_values, dry_run=args.dry_run, enabled=not args.no_env_patch
    )
    if not args.skip_env_sync:
        run_step(
            "sync env",
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "sync_env.py"),
                "--env-file",
                str(env_file),
            ],
            dry_run=args.dry_run,
        )

    env_values = read_env(env_file)
    if not args.skip_schema or not args.skip_catalog or not args.skip_availability:
        with psycopg2.connect(
            **database_config(env_values),
            application_name="aegis-v2-workstation-update",
        ) as conn:
            if not args.skip_schema:
                apply_schema_files(
                    conn, dry_run=args.dry_run, reapply=args.reapply_schema
                )
            if not args.skip_catalog:
                institutions_path = resolve_institutions_path(
                    args.monitored_institutions_yaml, env_values
                )
                institutions = load_institutions(institutions_path)
                print(
                    f"monitored institutions: {len(institutions)} from {institutions_path}"
                )
                if not args.dry_run:
                    upsert_catalog(conn, institutions)
                print(
                    f"catalog rows upserted: {len(SOURCE_ROWS)} sources, {len(institutions)} institutions"
                )
            if not args.skip_availability:
                institutions_by_ticker = monitored_tickers(conn)
                refresh = refresh_data_source_availability(
                    conn,
                    institutions_by_ticker,
                    replace=not args.merge_availability,
                    allow_missing_sources=args.allow_missing_sources,
                    allow_empty=args.allow_empty_availability,
                    dry_run=args.dry_run,
                )
                print(f"availability rows refreshed: {refresh.rows}")
                for source_name, count in refresh.source_counts.items():
                    print(f"  {source_name}: {count} bank-period row(s)")
                for warning in refresh.warnings[:20]:
                    print(f"warn: {warning}")
                if len(refresh.warnings) > 20:
                    print(
                        f"warn: {len(refresh.warnings) - 20} additional warning(s) suppressed"
                    )

    if not args.skip_prompts:
        sync_prompts(args.prompts_dir.expanduser().resolve(), args, env_file)

    if args.build_frontend:
        build_frontend(args)

    if not args.skip_server:
        start_server(args.port, args.host, dry_run=args.dry_run)
        if not args.skip_open and not args.dry_run:
            webbrowser.open(f"http://{args.host}:{args.port}/v2")
            print(f"opened http://{args.host}:{args.port}/v2")

    print("V2 workstation update complete")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--monitored-institutions-yaml", type=Path)
    parser.add_argument("--prompts-dir", type=Path, default=DEFAULT_PROMPTS_DIR)
    parser.add_argument("--port", type=int, default=8012)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned work without DB writes or process launch.",
    )
    parser.add_argument(
        "--no-env-patch",
        action="store_true",
        help="Do not append missing medium-tier keys to .env.",
    )
    parser.add_argument("--skip-env-sync", action="store_true")
    parser.add_argument("--skip-schema", action="store_true")
    parser.add_argument(
        "--reapply-schema",
        action="store_true",
        help="Run schema SQL even when target tables already exist.",
    )
    parser.add_argument("--skip-catalog", action="store_true")
    parser.add_argument("--skip-availability", action="store_true")
    parser.add_argument(
        "--merge-availability",
        action="store_true",
        help="Merge derived availability rows instead of replacing all rows.",
    )
    parser.add_argument(
        "--allow-missing-sources",
        action="store_true",
        help="Warn instead of failing if a processed source table is missing.",
    )
    parser.add_argument(
        "--allow-empty-availability",
        action="store_true",
        help="Allow refresh when no source availability rows are found.",
    )
    parser.add_argument("--skip-prompts", action="store_true")
    parser.add_argument(
        "--allow-empty-prompts",
        action="store_true",
        help="Do not fail when prompts-dir has no prompt YAML files.",
    )
    parser.add_argument(
        "--build-frontend",
        action="store_true",
        help="Run npm build for the V2 frontend.",
    )
    parser.add_argument(
        "--install-frontend-deps",
        action="store_true",
        help="Force npm dependency install before building the V2 frontend.",
    )
    parser.add_argument(
        "--skip-frontend-deps",
        action="store_true",
        help="Do not auto-install missing frontend dependencies before build.",
    )
    parser.add_argument("--skip-server", action="store_true")
    parser.add_argument("--skip-open", action="store_true")
    return parser.parse_args(argv)


def database_config(values: Mapping[str, str]) -> dict[str, str]:
    """Return psycopg2 connection args from root env values."""
    config = {
        "host": first(values, "POSTGRES_HOST", "DB_HOST", default="127.0.0.1"),
        "port": first(values, "POSTGRES_PORT", "DB_PORT", default="5432"),
        "dbname": first(values, "POSTGRES_DATABASE", "DB_NAME", default="postgres"),
        "user": first(values, "POSTGRES_USER", "DB_USER", default="postgres"),
        "password": first(values, "POSTGRES_PASSWORD", "DB_PASSWORD"),
    }
    return {key: value for key, value in config.items() if value}


def ensure_medium_env(
    path: Path, values: Mapping[str, str], *, dry_run: bool, enabled: bool
) -> None:
    """Append missing medium-tier model env keys for V2 model routing."""
    if not enabled:
        return
    defaults = dict(MEDIUM_ENV_DEFAULTS)
    if values.get("LLM_MODEL_LARGE"):
        defaults["LLM_MODEL_MEDIUM"] = values["LLM_MODEL_LARGE"]
    elif values.get("LLM_MODEL_SMALL"):
        defaults["LLM_MODEL_MEDIUM"] = values["LLM_MODEL_SMALL"]
    reasoning = first(
        values,
        "AGENT_LLM_REASONING_EFFORT_LARGE",
        "LLM_REASONING_EFFORT_LARGE",
        "AGENT_LLM_REASONING_EFFORT_SMALL",
        "LLM_REASONING_EFFORT_SMALL",
        default="low",
    )
    defaults["AGENT_LLM_REASONING_EFFORT_MEDIUM"] = reasoning
    defaults["LLM_REASONING_EFFORT_MEDIUM"] = reasoning

    missing = [key for key in defaults if key not in values]
    if not missing:
        print("medium model env: present")
        return

    print(f"medium model env: missing {', '.join(missing)}")
    if dry_run:
        print("dry-run: would append missing medium model keys to .env")
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n# Added by scripts/update_v2_workstation.py for V2 model routing.\n"
        )
        for key in missing:
            handle.write(f"{key}={defaults[key]}\n")
    print(f"medium model env: appended {len(missing)} key(s)")


def run_step(
    label: str, command: list[str], *, dry_run: bool, cwd: Path | None = None
) -> None:
    """Run a subprocess step with dry-run support."""
    rendered = " ".join(command)
    if dry_run:
        print(f"dry-run: {label}: {rendered}")
        return
    print(f"{label}: {rendered}")
    subprocess.run(command, cwd=str(cwd or REPO_ROOT), check=True)


def split_sql_statements(sql_text: str) -> list[str]:
    """Split SQL statements while preserving strings and dollar-quoted bodies."""
    statements: list[str] = []
    buffer: list[str] = []
    dollar_tag: str | None = None
    in_single_quote = False
    in_double_quote = False
    index = 0

    while index < len(sql_text):
        char = sql_text[index]
        if dollar_tag:
            if sql_text.startswith(dollar_tag, index):
                buffer.append(dollar_tag)
                index += len(dollar_tag)
                dollar_tag = None
                continue
            buffer.append(char)
            index += 1
            continue
        if in_single_quote:
            buffer.append(char)
            if char == "'" and sql_text[index + 1 : index + 2] == "'":
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
            match = re.match(r"\$[A-Za-z0-9_]*\$", sql_text[index:])
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


def table_exists(conn: PsycopgConnection, table_name: str) -> bool:
    """Return whether a public table exists."""
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


def column_exists(conn: PsycopgConnection, table_name: str, column_name: str) -> bool:
    """Return whether a public table contains a column."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = %s
                  AND column_name = %s
            )
            """,
            (table_name, column_name),
        )
        return bool(cur.fetchone()[0])


def apply_schema_files(
    conn: PsycopgConnection, *, dry_run: bool, reapply: bool
) -> None:
    """Create required V2 tables when missing."""
    for label, path, tables in SCHEMA_FILES:
        missing_tables = [table for table in tables if not table_exists(conn, table)]
        if not missing_tables and not reapply:
            print(f"schema {label}: present")
            continue
        if dry_run:
            target = ", ".join(missing_tables or tables)
            print(f"dry-run: would apply schema {path} for {target}")
            continue
        execute_sql_file(conn, path)
        print(f"schema {label}: applied {path.name}")


def execute_sql_file(conn: PsycopgConnection, path: Path) -> None:
    """Execute SQL file statements in one transaction."""
    statements = split_sql_statements(path.read_text(encoding="utf-8"))
    try:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def resolve_institutions_path(
    cli_path: Path | None, env_values: Mapping[str, str]
) -> Path:
    """Return the monitored institutions YAML path for this machine."""
    candidates = []
    if cli_path:
        candidates.append(cli_path.expanduser())
    configured = env_values.get("MONITORED_INSTITUTIONS_YAML")
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.extend(
        [
            DEFAULT_WORK_INSTITUTIONS,
            DEFAULT_WORK_INSTITUTIONS_SPACE,
            FALLBACK_INSTITUTIONS,
        ]
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            return resolved
    rendered = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(
        f"Could not find monitored institutions YAML. Checked: {rendered}"
    )


def slugify(value: str) -> str:
    """Return a stable lowercase identifier."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "institution"


def load_institutions(path: Path) -> list[dict[str, str]]:
    """Load monitored institution YAML rows for public.monitored_institutions."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected monitored institutions mapping at {path}")

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
        base_name = str(
            metadata.get("key") or metadata.get("bank_name") or slugify(display_name)
        )
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


def upsert_catalog(conn: PsycopgConnection, institutions: list[dict[str, str]]) -> None:
    """Upsert source registry and monitored institution rows."""
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO public.data_source_registry (
                data_source_name,
                data_source_display_name,
                data_source_description,
                updated_at
            )
            VALUES (
                %(data_source_name)s,
                %(data_source_display_name)s,
                %(data_source_description)s,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (data_source_name) DO UPDATE SET
                data_source_display_name = EXCLUDED.data_source_display_name,
                data_source_description = EXCLUDED.data_source_description,
                updated_at = CURRENT_TIMESTAMP
            """,
            SOURCE_ROWS,
        )
        cur.executemany(
            """
            INSERT INTO public.monitored_institutions (
                bank_ticker,
                bank_name,
                bank_display_name,
                bank_category,
                updated_at
            )
            VALUES (
                %(bank_ticker)s,
                %(bank_name)s,
                %(bank_display_name)s,
                %(bank_category)s,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (bank_ticker) DO UPDATE SET
                bank_name = EXCLUDED.bank_name,
                bank_display_name = EXCLUDED.bank_display_name,
                bank_category = EXCLUDED.bank_category,
                updated_at = CURRENT_TIMESTAMP
            """,
            institutions,
        )
    conn.commit()


def monitored_tickers(conn: PsycopgConnection) -> set[str]:
    """Return known monitored institution tickers."""
    if not table_exists(conn, "monitored_institutions"):
        return set()
    with conn.cursor() as cur:
        cur.execute("SELECT bank_ticker FROM public.monitored_institutions")
        return {str(row[0]) for row in cur.fetchall()}


def normalize_bank_ticker(raw_value: object, monitored: set[str]) -> str | None:
    """Normalize source table bank values to monitored bank_ticker values."""
    value = str(raw_value or "").strip()
    if not value:
        return None
    candidates = [
        value,
        value.upper(),
        value.split("_", 1)[0].upper(),
        value.split("/", 1)[0].upper(),
    ]
    alias = BANK_ALIAS_OVERRIDES.get(value.upper())
    if alias:
        candidates.insert(0, alias)
    upper = value.upper()
    for suffix in ("-CA", "-US", "-GB", "-FR", "-DE", "-ES", "-IT"):
        if not upper.endswith(suffix):
            candidates.append(f"{upper}{suffix}")
    for candidate in candidates:
        if candidate in monitored:
            return candidate
    return None


def parse_fiscal_year(value: object) -> int | None:
    """Return a four-digit fiscal year from a source table value."""
    if isinstance(value, int):
        return value
    match = re.search(r"20\d{2}", str(value or ""))
    return int(match.group(0)) if match else None


def normalize_quarter(value: object) -> str | None:
    """Normalize source table quarter values to Q1-Q4."""
    text = str(value or "").strip().upper()
    if text in {"1", "2", "3", "4"}:
        text = f"Q{text}"
    return text if text in {"Q1", "Q2", "Q3", "Q4"} else None


def refresh_data_source_availability(
    conn: PsycopgConnection,
    monitored: set[str],
    *,
    replace: bool,
    allow_missing_sources: bool,
    allow_empty: bool,
    dry_run: bool,
) -> AvailabilityRefresh:
    """Refresh V2 data_source_availability from live processed source tables."""
    if not monitored:
        raise RuntimeError(
            "No monitored institutions found. Seed catalog before refreshing availability."
        )

    availability: dict[tuple[str, int, str], set[str]] = {}
    source_counts: dict[str, int] = {}
    warnings: list[str] = []

    for source_name, table_name in SOURCE_TABLES.items():
        if not table_exists(conn, table_name):
            message = f'missing processed source table public."{table_name}" for {source_name}'
            if allow_missing_sources:
                warnings.append(message)
                continue
            raise RuntimeError(message)
        for required_column in ("bank", "fiscal_year", "quarter"):
            if not column_exists(conn, table_name, required_column):
                raise RuntimeError(
                    f'public."{table_name}" is missing required column {required_column!r}'
                )

        rows = source_period_rows(conn, table_name)
        source_row_count = 0
        unknown_banks: set[str] = set()
        for bank_raw, fiscal_year_raw, quarter_raw in rows:
            bank_ticker = normalize_bank_ticker(bank_raw, monitored)
            fiscal_year = parse_fiscal_year(fiscal_year_raw)
            quarter = normalize_quarter(quarter_raw)
            if not bank_ticker:
                unknown_banks.add(str(bank_raw))
                continue
            if fiscal_year is None or quarter is None:
                warnings.append(
                    f"{source_name}: skipped invalid period {bank_raw} {fiscal_year_raw} {quarter_raw}"
                )
                continue
            availability.setdefault((bank_ticker, fiscal_year, quarter), set()).add(
                source_name
            )
            source_row_count += 1
        source_counts[source_name] = source_row_count
        if unknown_banks:
            warnings.append(
                f"{source_name}: skipped unknown bank values {sorted(unknown_banks)[:12]}"
            )

    if not availability and not allow_empty:
        raise RuntimeError("No availability rows derived from source tables.")

    if dry_run:
        return AvailabilityRefresh(
            rows=len(availability), source_counts=source_counts, warnings=warnings
        )

    with conn.cursor() as cur:
        if replace:
            cur.execute("DELETE FROM public.data_source_availability")
        cur.executemany(
            """
            INSERT INTO public.data_source_availability (
                bank_ticker,
                fiscal_year,
                quarter,
                data_source_list,
                updated_at
            )
            VALUES (
                %(bank_ticker)s,
                %(fiscal_year)s,
                %(quarter)s,
                %(data_source_list)s,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT (bank_ticker, fiscal_year, quarter) DO UPDATE SET
                data_source_list = EXCLUDED.data_source_list,
                updated_at = CURRENT_TIMESTAMP
            """,
            [
                {
                    "bank_ticker": bank_ticker,
                    "fiscal_year": fiscal_year,
                    "quarter": quarter,
                    "data_source_list": sorted(
                        sources, key=lambda item: SOURCE_ORDER[item]
                    ),
                }
                for (bank_ticker, fiscal_year, quarter), sources in sorted(
                    availability.items()
                )
            ],
        )
    conn.commit()
    return AvailabilityRefresh(
        rows=len(availability), source_counts=source_counts, warnings=warnings
    )


def source_period_rows(
    conn: PsycopgConnection, table_name: str
) -> list[tuple[object, object, object]]:
    """Return distinct source bank/period rows."""
    query = sql.SQL(
        """
        SELECT DISTINCT bank, fiscal_year, quarter
        FROM public.{table}
        WHERE bank IS NOT NULL
          AND fiscal_year IS NOT NULL
          AND quarter IS NOT NULL
        """
    ).format(table=sql.Identifier(table_name))
    with conn.cursor() as cur:
        cur.execute(query)
        return list(cur.fetchall())


def sync_prompts(prompts_dir: Path, args: argparse.Namespace, env_file: Path) -> None:
    """Push prompt YAMLs into public.prompts when prompt files exist."""
    yaml_files = sorted(prompts_dir.rglob("*.yaml")) + sorted(
        prompts_dir.rglob("*.yml")
    )
    if not yaml_files:
        message = f"No prompt YAML files found under {prompts_dir}; prompts table was created but no prompts were pushed."
        if args.allow_empty_prompts:
            print(f"warn: {message}")
            return
        raise RuntimeError(
            f"{message} Add V2 prompt YAMLs or pass --allow-empty-prompts / --prompts-dir archive/v1/aegis-prompts."
        )
    command = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "push_aegis_prompts.py"),
        "--env-file",
        str(env_file),
        "--prompts-dir",
        str(prompts_dir),
    ]
    run_step("push prompts", command, dry_run=args.dry_run)


def build_frontend(args: argparse.Namespace) -> None:
    """Build the V2 frontend bundle."""
    frontend_root = AGENT_ROOT / "frontend"
    npm_command = shutil.which("npm")
    if not npm_command:
        raise RuntimeError(
            "npm was not found on PATH. Install Node.js/npm on this workstation, "
            "or rerun without --build-frontend to use the committed frontend bundle."
        )

    package_lock = frontend_root / "package-lock.json"
    install_command = (
        [npm_command, "ci"] if package_lock.exists() else [npm_command, "install"]
    )
    vite_bin = frontend_root / "node_modules" / ".bin" / "vite"
    tsc_bin = frontend_root / "node_modules" / ".bin" / "tsc"
    missing_deps = not vite_bin.exists() or not tsc_bin.exists()

    if args.install_frontend_deps or (missing_deps and not args.skip_frontend_deps):
        reason = (
            "forced"
            if args.install_frontend_deps
            else "missing node_modules/.bin build tools"
        )
        print(f"frontend dependencies: installing ({reason})")
        run_step(
            "frontend dependencies",
            install_command,
            dry_run=args.dry_run,
            cwd=frontend_root,
        )
    elif missing_deps:
        print(
            "warn: frontend dependencies appear to be missing, but --skip-frontend-deps was passed; "
            "npm run build may fail with exit status 127."
        )
    run_step(
        "frontend build",
        [npm_command, "run", "build"],
        dry_run=args.dry_run,
        cwd=frontend_root,
    )


def port_is_open(host: str, port: int) -> bool:
    """Return whether a local TCP port is accepting connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) == 0


def start_server(port: int, host: str, *, dry_run: bool) -> None:
    """Start the FastAPI server unless the target port already responds."""
    if dry_run:
        print(f"dry-run: would start server on http://{host}:{port}/v2")
        return
    if port_is_open(host, port):
        print(f"server: http://{host}:{port}/v2 already appears to be running")
        return

    log_dir = AGENT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"v2_server_{port}.log"
    log_handle = log_path.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "run_fastapi.py", "--port", str(port)],
        cwd=str(AGENT_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"server started: pid={process.pid} log={log_path}")


if __name__ == "__main__":
    raise SystemExit(main())
