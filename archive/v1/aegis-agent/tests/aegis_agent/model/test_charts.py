"""Tests for planner-authored Aegis chart specs."""

from __future__ import annotations

import asyncio
import json

import pytest

from aegis_agent.model.agents import charts
from aegis_agent.model.agents.charts import (
    build_chart_options,
    chart_instruction_text,
    chart_instruction_text_from_artifacts,
    plan_chart_options,
    publish_chart_artifacts,
    validate_planner_chart_plan,
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
    summary: str | None = None,
) -> Finding:
    return Finding(
        combo_label=f"Investor slides: {bank} {quarter} {year}",
        summary=summary or f"{bank} {metric_name} was {value}{unit}.",
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


def _peer_rank_plan(metric_name: str = "CET1 ratio") -> dict:
    return {
        "charts": [
            {
                "chart_type": "peer_rank_bar",
                "title": f"{metric_name} peer ranking",
                "subtitle": "Q1 2026 | %",
                "metric_name": metric_name,
                "unit": "%",
                "x_label": "%",
                "y_label": "Bank",
                "rationale": f"{metric_name} is the metric the user asked to compare.",
                "points": [
                    {"label": "RY-CA", "value": 13.7, "evidence_ids": ["E1"]},
                    {"label": "TD-CA", "value": 13.1, "evidence_ids": ["E2"]},
                ],
            }
        ]
    }


def test_valid_planner_output_publishes_json_specs_without_image_assets() -> None:
    """Validated planner charts should publish existing JSON chart artifacts only."""
    options = validate_planner_chart_plan(
        _peer_rank_plan(),
        [_finding("RY-CA", "13.7", "E1"), _finding("TD-CA", "13.1", "E2")],
        "Compare CET1 ratio for RBC and TD.",
    )

    assert len(options) == 1
    option = options[0]
    assert option.chart_id == "C1"
    assert option.chart_type == "peer_rank_bar"
    assert option.spec.source_kind == "chart_planner"
    assert option.spec.points[0]["value"] == 13.7
    assert option.evidence_ids == ["E1", "E2"]
    assert "asset_url" not in option.model_dump(mode="json")
    assert "[[CHART:C1]]" in chart_instruction_text(options)
    assert "chart data is already fixed" in chart_instruction_text(options)

    queue: asyncio.Queue = asyncio.Queue()
    context = {"background_event_queue": queue}
    publish_chart_artifacts(options, context)

    event = queue.get_nowait()
    assert event["type"] == "chart_artifact"
    assert event["content"]["chart_id"] == "C1"
    assert event["content"]["spec"]["points"][0]["value"] == 13.7
    assert event["content"]["spec"]["facts"] == []
    assert event["content"]["rationale"]
    assert "asset_url" not in event["content"]
    assert context["chart_artifacts"]["C1"]["spec"]["metric_name"] == "CET1 ratio"


def test_invalid_evidence_ids_are_rejected() -> None:
    """The planner cannot cite evidence IDs that do not exist in this turn."""
    plan = _peer_rank_plan()
    plan["charts"][0]["points"][1]["evidence_ids"] = ["E999"]

    assert (
        validate_planner_chart_plan(
            plan,
            [_finding("RY-CA", "13.7", "E1"), _finding("TD-CA", "13.1", "E2")],
            "Compare CET1 ratio for RBC and TD.",
        )
        == []
    )


def test_invented_numeric_values_are_rejected() -> None:
    """The planner cannot publish values that are not traceable to cited evidence."""
    plan = _peer_rank_plan()
    plan["charts"][0]["points"][1]["value"] = 99.9

    assert (
        validate_planner_chart_plan(
            plan,
            [_finding("RY-CA", "13.7", "E1"), _finding("TD-CA", "13.1", "E2")],
            "Compare CET1 ratio for RBC and TD.",
        )
        == []
    )


def test_underpopulated_charts_are_dropped() -> None:
    """A chart should disappear when too few valid points remain after validation."""
    plan = _peer_rank_plan()
    plan["charts"][0]["points"] = [{"label": "RY-CA", "value": 13.7, "evidence_ids": ["E1"]}]

    assert (
        validate_planner_chart_plan(
            plan,
            [_finding("RY-CA", "13.7", "E1")],
            "Compare CET1 ratio for RBC.",
        )
        == []
    )


def test_relevance_validation_drops_unrelated_capital_chart_for_credit_question() -> None:
    """Planner charts must match the user's financial intent, not any available metric."""
    options = validate_planner_chart_plan(
        _peer_rank_plan(),
        [_finding("RY-CA", "13.7", "E1"), _finding("TD-CA", "13.1", "E2")],
        "Compare credit quality for RBC and TD.",
    )

    assert options == []


def test_generic_bank_comparison_does_not_reject_metric_chart() -> None:
    """Bank names alone should not make the relevance gate reject all charts."""
    options = validate_planner_chart_plan(
        _peer_rank_plan(),
        [_finding("RY-CA", "13.7", "E1"), _finding("TD-CA", "13.1", "E2")],
        "Compare RBC and TD.",
    )

    assert len(options) == 1


def test_cet1_question_drops_unrelated_metric_but_keeps_cet1_chart() -> None:
    """A relevant CET1 chart should survive while an unrelated metric chart is dropped."""
    plan = _peer_rank_plan()
    plan["charts"].append(
        {
            "chart_type": "peer_rank_bar",
            "title": "ROE peer ranking",
            "subtitle": "Q1 2026 | %",
            "metric_name": "ROE",
            "unit": "%",
            "x_label": "%",
            "y_label": "Bank",
            "rationale": "ROE is available but not requested.",
            "points": [
                {"label": "RY-CA", "value": 17.0, "evidence_ids": ["E3"]},
                {"label": "TD-CA", "value": 15.0, "evidence_ids": ["E4"]},
            ],
        }
    )

    options = validate_planner_chart_plan(
        plan,
        [
            _finding("RY-CA", "13.7", "E1"),
            _finding("TD-CA", "13.1", "E2"),
            _finding("RY-CA", "17.0", "E3", metric_name="ROE"),
            _finding("TD-CA", "15.0", "E4", metric_name="ROE"),
        ],
        "Compare CET1 ratio for RBC and TD.",
    )

    assert [option.metric_name for option in options] == ["CET1 ratio"]


def test_generic_ratio_word_does_not_allow_unrelated_ratio_chart() -> None:
    """Generic words like ratio should not make an unrelated metric relevant."""
    plan = _peer_rank_plan("Efficiency ratio")
    plan["charts"][0]["points"] = [
        {"label": "RY-CA", "value": 54.0, "evidence_ids": ["E1"]},
        {"label": "TD-CA", "value": 52.0, "evidence_ids": ["E2"]},
    ]

    assert (
        validate_planner_chart_plan(
            plan,
            [
                _finding("RY-CA", "54.0", "E1", metric_name="Efficiency ratio"),
                _finding("TD-CA", "52.0", "E2", metric_name="Efficiency ratio"),
            ],
            "Compare CET1 ratio for RBC and TD.",
        )
        == []
    )


def test_finance_acronym_aliases_allow_relevant_chart() -> None:
    """Common finance acronyms should match their expanded metric labels."""
    plan = _peer_rank_plan("Net interest margin")
    plan["charts"][0]["points"] = [
        {"label": "RY-CA", "value": 1.62, "evidence_ids": ["E1"]},
        {"label": "TD-CA", "value": 1.58, "evidence_ids": ["E2"]},
    ]

    options = validate_planner_chart_plan(
        plan,
        [
            _finding("RY-CA", "1.62", "E1", metric_name="Net interest margin"),
            _finding("TD-CA", "1.58", "E2", metric_name="Net interest margin"),
        ],
        "Compare NIM for RBC and TD.",
    )

    assert len(options) == 1


def test_table_values_can_validate_planner_composition_chart() -> None:
    """Planner charts may use source-grounded table cells when evidence is cited."""
    plan = {
        "charts": [
            {
                "chart_type": "composition_100_bar",
                "title": "Revenue mix",
                "subtitle": "Q1 2026 | %",
                "metric_name": "Revenue mix",
                "unit": "%",
                "x_label": "Segment",
                "y_label": "Share",
                "rationale": "Revenue mix directly shows the segment composition requested.",
                "points": [
                    {
                        "group": "Q1 2026",
                        "category": "Personal Banking",
                        "value": 45,
                        "evidence_ids": ["E1"],
                    },
                    {
                        "group": "Q1 2026",
                        "category": "Commercial Banking",
                        "value": 35,
                        "evidence_ids": ["E1"],
                    },
                    {"group": "Q1 2026", "category": "Wealth", "value": 20, "evidence_ids": ["E1"]},
                ],
            }
        ]
    }

    options = validate_planner_chart_plan(
        plan,
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
        ],
        "Show the revenue mix by segment.",
    )

    assert options[0].chart_type == "composition_100_bar"
    assert len(options[0].spec.points) == 3


