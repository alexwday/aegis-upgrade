"""Tests for backend-approved Aegis chart specs."""

from __future__ import annotations

import asyncio

from aegis_agent.model.agents.charts import (
    build_chart_options,
    chart_instruction_text,
    chart_instruction_text_from_artifacts,
    publish_chart_artifacts,
)
from aegis_agent.model.agents.schemas import (
    EvidenceReference,
    Finding,
    MetricObservation,
    ResearchTable,
)


def _finding(
    bank: str,
    value: str,
    evidence_id: str,
    quarter: str = "Q1",
    year: int = 2026,
    metric_name: str = "CET1 ratio",
    unit: str = "%",
) -> Finding:
    return Finding(
        combo_label=f"Investor slides: {bank} {quarter} {year}",
        summary=f"{bank} {metric_name} was {value}{unit}.",
        finding_type="quantitative",
        metric=MetricObservation(
            metric_name=metric_name,
            metric_value=value,
            unit=unit,
            period=f"{quarter} {year}",
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


def _table_finding(table: ResearchTable, evidence_id: str = "E1") -> Finding:
    return Finding(
        combo_label="Supplementary financials: RY-CA Q1 2026",
        summary=table.title or "Table finding",
        finding_type="table",
        table=table,
        evidence_refs=[
            EvidenceReference(
                evidence_id=evidence_id,
                source_id="supplementary_financials",
                source_label="Supplementary financials",
                display_label=f"Supplementary financials {evidence_id}",
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
    assert option.chart_type == "peer_rank_bar"
    assert option.spec.chart_type == "peer_rank_bar"
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


def test_single_bank_trend_produces_line_and_bar_options() -> None:
    """Trend data should support both line and bar templates for follow-up requests."""
    options = build_chart_options(
        [
            _finding("RY-CA", "13.4", "E1", quarter="Q1"),
            _finding("RY-CA", "13.6", "E2", quarter="Q2"),
            _finding("RY-CA", "13.7", "E3", quarter="Q3"),
        ]
    )

    assert [option.chart_type for option in options] == ["trend_line", "trend_bar"]
    assert [option.chart_id for option in options] == ["C1", "C2"]
    assert options[1].spec.x_label == "Period"
    assert "[[CHART:C2]]" in chart_instruction_text(options)


def test_two_period_peer_data_produces_slopegraph_and_delta() -> None:
    """Two-period peer comparisons should emphasize rank movement and change."""
    options = build_chart_options(
        [
            _finding("RY-CA", "13.4", "E1", quarter="Q1"),
            _finding("RY-CA", "13.7", "E2", quarter="Q2"),
            _finding("TD-CA", "12.8", "E3", quarter="Q1"),
            _finding("TD-CA", "13.1", "E4", quarter="Q2"),
        ]
    )

    assert [option.chart_type for option in options[:2]] == ["slopegraph", "delta_bar"]


def test_multi_period_peer_data_produces_multi_series_line() -> None:
    """Multi-bank, multi-period metric facts should produce a multi-series trend."""
    options = build_chart_options(
        [
            _finding("RY-CA", "13.4", "E1", quarter="Q1"),
            _finding("RY-CA", "13.6", "E2", quarter="Q2"),
            _finding("RY-CA", "13.7", "E3", quarter="Q3"),
            _finding("TD-CA", "12.8", "E4", quarter="Q1"),
            _finding("TD-CA", "13.0", "E5", quarter="Q2"),
            _finding("TD-CA", "13.1", "E6", quarter="Q3"),
        ]
    )

    assert options[0].chart_type == "multi_series_line"
    assert options[0].spec.series[0]["name"] in {"RY-CA", "TD-CA"}


def test_peer_metric_pairs_can_produce_scatter_plot() -> None:
    """Two same-period peer metrics should produce a scatter plot when enough peers exist."""
    findings = []
    for index, bank in enumerate(["RY-CA", "TD-CA", "BMO-CA", "BNS-CA"], start=1):
        findings.append(
            _finding(bank, str(12 + index), f"E{index}", metric_name="CET1 ratio", unit="%")
        )
        findings.append(
            _finding(bank, str(8 + index), f"E{index + 10}", metric_name="ROE", unit="%")
        )

    options = build_chart_options(findings)

    assert "scatter_plot" in {option.chart_type for option in options}


def test_period_table_produces_table_derived_trend() -> None:
    """A period-shaped table should produce a table-derived trend chart."""
    options = build_chart_options(
        [
            _table_finding(
                ResearchTable(
                    title="Net interest margin",
                    columns=["Metric", "Q1 2026", "Q2 2026", "Q3 2026"],
                    rows=[
                        {
                            "Metric": "Net interest margin",
                            "Q1 2026": "1.62%",
                            "Q2 2026": "1.66%",
                            "Q3 2026": "1.70%",
                        }
                    ],
                )
            )
        ]
    )

    assert options[0].chart_type == "trend_line"
    assert options[0].spec.source_kind == "table"
    assert options[0].spec.series[0]["name"] == "Net interest margin"


def test_segment_table_produces_composition_chart() -> None:
    """Segment/category tables should produce composition charts."""
    options = build_chart_options(
        [
            _table_finding(
                ResearchTable(
                    title="Revenue by business segment",
                    columns=["Segment", "Q1 2026"],
                    rows=[
                        {"Segment": "Personal Banking", "Q1 2026": "45%"},
                        {"Segment": "Commercial Banking", "Q1 2026": "35%"},
                        {"Segment": "Wealth", "Q1 2026": "20%"},
                    ],
                )
            )
        ]
    )

    assert options[0].chart_type == "composition_100_bar"
    assert len(options[0].spec.points) == 3


def test_bridge_table_produces_waterfall() -> None:
    """Bridge or movement tables should produce waterfall charts."""
    options = build_chart_options(
        [
            _table_finding(
                ResearchTable(
                    title="CET1 capital bridge",
                    columns=["Driver", "Impact"],
                    rows=[
                        {"Driver": "Opening CET1", "Impact": "13.4%"},
                        {"Driver": "Internal capital generation", "Impact": "0.3%"},
                        {"Driver": "RWA growth", "Impact": "-0.2%"},
                        {"Driver": "Ending CET1", "Impact": "13.5%"},
                    ],
                )
            )
        ]
    )

    assert options[0].chart_type == "waterfall"
    assert options[0].spec.points[0]["is_total"] is True


def test_mixed_unit_period_table_produces_small_multiples() -> None:
    """Mixed-unit metric tables should use small multiples rather than dual axes."""
    options = build_chart_options(
        [
            _table_finding(
                ResearchTable(
                    title="Credit and profitability metrics",
                    columns=["Metric", "Q1 2026", "Q2 2026", "Q3 2026"],
                    rows=[
                        {"Metric": "NIM", "Q1 2026": "1.62%", "Q2 2026": "1.66%", "Q3 2026": "1.70%"},
                        {"Metric": "PCL ratio", "Q1 2026": "42 bps", "Q2 2026": "45 bps", "Q3 2026": "44 bps"},
                    ],
                )
            )
        ]
    )

    assert options[0].chart_type == "small_multiple_panel"
    assert len({series["unit"] for series in options[0].spec.series}) == 2


def test_no_chart_for_one_point_or_missing_evidence() -> None:
    """Charts should not be approved when data or evidence is insufficient."""
    assert build_chart_options([_finding("RY-CA", "13.7", "E1")]) == []

    finding = _finding("RY-CA", "13.7", "E1")
    finding.evidence_refs = []
    assert build_chart_options([finding, _finding("TD-CA", "13.1", "E2")]) == []


def test_no_chart_for_ambiguous_table_headers() -> None:
    """Ambiguous tables should be ignored instead of guessed into charts."""
    options = build_chart_options(
        [
            _table_finding(
                ResearchTable(
                    title="Ambiguous values",
                    columns=["Label", "Value"],
                    rows=[{"Label": "Alpha", "Value": "10"}, {"Label": "Beta", "Value": "12"}],
                )
            )
        ]
    )

    assert options == []


def test_prior_chart_instruction_reuses_approved_artifacts_only() -> None:
    """Follow-up chart requests should reuse approved JSON artifacts, not markdown charts."""
    instruction = chart_instruction_text_from_artifacts(
        {
            "C2": {
                "chart_id": "C2",
                "chart_type": "trend_bar",
                "title": "RY-CA CET1 ratio by period",
                "subtitle": "Q1 2026 to Q3 2026 | %",
                "status": "ready",
                "evidence_ids": ["E1", "E2", "E3"],
                "spec": {
                    "chart_type": "trend_bar",
                    "metric_name": "CET1 ratio",
                    "unit": "%",
                },
            }
        }
    )

    assert "Previously approved interactive chart options" in instruction
    assert '"chart_id": "C2"' in instruction
    assert "markdown/ascii chart substitutes" in instruction
