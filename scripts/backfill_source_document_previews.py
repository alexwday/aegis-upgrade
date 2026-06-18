#!/usr/bin/env python3
"""Backfill browser-preview bytes for stored Aegis source documents."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import Binary, sql
from psycopg2.extensions import connection as PsycopgConnection
from psycopg2.extras import Json

from retrieval_source_config import AEGIS_PIPELINE_ROOT, PROJECT_ROOT, SOURCES
from sync_env import PIPELINE_SOURCES, first, read_env

if str(AEGIS_PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(AEGIS_PIPELINE_ROOT))

from utils.source_document_previews import (  # noqa: E402
    PDF_MIME_TYPE,
    PREVIEW_RENDERER_VERSION,
    build_source_document_preview,
    preview_error_metadata,
)


PUBLIC_SCHEMA = "public"
DOCUMENTS_TABLE = "aegis_source_documents"
REQUIRED_PREVIEW_COLUMNS = {
    "preview_mime_type",
    "preview_bytes",
    "preview_metadata",
    "preview_generated_at",
    "preview_error",
}


@dataclass(frozen=True)
class Candidate:
    """One source document selected for preview backfill."""

    source_key: str
    source_type: str
    file_id: str
    filename: str
    file_type: str
    mime_type: str
    file_hash: str
    original_bytes: Any | None = None


@dataclass
class BackfillCounts:
    """Backfill result counters."""

    scanned: int = 0
    generated: int = 0
    failed: int = 0


def main(argv: list[str] | None = None) -> int:
    """Run the preview backfill."""
    args = parse_args(argv)
    env_file = args.env_file.expanduser().resolve()
    values = read_env(env_file)
    db_config = database_config(values)
    selected_keys = selected_source_keys(args)

    with psycopg2.connect(
        **db_config,
        application_name="aegis-source-document-preview-backfill",
    ) as conn:
        conn.autocommit = False
        existing_columns = source_document_columns(conn)
        missing_columns = REQUIRED_PREVIEW_COLUMNS - existing_columns

        if missing_columns and not args.apply:
            print(
                "Preview columns are missing: "
                f"{', '.join(sorted(missing_columns))}"
            )
            print("--apply will add the columns and backfill matching rows.")
            print_missing_column_dry_run(conn, selected_keys)
            conn.rollback()
            return 0

        if missing_columns:
            ensure_preview_columns(conn)
            conn.commit()
            print("Added missing preview columns.")

        total = BackfillCounts()
        for source_key in selected_keys:
            candidates = load_candidates(
                conn,
                source_key=source_key,
                include_original=args.apply,
                force=args.force,
                limit=args.limit,
            )
            total.scanned += len(candidates)
            print_candidate_summary(source_key, candidates, dry_run=not args.apply)
            if not args.apply:
                continue
            counts = apply_backfill(conn, candidates)
            total.generated += counts.generated
            total.failed += counts.failed

    if args.apply:
        print(
            "Backfill complete: "
            f"generated={total.generated}, failed={total.failed}, "
            f"selected={total.scanned}"
        )
    else:
        print(f"Dry run complete: selected={total.scanned}")
        print("Run with --apply to write preview bytes.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=PROJECT_ROOT / ".env",
        help="Root dotenv file with DB or POSTGRES settings.",
    )
    parser.add_argument(
        "--source",
        action="append",
        choices=tuple(SOURCES),
        help="Backfill one source key. May be provided more than once.",
    )
    parser.add_argument(
        "--all",
        dest="all_sources",
        action="store_true",
        help="Backfill all configured retrieval sources.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write preview fields. Omit for dry-run counts.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate all selected previews even if they look current.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit candidate rows per selected source.",
    )
    args = parser.parse_args(argv)
    if not args.all_sources and not args.source:
        parser.error("Choose --all or at least one --source.")
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be positive when provided.")
    return args


def selected_source_keys(args: argparse.Namespace) -> list[str]:
    """Return source keys selected by CLI options in stable source order."""
    if args.all_sources:
        return list(SOURCES)
    requested = set(args.source or [])
    return [key for key in SOURCES if key in requested]


def database_config(values: Mapping[str, str]) -> dict[str, str]:
    """Return psycopg2 connection settings from the root dotenv values."""
    config = {
        "host": first(values, "DB_HOST", "POSTGRES_HOST", default="127.0.0.1"),
        "port": first(values, "DB_PORT", "POSTGRES_PORT", default="5432"),
        "dbname": first(values, "DB_NAME", "POSTGRES_DATABASE", default="postgres"),
        "user": first(values, "DB_USER", "POSTGRES_USER", default="postgres"),
        "password": first(values, "DB_PASSWORD", "POSTGRES_PASSWORD"),
    }
    return {key: value for key, value in config.items() if value}


def source_document_columns(conn: PsycopgConnection) -> set[str]:
    """Return columns currently present on the source document byte table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            """,
            (PUBLIC_SCHEMA, DOCUMENTS_TABLE),
        )
        return {str(row[0]) for row in cur.fetchall()}


def ensure_preview_columns(conn: PsycopgConnection) -> None:
    """Add preview columns required by the current two-byte document model."""
    statements = [
        "ALTER TABLE public.aegis_source_documents "
        "ADD COLUMN IF NOT EXISTS preview_mime_type text",
        "ALTER TABLE public.aegis_source_documents "
        "ADD COLUMN IF NOT EXISTS preview_bytes bytea",
        "ALTER TABLE public.aegis_source_documents "
        "ADD COLUMN IF NOT EXISTS preview_metadata jsonb NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE public.aegis_source_documents "
        "ADD COLUMN IF NOT EXISTS preview_generated_at timestamptz",
        "ALTER TABLE public.aegis_source_documents "
        "ADD COLUMN IF NOT EXISTS preview_error text",
    ]
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)


def print_missing_column_dry_run(
    conn: PsycopgConnection,
    source_keys: Sequence[str],
) -> None:
    """Print counts without referencing not-yet-created preview columns."""
    for source_key in source_keys:
        candidates = load_all_documents_without_preview_columns(conn, source_key)
        print_candidate_summary(source_key, candidates, dry_run=True)


def load_all_documents_without_preview_columns(
    conn: PsycopgConnection,
    source_key: str,
) -> list[Candidate]:
    """Return all documents for one source when preview columns are absent."""
    source_type = source_type_for_key(source_key)
    query = sql.SQL(
        """
        SELECT
            source_type,
            file_id,
            filename,
            file_type,
            mime_type,
            file_hash,
            NULL::bytea AS original_bytes
        FROM {}
        WHERE source_type = %s
        ORDER BY file_type, file_id
        """
    ).format(table_ref(DOCUMENTS_TABLE))
    with conn.cursor() as cur:
        cur.execute(query, (source_type,))
        return candidates_from_rows(cur.fetchall(), source_key)


def load_candidates(
    conn: PsycopgConnection,
    *,
    source_key: str,
    include_original: bool,
    force: bool,
    limit: int | None,
) -> list[Candidate]:
    """Return rows needing preview generation for one source."""
    source_type = source_type_for_key(source_key)
    original_column = (
        sql.SQL("original_bytes")
        if include_original
        else sql.SQL("NULL::bytea AS original_bytes")
    )
    stale_filter = sql.SQL("")
    params: list[Any] = [source_type]
    if not force:
        stale_filter = sql.SQL(
            """
            AND (
                preview_bytes IS NULL
             OR preview_mime_type IS DISTINCT FROM %s
             OR coalesce(preview_metadata->>'renderer_version', '') <> %s
             OR preview_error IS NOT NULL
            )
            """
        )
        params.extend([PDF_MIME_TYPE, PREVIEW_RENDERER_VERSION])

    limit_sql = sql.SQL("")
    if limit is not None:
        limit_sql = sql.SQL("LIMIT %s")
        params.append(limit)

    query = sql.SQL(
        """
        SELECT
            source_type,
            file_id,
            filename,
            file_type,
            mime_type,
            file_hash,
            {}
        FROM {}
        WHERE source_type = %s
        {}
        ORDER BY file_type, file_id
        {}
        """
    ).format(original_column, table_ref(DOCUMENTS_TABLE), stale_filter, limit_sql)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return candidates_from_rows(cur.fetchall(), source_key)


def candidates_from_rows(rows: Sequence[tuple[Any, ...]], source_key: str) -> list[Candidate]:
    """Convert query rows into candidate records."""
    return [
        Candidate(
            source_key=source_key,
            source_type=str(row[0]),
            file_id=str(row[1]),
            filename=str(row[2]),
            file_type=str(row[3]),
            mime_type=str(row[4]),
            file_hash=str(row[5]),
            original_bytes=row[6],
        )
        for row in rows
    ]


def print_candidate_summary(
    source_key: str,
    candidates: Sequence[Candidate],
    *,
    dry_run: bool,
) -> None:
    """Print compact candidate counts for one source."""
    source = SOURCES[source_key]
    mode = "would backfill" if dry_run else "selected"
    counts = Counter(candidate.file_type.lower() for candidate in candidates)
    if counts:
        detail = ", ".join(
            f"{file_type or 'unknown'}={count}"
            for file_type, count in sorted(counts.items())
        )
    else:
        detail = "none"
    print(f"{source.key}: {mode} {len(candidates)} document(s) ({detail})")


def apply_backfill(
    conn: PsycopgConnection,
    candidates: Sequence[Candidate],
) -> BackfillCounts:
    """Generate and persist previews for selected source documents."""
    counts = BackfillCounts(scanned=len(candidates))
    for candidate in candidates:
        original_bytes = original_bytes_from_candidate(candidate)
        try:
            preview = build_source_document_preview(
                original_bytes=original_bytes,
                filename=candidate.filename,
                file_type=candidate.file_type,
                mime_type=candidate.mime_type,
                source_type=candidate.source_type,
                file_hash=candidate.file_hash,
            )
        except Exception as exc:
            record_preview_failure(conn, candidate, exc)
            conn.commit()
            counts.failed += 1
            print(f"{candidate.source_key}/{candidate.file_id}: {type(exc).__name__}: {exc}")
            continue

        update_preview_success(
            conn,
            candidate,
            preview_mime_type=preview.preview_mime_type,
            preview_bytes=preview.preview_bytes,
            preview_metadata=preview.preview_metadata,
        )
        conn.commit()
        counts.generated += 1
    return counts


def original_bytes_from_candidate(candidate: Candidate) -> bytes:
    """Return bytes loaded from PostgreSQL bytea."""
    if candidate.original_bytes is None:
        raise ValueError(f"Missing original_bytes for {candidate.file_id}")
    if isinstance(candidate.original_bytes, memoryview):
        return candidate.original_bytes.tobytes()
    return bytes(candidate.original_bytes)


def update_preview_success(
    conn: PsycopgConnection,
    candidate: Candidate,
    *,
    preview_mime_type: str,
    preview_bytes: bytes,
    preview_metadata: Mapping[str, Any],
) -> None:
    """Persist generated preview bytes and clear any previous error."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                UPDATE {}
                SET preview_mime_type = %s,
                    preview_bytes = %s,
                    preview_metadata = %s,
                    preview_generated_at = now(),
                    preview_error = NULL,
                    updated_at = now()
                WHERE source_type = %s
                  AND file_id = %s
                """
            ).format(table_ref(DOCUMENTS_TABLE)),
            (
                preview_mime_type,
                Binary(preview_bytes),
                Json(dict(preview_metadata)),
                candidate.source_type,
                candidate.file_id,
            ),
        )


def record_preview_failure(
    conn: PsycopgConnection,
    candidate: Candidate,
    error: BaseException,
) -> None:
    """Persist preview generation failure without changing original bytes."""
    with conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                """
                UPDATE {}
                SET preview_mime_type = NULL,
                    preview_bytes = NULL,
                    preview_metadata = %s,
                    preview_generated_at = now(),
                    preview_error = %s,
                    updated_at = now()
                WHERE source_type = %s
                  AND file_id = %s
                """
            ).format(table_ref(DOCUMENTS_TABLE)),
            (
                Json(
                    preview_error_metadata(
                        filename=candidate.filename,
                        file_type=candidate.file_type,
                        mime_type=candidate.mime_type,
                        source_type=candidate.source_type,
                        file_hash=candidate.file_hash,
                        error=error,
                    )
                ),
                error_text(error),
                candidate.source_type,
                candidate.file_id,
            ),
        )


def source_type_for_key(source_key: str) -> str:
    """Return the pipeline source_type stored in aegis_source_documents."""
    return str(PIPELINE_SOURCES[source_key]["data_source"])


def error_text(error: BaseException) -> str:
    """Return a compact DB-safe error string."""
    return f"{type(error).__name__}: {error}"[:4000]


def table_ref(table_name: str) -> sql.Identifier:
    """Return a public-schema table reference."""
    return sql.Identifier(PUBLIC_SCHEMA, table_name)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
