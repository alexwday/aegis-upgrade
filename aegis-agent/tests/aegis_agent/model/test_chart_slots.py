"""Tests for async chart slot parsing and worker generation."""

from __future__ import annotations

import asyncio
import json

import pytest

from aegis_agent.model.agents.chart_slots import (
    ChartSlot,
    ChartSlotStreamProcessor,
    build_chart_artifact_for_slot,
)
from aegis_agent.model.agents.schemas import (
    EvidenceReference,
    Finding,
    MetricObservation,
    ResearchResult,
)


def _finding(
    bank: str,
    metric: str,
    value: str,
    evidence_id: str,
    *,
    unit: str = "%",
    period: str = "Q2 2026",
) -> Finding:
    return Finding(
        combo_label=f"Investor slides: {bank} Q2 2026",
        summary=f"{bank} {metric} was {value}{unit}.",
        finding_type="quantitative",
        metric=MetricObservation(
            metric_name=metric,
            metric_value=value,
            unit=unit,
            period=period,
        ),
        evidence_refs=[
            EvidenceReference(
                evidence_id=evidence_id,
                source_id="investor_slides",
                source_label="Investor slides",
                display_label=f"Investor slides {evidence_id}",
            )
        ],
    )


def _research_result(findings: list[Finding]) -> ResearchResult:
    registry = {
        "investor_slides": {
            ref.evidence_id: ref
            for finding in findings
            for ref in finding.evidence_refs
            if ref.evidence_id
        }
    }
    return ResearchResult(
        status="success",
        quick_summary="Research complete.",
        findings=findings,
        evidence_registry=registry,
    )


def _slot(**overrides) -> ChartSlot:
    payload = {
        "slot_id": "C1",
        "title": "TD vs RBC CET1",
        "chart_type": "peer_rank_bar",
        "intent": "Compare CET1 ratio for TD and RBC",
        "banks": ["TD-CA", "RY-CA"],
        "periods": ["Q2 2026"],
        "metrics": ["CET1 ratio"],
    }
    payload.update(overrides)
    return ChartSlot.model_validate(payload)


@pytest.mark.asyncio
async def test_slot_marker_emits_pending_card_and_ready_artifact() -> None:
    """A full slot marker should stream a loading card, then hydrate it."""
    findings = [
        _finding("RY-CA", "CET1 ratio", "13.7", "E1"),
        _finding("TD-CA", "CET1 ratio", "13.1", "E2"),
    ]
    queue: asyncio.Queue = asyncio.Queue()
    context = {
        "execution_id": "test",
        "background_event_queue": queue,
        "latest_research_result": _research_result(findings),
    }
    processor = ChartSlotStreamProcessor(context)
    marker = '[[CHART_SLOT:{"slot_id":"C1","title":"TD vs RBC CET1","chart_type":"peer_rank_bar","intent":"Compare CET1 ratio for TD and RBC","banks":["TD-CA","RY-CA"],"periods":["Q2 2026"],"metrics":["CET1 ratio"]}]]'

    events = processor.push(marker)
    await asyncio.gather(*context["chart_worker_tasks"])
    ready_event = queue.get_nowait()

    assert events[0]["type"] == "chart_artifact"
    assert events[0]["content"]["status"] == "pending"
    assert events[1]["content"] == "[[CHART:C1]]"
    assert ready_event["content"]["status"] == "ready"
    assert ready_event["content"]["chart_id"] == "C1"
    assert ready_event["content"]["spec"]["chart_type"] == "peer_rank_bar"


@pytest.mark.asyncio
async def test_split_slot_marker_is_buffered_until_complete() -> None:
    """A slot split across stream chunks should not leak raw marker text."""
    processor = ChartSlotStreamProcessor({"execution_id": "test", "background_event_queue": asyncio.Queue()})

    first = processor.push('Intro\n[[CHART_SLOT:{"slot_id"')
    second = processor.push(':"C1","title":"T","chart_type":"peer_rank_bar","intent":"I"}]]\nDone')

    assert first == [{"type": "agent", "name": "aegis", "content": "Intro\n"}]
    assert second[0]["type"] == "chart_artifact"
    assert second[1]["content"] == "[[CHART:C1]]"
    assert second[2]["content"] == "\nDone"
    await asyncio.gather(*processor.context["chart_worker_tasks"])


def test_malformed_slot_json_is_stripped() -> None:
    """Malformed chart slots should not appear in the final answer."""
    processor = ChartSlotStreamProcessor({"execution_id": "test"})

    events = processor.push("Before\n[[CHART_SLOT:not-json]]\nAfter")

    assert events == [
        {"type": "agent", "name": "aegis", "content": "Before\n"},
        {"type": "agent", "name": "aegis", "content": "\nAfter"},
    ]


