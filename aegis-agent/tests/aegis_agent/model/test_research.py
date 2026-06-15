"""Tests for run_research availability and evidence-link paths."""

from __future__ import annotations

import asyncio

import pytest

from aegis_agent.model.agents.research import (
    _aggregate_results,
    _result_from_document_raw,
    run_research_tool,
)
from aegis_agent.model.agents.schemas import (
    BankPeriodCombination,
)


DOCUMENT_SOURCES = ["investor_slides", "supplementary_financials", "rts", "pillar3"]


def _args(*symbols: str) -> dict:
    return {
        "question": "credit quality",
        "sources": ["transcripts"],
        "combinations": [
            {"bank_symbol": symbol, "fiscal_year": 2026, "quarter": "Q1"} for symbol in symbols
        ],
    }


def _combo(symbol: str) -> BankPeriodCombination:
    return BankPeriodCombination(bank_symbol=symbol, fiscal_year=2026, quarter="Q1")


def test_document_adapter_converts_references_to_evidence_refs() -> None:
    """Document findings should preserve filename/page metadata as linked evidence refs."""
    raw = {
        "combo_results": [
            {
                "combo": {"bank_symbol": "RY-CA", "fiscal_year": 2026, "quarter": "Q1"},
                "findings": [
                    {
                        "finding": "CET1 ratio was 13.7%.",
                        "page": 9,
                        "location_detail": "Capital",
                        "source_ref_ids": ["S1"],
                        "metric_name": "CET1 ratio",
                        "metric_value": "13.7",
                        "unit": "%",
                        "period": "Q1 2026",
                        "segment": "Enterprise",
                        "details": "Management highlighted stable capital generation.",
                        "references": [
                            {
                                "filename": "rbc_q1_2026_investor_slides.pdf",
                                "s3_key": "rbc_q1_2026_investor_slides.pdf",
                                "file_type": "pdf",
                                "page": 9,
                                "location": "Capital",
                            }
                        ],
                    }
                ],
                "expanded_chunks": [{"name": "Capital", "chunk_id": "p9"}],
            }
        ]
    }

    result = _result_from_document_raw("investor_slides", raw, [], "")

    reference = result.findings[0].evidence_refs[0]
    assert reference.source_id == "investor_slides"
    assert reference.filename == "rbc_q1_2026_investor_slides.pdf"
    assert reference.page_number == 9
    assert reference.display_label == "Investor slides p.9"
    assert reference.href and reference.href.endswith("rbc_q1_2026_investor_slides.pdf#page=9")
    assert result.findings[0].finding_type == "quantitative"
    assert result.findings[0].metric
    assert result.findings[0].metric.metric_name == "CET1 ratio"
    assert result.findings[0].metric.metric_value == "13.7"
    assert result.findings[0].details == "Management highlighted stable capital generation."


def test_document_adapter_preserves_table_payloads() -> None:
    """Document findings should preserve optional table payloads for tabular answers."""
    raw = {
        "combo_results": [
            {
                "combo": {"bank_symbol": "RY-CA", "fiscal_year": 2026, "quarter": "Q1"},
                "findings": [
                    {
                        "finding": "Capital metrics were disclosed in a compact table.",
                        "finding_type": "table",
                        "page": 9,
                        "location_detail": "Capital table",
                        "source_ref_ids": ["S1"],
                        "table": {
                            "title": "Capital metrics",
                            "columns": ["Metric", "Q1 2026"],
                            "rows": [{"Metric": "CET1", "Q1 2026": "13.7%"}],
                            "notes": "As reported.",
                        },
                    }
                ],
                "expanded_chunks": [{"name": "Capital table", "chunk_id": "p9"}],
            }
        ]
    }

    result = _result_from_document_raw("investor_slides", raw, [], "")

    finding = result.findings[0]
    assert finding.finding_type == "table"
    assert finding.table
    assert finding.table.title == "Capital metrics"
    assert finding.table.rows[0]["Q1 2026"] == "13.7%"


def test_aggregate_assigns_unique_evidence_ids_across_sources() -> None:
    """The aggregator should assign turn-local E# IDs before the agent sees results."""
    raw = {
        "combo_results": [
            {
                "combo": {"bank_symbol": "RY-CA", "fiscal_year": 2026, "quarter": "Q1"},
                "findings": [
                    {
                        "finding": "RWA was $734.7B.",
                        "page": 3,
                        "location_detail": "Page_3",
                        "source_ref_ids": ["S1"],
                    }
                ],
                "expanded_chunks": [{"name": "Page_3", "chunk_id": "sheet_1"}],
            }
        ]
    }
    source_result = _result_from_document_raw("supplementary_financials", raw, [], "")

    aggregate = _aggregate_results([source_result])

    assert aggregate.findings[0].evidence_refs[0].evidence_id == "E1"
    assert "E1" in aggregate.evidence_registry["supplementary_financials"]
    assert aggregate.evidence_registry["supplementary_financials"]["E1"].display_label
    assert aggregate.citations[0].evidence_id == "E1"


