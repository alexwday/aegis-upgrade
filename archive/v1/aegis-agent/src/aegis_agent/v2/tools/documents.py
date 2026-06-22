"""Document browser and search tool for Aegis V2."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy.exc import SQLAlchemyError

from ...connections.postgres_connector import fetch_all
from ..schemas import DocumentFilters, DocumentSearchResponse, DocumentSummary
from ..sources import SOURCE_TABLES, normalize_source_ids, source_label
from .availability import bank_category, banks_for_categories


def _preview_url(source_id: str, file_id: str) -> str:
    """Return browser preview URL for a source document."""
    return f"/source-documents/{quote(source_id, safe='')}/{quote(file_id, safe='')}/preview"


def _download_url(source_id: str, file_id: str) -> str:
    """Return original-byte download URL for a source document."""
    return f"/source-documents/{quote(source_id, safe='')}/{quote(file_id, safe='')}/download"


def _preview_status(record: dict[str, Any]) -> str:
    """Return source document preview status."""
    if record.get("preview_error"):
        return "error"
    if record.get("preview_mime_type"):
        return "ready"
    return "missing"


def _category_key(value: str) -> str:
    """Normalize category values for comparison."""
    return str(value or "").strip().replace(" ", "_").lower()


def _matches_keyword(record: dict[str, Any], keyword: str | None, content_file_ids: set[tuple[str, str]]) -> bool:
    """Return whether a document matches metadata or content keyword search."""
    if not keyword:
        return True
    source_id = str(record.get("source_type") or "")
    file_id = str(record.get("file_id") or "")
    if (source_id, file_id) in content_file_ids:
        return True

    term = keyword.lower().strip()
    if not term:
        return True
    haystack = " ".join(
        str(record.get(field) or "")
        for field in ("source_type", "bank", "fiscal_year", "quarter", "filename", "file_type", "file_path")
    ).lower()
    return term in haystack or _is_subsequence(term, haystack)


def _is_subsequence(needle: str, haystack: str) -> bool:
    """Tiny fuzzy match for short analyst search terms."""
    if len(needle) < 3:
        return False
    cursor = 0
    for char in haystack:
        if cursor < len(needle) and needle[cursor] == char:
            cursor += 1
    return cursor == len(needle)


def _document_from_record(record: dict[str, Any]) -> DocumentSummary:
    """Convert a source document row into a V2 document summary."""
    source_id = str(record["source_type"])
    bank_symbol = str(record["bank"])
    category, _ = bank_category(bank_symbol, [])
    updated_at = record.get("updated_at")
    if updated_at is not None and not isinstance(updated_at, datetime):
        updated_at = None
    return DocumentSummary(
        source_id=source_id,
        source_label=source_label(source_id),
        file_id=str(record["file_id"]),
        bank_symbol=bank_symbol,
        bank_category=category,
        fiscal_year=str(record["fiscal_year"]),
        quarter=str(record["quarter"]),
        filename=str(record["filename"]),
        file_type=str(record["file_type"]),
        preview_url=_preview_url(source_id, str(record["file_id"])),
        download_url=_download_url(source_id, str(record["file_id"])),
        preview_status=_preview_status(record),  # type: ignore[arg-type]
        preview_error=record.get("preview_error"),
        updated_at=updated_at,
    )


def _build_document_where(filters: DocumentFilters, category_banks: set[str]) -> tuple[str, dict[str, Any]]:
    """Build safe SQL predicates for source document filters."""
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": filters.limit}

    selected_sources = normalize_source_ids(filters.source_ids)
    if selected_sources:
        clauses.append("source_type = ANY(:source_ids)")
        params["source_ids"] = selected_sources

    bank_symbols = [symbol.upper() for symbol in filters.bank_symbols]
    if category_banks:
        bank_symbols = sorted(set(bank_symbols).intersection(category_banks)) if bank_symbols else sorted(category_banks)
    if bank_symbols:
        clauses.append("bank = ANY(:bank_symbols)")
        params["bank_symbols"] = bank_symbols
    elif filters.bank_categories:
        return "1=0", params

    if filters.fiscal_years:
        clauses.append("fiscal_year = ANY(:fiscal_years)")
        params["fiscal_years"] = [str(year) for year in filters.fiscal_years]
    if filters.quarters:
        clauses.append("quarter = ANY(:quarters)")
        params["quarters"] = [quarter.upper() for quarter in filters.quarters]

    return " AND ".join(clauses), params


async def _table_exists(regclass_name: str) -> bool:
    """Return whether a public table exists without raising relation errors."""
    try:
        records = await fetch_all(
            "SELECT to_regclass(:regclass_name) AS table_name",
            {"regclass_name": regclass_name},
        )
    except SQLAlchemyError:
        return False
    return bool(records and records[0].get("table_name"))


async def _content_file_ids(filters: DocumentFilters) -> set[tuple[str, str]]:
    """Search source content tables for keyword hits and return source/file ids."""
    keyword = str(filters.keyword or "").strip()
    if not keyword:
        return set()

    term = f"%{keyword}%"
    selected_sources = normalize_source_ids(filters.source_ids)
    source_ids = selected_sources or list(SOURCE_TABLES)
    matches: set[tuple[str, str]] = set()

    for source_id in source_ids:
        table_name = SOURCE_TABLES.get(source_id)
        if not table_name:
            continue
        if not await _table_exists(f'public."{table_name}"'):
            continue
        clauses = [
            """
            (
                name ILIKE :term OR
                summary ILIKE :term OR
                chunk_content ILIKE :term OR
                keywords::text ILIKE :term OR
                metrics::text ILIKE :term
            )
            """
        ]
        params: dict[str, Any] = {"term": term, "limit": filters.limit}
        if filters.bank_symbols:
            clauses.append("bank = ANY(:bank_symbols)")
            params["bank_symbols"] = [symbol.upper() for symbol in filters.bank_symbols]
        if filters.fiscal_years:
            clauses.append("fiscal_year = ANY(:fiscal_years)")
            params["fiscal_years"] = [str(year) for year in filters.fiscal_years]
        if filters.quarters:
            clauses.append("quarter = ANY(:quarters)")
            params["quarters"] = [quarter.upper() for quarter in filters.quarters]

        try:
            records = await fetch_all(
                f"""
                SELECT DISTINCT file_id
                FROM public."{table_name}"
                WHERE {" AND ".join(clauses)}
                LIMIT :limit
                """,
                params,
            )
        except SQLAlchemyError:
            continue

        for record in records:
            file_id = str(record.get("file_id") or "").strip()
            if file_id:
                matches.add((source_id, file_id))

    return matches


async def list_documents(filters: DocumentFilters | None = None) -> DocumentSearchResponse:
    """List previewable source documents for the V2 file explorer."""
    filters = filters or DocumentFilters()
    if not await _table_exists("public.aegis_source_documents"):
        return DocumentSearchResponse(documents=[], total=0)

    category_banks = banks_for_categories(filters.bank_categories)
    where_clause, params = _build_document_where(filters, category_banks)
    try:
        records = await fetch_all(
            f"""
            SELECT
                source_type,
                file_id,
                fiscal_year,
                quarter,
                bank,
                filename,
                file_type,
                file_path,
                preview_mime_type,
                preview_error,
                updated_at
            FROM public.aegis_source_documents
            WHERE {where_clause}
            ORDER BY updated_at DESC, source_type ASC, bank ASC, fiscal_year DESC, quarter DESC
            LIMIT :limit
            """,
            params,
        )
    except SQLAlchemyError:
        return DocumentSearchResponse(documents=[], total=0)

    content_matches = await _content_file_ids(filters)
    documents = [
        _document_from_record(record)
        for record in records
        if _matches_keyword(record, filters.keyword, content_matches)
    ]
    category_filters = {_category_key(category) for category in filters.bank_categories}
    if category_filters:
        documents = [
            document
            for document in documents
            if _category_key(document.bank_category) in category_filters
        ]

    return DocumentSearchResponse(documents=documents[: filters.limit], total=len(documents))