@pytest.mark.asyncio
async def test_duplicate_slot_ids_are_normalized_deterministically() -> None:
    """Duplicate slot IDs should not collide in the UI artifact registry."""
    processor = ChartSlotStreamProcessor({"execution_id": "test", "background_event_queue": asyncio.Queue()})
    marker = '{"slot_id":"C1","title":"T","chart_type":"peer_rank_bar","intent":"I"}'

    events = processor.push(f"[[CHART_SLOT:{marker}]]\n[[CHART_SLOT:{marker}]]")

    assert [event["content"] for event in events if event["type"] == "agent"] == [
        "[[CHART:C1]]",
        "\n",
        "[[CHART:C2]]",
    ]
    await asyncio.gather(*processor.context["chart_worker_tasks"])


@pytest.mark.asyncio
async def test_broad_mixed_metric_slot_builds_small_multiple_panel() -> None:
    """Mixed units should produce a small multiple panel, not one shared axis."""
    findings = [
        _finding("RY-CA", "Total assets", "2100", "E1", unit="$"),
        _finding("TD-CA", "Total assets", "1900", "E2", unit="$"),
        _finding("RY-CA", "CET1 ratio", "13.7", "E3", unit="%"),
        _finding("TD-CA", "CET1 ratio", "13.1", "E4", unit="%"),
    ]
    artifact = await build_chart_artifact_for_slot(
        _slot(
            title="TD vs RBC key balance sheet metrics",
            chart_type="small_multiple_panel",
            intent="Compare key balance sheet metrics across TD and RBC",
            metrics=["Total assets", "CET1 ratio"],
        ),
        {"latest_research_result": _research_result(findings)},
    )

    assert artifact is not None
    assert artifact["spec"]["chart_type"] == "small_multiple_panel"
    assert len(artifact["spec"]["series"]) == 2


@pytest.mark.asyncio
async def test_llm_slot_output_with_invalid_evidence_is_rejected(monkeypatch) -> None:
    """LLM fallback specs must cite evidence IDs from the current research turn."""
    findings = [
        _finding("RY-CA", "CET1 ratio", "13.7", "E1"),
        _finding("TD-CA", "CET1 ratio", "13.1", "E2"),
    ]

    async def fake_complete_with_tools(*_args, **_kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "submit_chart_slot",
                                    "arguments": json.dumps(
                                        {
                                            "chart": {
                                                "chart_type": "peer_rank_bar",
                                                "title": "ROE",
                                                "subtitle": "Q2 2026 | %",
                                                "metric_name": "ROE",
                                                "unit": "%",
                                                "x_label": "%",
                                                "y_label": "Bank",
                                                "rationale": "Compare ROE",
                                                "points": [
                                                    {"label": "RY-CA", "value": 10.0, "evidence_ids": ["E999"]},
                                                    {"label": "TD-CA", "value": 11.0, "evidence_ids": ["E2"]},
                                                ],
                                            }
                                        }
                                    ),
                                }
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("aegis_agent.model.agents.chart_slots.complete_with_tools", fake_complete_with_tools)
    artifact = await build_chart_artifact_for_slot(
        _slot(intent="Compare ROE for TD and RBC", metrics=["ROE"]),
        {"latest_research_result": _research_result(findings), "auth_config": {"token": "token"}},
    )

    assert artifact is None


@pytest.mark.asyncio
async def test_llm_slot_output_with_invented_value_is_rejected(monkeypatch) -> None:
    """LLM fallback specs must use numeric values traceable to cited findings."""
    findings = [
        _finding("RY-CA", "CET1 ratio", "13.7", "E1"),
        _finding("TD-CA", "CET1 ratio", "13.1", "E2"),
    ]

    async def fake_complete_with_tools(*_args, **_kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "submit_chart_slot",
                                    "arguments": json.dumps(
                                        {
                                            "chart": {
                                                "chart_type": "peer_rank_bar",
                                                "title": "ROE",
                                                "subtitle": "Q2 2026 | %",
                                                "metric_name": "ROE",
                                                "unit": "%",
                                                "x_label": "%",
                                                "y_label": "Bank",
                                                "rationale": "Compare ROE",
                                                "points": [
                                                    {"label": "RY-CA", "value": 99.9, "evidence_ids": ["E1"]},
                                                    {"label": "TD-CA", "value": 13.1, "evidence_ids": ["E2"]},
                                                ],
                                            }
                                        }
                                    ),
                                }
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr("aegis_agent.model.agents.chart_slots.complete_with_tools", fake_complete_with_tools)
    artifact = await build_chart_artifact_for_slot(
        _slot(intent="Compare ROE for TD and RBC", metrics=["ROE"]),
        {"latest_research_result": _research_result(findings), "auth_config": {"token": "token"}},
    )

    assert artifact is None