@pytest.mark.asyncio
async def test_run_research_success(monkeypatch) -> None:
    """Research should return success when all available combos complete."""

    async def fake_availability(source, combinations, _context):
        assert source == "transcripts"
        return list(combinations), []

    async def fake_retrieve(query_text, latest_message, bank_period_combinations, context, **kwargs):
        _ = query_text, latest_message, context, kwargs
        combo = bank_period_combinations[0]
        return {
            "combo_results": [
                {
                    "combo": combo,
                    "findings": [{"finding": "summary", "page": 1, "location_detail": "Q&A"}],
                    "expanded_chunks": [{"name": "Q&A", "chunk_id": "page_1.1"}],
                }
            ],
            "prepared_query": {"sub_queries": [], "keywords": [], "metrics": []},
        }

    def fake_loader(source):
        assert source == "transcripts"
        return fake_retrieve, lambda _raw: "## Research Findings\n\n- evidence"

    monkeypatch.setattr(
        "aegis_agent.model.agents.research.check_source_availability", fake_availability
    )
    monkeypatch.setattr("aegis_agent.model.agents.research._load_document_retriever", fake_loader)
    result = await run_research_tool(
        _args("RY-CA"),
        {"execution_id": "test"},
        asyncio.Queue(),
    )

    assert result["status"] == "success"
    assert result["findings"]


@pytest.mark.asyncio
async def test_run_research_partial_success(monkeypatch) -> None:
    """Research should continue for available combos while reporting missing combos."""

    async def fake_availability(source, _combinations, _context):
        assert source == "transcripts"
        return [_combo("RY-CA")], [_combo("TD-CA")]

    async def fake_retrieve(query_text, latest_message, bank_period_combinations, context, **kwargs):
        _ = query_text, latest_message, context, kwargs
        combo = bank_period_combinations[0]
        return {
            "combo_results": [
                {
                    "combo": combo,
                    "findings": [{"finding": "summary", "page": 1, "location_detail": "Q&A"}],
                    "expanded_chunks": [{"name": "Q&A", "chunk_id": "page_1.1"}],
                }
            ],
            "prepared_query": {"sub_queries": [], "keywords": [], "metrics": []},
        }

    def fake_loader(source):
        assert source == "transcripts"
        return fake_retrieve, lambda _raw: "## Research Findings\n\n- evidence"

    monkeypatch.setattr(
        "aegis_agent.model.agents.research.check_source_availability", fake_availability
    )
    monkeypatch.setattr("aegis_agent.model.agents.research._load_document_retriever", fake_loader)
    result = await run_research_tool(
        _args("RY-CA", "TD-CA"),
        {"execution_id": "test"},
        asyncio.Queue(),
    )

    assert result["status"] == "partial_success"
    assert result["gaps"][0]["combo_label"] == "Transcripts: TD-CA Q1 2026"


@pytest.mark.asyncio
async def test_run_research_no_available_data(monkeypatch) -> None:
    """Research should return immediately when no requested combos are available."""

    async def fake_availability(source, combinations, _context):
        assert source == "transcripts"
        return [], list(combinations)

    def fake_loader(_source):
        raise AssertionError("transcript retrieval should not run")

    monkeypatch.setattr(
        "aegis_agent.model.agents.research.check_source_availability", fake_availability
    )
    monkeypatch.setattr("aegis_agent.model.agents.research._load_document_retriever", fake_loader)
    result = await run_research_tool(
        _args("TD-CA"),
        {"execution_id": "test"},
        asyncio.Queue(),
    )

    assert result["status"] == "no_available_data"
    assert result["gaps"][0]["combo_label"] == "Transcripts: TD-CA Q1 2026"