def test_deterministic_chart_generation_uses_source_grounded_findings() -> None:
    """Deterministic chart candidates should use only retrieved structured findings."""
    options = build_chart_options(
        [_finding("RY-CA", "13.7", "E1"), _finding("TD-CA", "13.1", "E2")],
        "Compare CET1 ratio for RBC and TD.",
    )

    assert len(options) >= 1
    assert options[0].chart_type == "peer_rank_bar"
    assert options[0].evidence_ids == ["E1", "E2"]
    assert options[0].spec.facts[0].metric_name == "CET1 ratio"


@pytest.mark.asyncio
async def test_planner_failure_returns_no_charts_without_slot_worker(monkeypatch) -> None:
    """Planner errors should not trigger the old pre-answer fallback path."""

    def fake_prompt(*args, **kwargs):
        _ = args, kwargs
        raise LookupError("missing prompt")

    monkeypatch.setattr(charts, "load_prompt_from_db", fake_prompt)

    options = await plan_chart_options(
        [_finding("RY-CA", "13.7", "E1"), _finding("TD-CA", "13.1", "E2")],
        "Compare CET1 ratio for RBC and TD.",
        {"execution_id": "test", "auth_config": {"token": "token"}},
    )

    assert options == []


@pytest.mark.asyncio
async def test_empty_planner_plan_returns_no_preplanned_charts(monkeypatch) -> None:
    """A conservative empty planner response should not prepublish charts."""

    def fake_prompt(*args, **kwargs):
        _ = args, kwargs
        return {
            "system_prompt": "Plan charts.",
            "user_prompt": "{{question}}\n{{chart_templates}}\n{{research_payload}}",
            "tool_definition": charts.DEFAULT_CHART_PLANNER_TOOL,
        }

    async def fake_complete_with_tools(messages, tools, context, llm_params):
        _ = messages, tools, context, llm_params
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "submit_chart_plan",
                                    "arguments": json.dumps({"charts": []}),
                                }
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr(charts, "load_prompt_from_db", fake_prompt)
    monkeypatch.setattr(charts, "complete_with_tools", fake_complete_with_tools)

    options = await plan_chart_options(
        [_finding("RY-CA", "13.7", "E1"), _finding("TD-CA", "13.1", "E2")],
        "Compare CET1 ratio for RBC and TD.",
        {"execution_id": "test", "auth_config": {"token": "token"}},
    )

    assert options == []


