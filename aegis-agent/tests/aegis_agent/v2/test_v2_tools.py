"""Tests for Aegis V2 availability and document tools."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.exc import ProgrammingError

from aegis_agent.v2.schemas import AvailabilityFilters, DocumentFilters
from aegis_agent.v2 import mock_data
from aegis_agent.v2.tools import availability, documents


@pytest.mark.asyncio
async def test_check_data_availability_filters_sources_and_reports_missing(monkeypatch) -> None:
    """Availability should expose selected sources and missing source gaps."""
    async def fake_fetch_all(_query, _params=None, _execution_id=None):
        return [
            {
                "bank_id": 1,
                "bank_name": "Royal Bank of Canada",
                "bank_symbol": "RY-CA",
                "bank_tags": ["canadian_bank", "rbc"],
                "fiscal_year": 2026,
                "quarter": "Q1",
                "database_names": ["investor_slides", "rts"],
                "last_updated": datetime(2026, 6, 1, 12, 0),
            }
        ]

    monkeypatch.setattr(availability, "fetch_all", fake_fetch_all)
    response = await availability.check_data_availability(
        AvailabilityFilters(source_ids=["investor_slides", "rts", "pillar3"])
    )

    assert len(response.rows) == 1
    assert response.rows[0].source_ids == ["investor_slides", "rts"]
    assert response.rows[0].bank_category == "Canadian Banks"
    assert response.missing[0].missing_source_ids == ["pillar3"]


@pytest.mark.asyncio
async def test_check_data_availability_category_filter_without_matches_short_circuits(monkeypatch) -> None:
    """Unknown category filters should avoid returning unrelated rows."""
    captured = {}

    async def fake_fetch_all(query, params=None, _execution_id=None):
        captured["query"] = query
        captured["params"] = params or {}
        return []

    monkeypatch.setattr(availability, "fetch_all", fake_fetch_all)
    response = await availability.check_data_availability(
        AvailabilityFilters(bank_categories=["Not A Real Category"])
    )

    assert "1=0" in captured["query"]
    assert response.rows == []


@pytest.mark.asyncio
async def test_list_documents_builds_preview_and_download_urls(monkeypatch) -> None:
    """Document summaries should point at the existing preview/download routes."""
    async def fake_fetch_all(query, _params=None, _execution_id=None):
        if "to_regclass" in query:
            return [{"table_name": "aegis_source_documents"}]
        if "aegis_source_documents" not in query:
            return []
        return [
            {
                "source_type": "investor_slides",
                "file_id": "file-1",
                "fiscal_year": "2026",
                "quarter": "Q1",
                "bank": "RY-CA",
                "filename": "rbc_q1_2026_investor_slides.pdf",
                "file_type": "pdf",
                "file_path": "aegis-documents/investor_slides/2026_Q1/RY-CA/file.pdf",
                "preview_mime_type": "application/pdf",
                "preview_error": None,
                "updated_at": datetime(2026, 6, 1, 12, 0),
            }
        ]

    monkeypatch.setattr(documents, "fetch_all", fake_fetch_all)
    response = await documents.list_documents(DocumentFilters(source_ids=["investor_slides"]))

    assert response.total == 1
    document = response.documents[0]
    assert document.preview_status == "ready"
    assert document.preview_url == "/source-documents/investor_slides/file-1/preview"
    assert document.download_url == "/source-documents/investor_slides/file-1/download"


@pytest.mark.asyncio
async def test_list_documents_returns_empty_when_preview_table_is_missing(monkeypatch) -> None:
    """The V2 file explorer should not crash before preview storage is migrated."""
    async def fake_fetch_all(_query, _params=None, _execution_id=None):
        raise ProgrammingError("select 1", {}, Exception("missing table"))

    monkeypatch.setattr(documents, "fetch_all", fake_fetch_all)
    response = await documents.list_documents(DocumentFilters())

    assert response.documents == []
    assert response.total == 0


def test_mock_document_catalog_defines_file_explorer_contract() -> None:
    """Mock documents should exercise the same contract the file explorer renders."""
    response = mock_data.mock_document_search_response(
        DocumentFilters(
            source_ids=["investor_slides"],
            bank_symbols=["RY-CA"],
            fiscal_years=[2026],
            quarters=["Q1"],
        )
    )

    assert response.total == 1
    document = response.documents[0]
    assert document.file_id.startswith("mock-investor_slides-2026-q1-ry-ca")
    assert document.bank_category == "Canadian Banks"
    assert document.preview_status == "ready"
    assert document.preview_url == f"/source-documents/{document.source_id}/{document.file_id}/preview"
    assert document.download_url == f"/source-documents/{document.source_id}/{document.file_id}/download"


def test_mock_source_document_record_serves_html_preview() -> None:
    """Mock source documents should be previewable through the existing route shape."""
    document = mock_data.mock_document_search_response(
        DocumentFilters(source_ids=["transcripts"], bank_symbols=["JPM"], fiscal_years=[2026], quarters=["Q1"])
    ).documents[0]

    record = mock_data.mock_source_document_record(document.source_id, document.file_id)

    assert record is not None
    assert record["preview_mime_type"] == "text/html"
    assert document.filename.encode("utf-8") in record["preview_bytes"]