@pytest.mark.asyncio
async def test_run_research_dispatches_all_document_sources(monkeypatch) -> None:
    """Four-source research should emit one dropdown and coverage row per source."""

    async def fake_availability(_source, combinations, _context):
        return list(combinations), []

    async def fake_retrieve(query_text, latest_message, bank_period_combinations, context, **kwargs):
        assert "top_k" not in kwargs
        combo = bank_period_combinations[0]
        return {
            "combo_results": [
                {
                    "combo": combo,
                    "findings": [
                        {
                            "finding": f"Finding for {query_text}",
                            "page": 1,
                            "location_detail": "Sheet 1",
                            "source_ref_ids": ["S1"],
                            "metric_name": "CET1",
                            "metric_value": "13.7",
                            "unit": "%",
                            "period": "Q1 2026",
                            "segment": "Enterprise",
                        }
                    ],
                    "expanded_chunks": [{"name": "Sheet 1", "chunk_id": "sheet_1.1"}],
                    "reranked_chunks": [],
                    "metrics": {},
                }
            ],
            "chunks": [{"name": "Sheet 1", "chunk_id": "sheet_1.1"}],
            "findings": [],
            "prepared_query": {"sub_queries": [], "keywords": [], "metrics": []},
        }

    def fake_loader(_source):
        return fake_retrieve, lambda _raw: "## Research Findings\n\n- evidence"

    monkeypatch.setattr(
        "aegis_agent.model.agents.research.check_source_availability",
        fake_availability,
    )
    monkeypatch.setattr("aegis_agent.model.agents.research._load_document_retriever", fake_loader)

    queue = asyncio.Queue()
    result = await run_research_tool(
        {
            "question": "capital",
            "sources": DOCUMENT_SOURCES,
            "combinations": [{"bank_symbol": "RY-CA", "fiscal_year": 2026, "quarter": "Q1"}],
        },
        {"execution_id": "test"},
        queue,
    )

    events = []
    while not queue.empty():
        events.append(queue.get_nowait())

    assert result["status"] == "success"
    assert len(result["findings"]) == 4
    assert result["findings"][0]["finding_type"] == "quantitative"
    assert result["findings"][0]["metric"]["metric_name"] == "CET1"
    assert "[[E" in result["dropdown_markdown"]
    assert "{{S3_LINK" not in result["dropdown_markdown"]
    assert {item["source"] for item in result["coverage"]} == set(DOCUMENT_SOURCES)
    assert [event["name"] for event in events if event["type"] == "subagent"] == DOCUMENT_SOURCES
    assert all(
        "[[E" in event["content"]
        for event in events
        if event["type"] == "subagent"
    )


@pytest.mark.asyncio
async def test_run_research_summarizes_completed_source_while_others_continue(
    monkeypatch,
) -> None:
    """A finished source should update captured summaries before slower sources complete."""

    async def fake_availability(_source, combinations, _context):
        return list(combinations), []

    slow_release = asyncio.Event()

    def fake_loader(source):
        async def fake_retrieve(
            query_text,
            latest_message,
            bank_period_combinations,
            context,
            **kwargs,
        ):
            if source == "rts":
                await slow_release.wait()
            combo = bank_period_combinations[0]
            return {
                "combo_results": [
                    {
                        "combo": combo,
                        "findings": [
                            {
                                "finding": f"{source} interim finding for {query_text}.",
                                "finding_type": "summary",
                                "page": 1,
                                "location_detail": "Capital",
                                "source_ref_ids": ["S1"],
                            }
                        ],
                        "expanded_chunks": [{"name": "Capital", "chunk_id": f"{source}_p1"}],
                        "reranked_chunks": [],
                    }
                ],
                "chunks": [],
                "findings": [],
                "prepared_query": {
                    "sub_queries": ["capital adequacy trend"],
                    "keywords": ["capital"],
                    "metrics": ["CET1"],
                },
            }

        return fake_retrieve, lambda _raw: "## Research Findings\n\n- evidence"

    monkeypatch.setattr(
        "aegis_agent.model.agents.research.check_source_availability",
        fake_availability,
    )
    monkeypatch.setattr("aegis_agent.model.agents.research._load_document_retriever", fake_loader)

    queue = asyncio.Queue()
    task = asyncio.create_task(
        run_research_tool(
            {
                "question": "capital",
                "sources": ["investor_slides", "rts"],
                "combinations": [{"bank_symbol": "RY-CA", "fiscal_year": 2026, "quarter": "Q1"}],
            },
            {"execution_id": "test"},
            queue,
        )
    )

    summary_event = None
    try:
        for _ in range(30):
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            if event["type"] != "research_status_snapshot":
                continue
            summaries = event["content"].get("completed_summaries", [])
            if any(item.get("source_id") == "investor_slides" for item in summaries):
                summary_event = event
                break

        assert summary_event is not None
        summaries = summary_event["content"]["completed_summaries"]
        investor_summary = next(
            item for item in summaries if item["source_id"] == "investor_slides"
        )
        assert investor_summary["quick_summary"].startswith("Investor slides research produced")
        assert investor_summary["summary_text"].startswith("RY-CA Q1 2026:")
        assert "investor_slides interim finding for capital" in investor_summary["summary_text"]
        assert investor_summary["finding_count"] == 1
        rows = {row["source_id"]: row for row in summary_event["content"]["rows"]}
        assert rows["investor_slides"]["status"] == "complete"
        assert rows["rts"]["status"] in {"pending", "checking", "in_progress"}
        assert not task.done()
    finally:
        slow_release.set()

    result = await asyncio.wait_for(task, timeout=1.0)
    assert result["status"] == "success"