@pytest.mark.asyncio
async def test_chart_planner_uses_required_tool_call(monkeypatch) -> None:
    """The planner should call submit_chart_plan and validate the returned specs."""

    def fake_prompt(*args, **kwargs):
        _ = args, kwargs
        return {
            "system_prompt": "Plan charts.",
            "user_prompt": "{{question}}\n{{chart_templates}}\n{{research_payload}}",
            "tool_definition": charts.DEFAULT_CHART_PLANNER_TOOL,
        }

    async def fake_complete_with_tools(messages, tools, context, llm_params):
        assert "Compare CET1" in messages[1]["content"]
        assert tools[0]["function"]["name"] == "submit_chart_plan"
        assert llm_params["tool_choice"]["function"]["name"] == "submit_chart_plan"
        _ = context
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "submit_chart_plan",
                                    "arguments": json.dumps(_peer_rank_plan()),
                                }
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr(charts, "load_prompt_from_db", fake_prompt)
    monkeypatch.setattr(charts, "complete_with_tools", fake_complete_with_tools)

    options = await plan_chart_options(
        [_finding("RY-CA", "13.7", "E1"), _finding("TD-CA", "13.1", "E2")],
        "Compare CET1 ratio for RBC and TD.",
        {"execution_id": "test", "auth_config": {"token": "token"}},
    )

    assert len(options) == 1
    assert options[0].chart_type == "peer_rank_bar"


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
