"""Tests for backend-approved Aegis chart specs."""

from __future__ import annotations

import asyncio

from aegis_agent.model.agents.charts import (
    build_chart_options,
    chart_instruction_text,
    publish_chart_artifacts,
)
from aegis_agent.model.agents.schemas import EvidenceReference, Finding, MetricObservation


def _finding(bank: str, value: str, evidence_id: str) -> Finding:
    return Finding(
        combo_label=f"Investor slides: {bank} Q1 2026",
        summary=f"{bank} CET1 ratio was {value}%.",
        finding_type="quantitative",
        metric=MetricObservation(
            metric_name="CET1 ratio",
            metric_value=value,
            unit="%",
            period="Q1 2026",
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


def test_chart_options_publish_json_specs_without_image_assets() -> None:
    """Peer-period numeric findings should produce JSON chart artifacts only."""
    options = build_chart_options(
        [
            _finding("RY-CA", "13.7", "E1"),
            _finding("TD-CA", "13.1", "E2"),
        ]
    )

    assert len(options) == 1
    option = options[0]
    assert option.chart_id == "C1"
    assert option.chart_type == "peer_bar"
    assert option.spec.chart_type == "peer_bar"
    assert [fact.bank_label for fact in option.spec.facts] == ["RY-CA", "TD-CA"]
    assert option.evidence_ids == ["E1", "E2"]
    assert "asset_url" not in option.model_dump(mode="json")
    assert "[[CHART:C1]]" in chart_instruction_text(options)

    queue: asyncio.Queue = asyncio.Queue()
    context = {"background_event_queue": queue}
    publish_chart_artifacts(options, context)

    event = queue.get_nowait()
    assert event["type"] == "chart_artifact"
    assert event["content"]["chart_id"] == "C1"
    assert event["content"]["spec"]["facts"][0]["value"] == 13.7
    assert "asset_url" not in event["content"]
    assert context["chart_artifacts"]["C1"]["spec"]["metric_name"] == "CET1 ratio"
