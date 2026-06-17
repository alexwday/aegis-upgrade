"""Tests for the single agent loop."""

from __future__ import annotations

import pytest

from aegis_agent.model.agents.aegis_agent import (
    _source_scope_message,
    run_aegis_agent,
)
from aegis_agent.model.agents.research import _merge_latest_research_result
from aegis_agent.model.agents.schemas import (
    DEFAULT_DOCUMENT_SOURCES,
    EvidenceReference,
    Finding,
    MetricObservation,
    ResearchResult,
)


@pytest.fixture(autouse=True)
def stub_system_prompt(monkeypatch) -> None:
    """Keep loop tests independent from the live prompt database."""
    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent._load_system_prompt",
        lambda: "You are Aegis.",
    )


def test_source_scope_message_lists_all_default_sources() -> None:
    """The model should see all six sources as the unfiltered default scope."""
    message = _source_scope_message({"db_names": list(DEFAULT_DOCUMENT_SOURCES)})

    assert "all 6 sources" in message
    assert "transcripts, event_transcripts, investor_slides" in message
    assert "supplementary_financials, rts, pillar3" in message
    assert "all four" not in message


def test_source_scope_message_honors_user_filter() -> None:
    """The model should know when the UI selected a narrower source scope."""
    message = _source_scope_message(
        {"source_filter": ["event_transcripts", "transcripts"]}
    )

    assert "User-selected source filter" in message
    assert "event_transcripts, transcripts" in message
    assert "Only call run_research with those source IDs" in message
    assert "all 6 sources" not in message


@pytest.mark.asyncio
async def test_agent_emits_choice_card(monkeypatch) -> None:
    """The agent should surface small clarification choices as ui_card events."""

    async def fake_stream_with_tools(*_args, **_kwargs):
        yield {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "tool-1",
                                "type": "function",
                                "function": {
                                    "name": "present_choice_card",
                                    "arguments": (
                                        '{"question":"Which quarter?","options":['
                                        '{"id":"q1","label":"Q1 2026"},'
                                        '{"id":"q4","label":"Q4 2025"}]}'
                                    ),
                                },
                            }
                        ]
                    }
                }
            ]
        }

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )
    events = [
        event
        async for event in run_aegis_agent(
            [{"role": "user", "content": "Compare RBC and TD for last quarter"}],
            {"execution_id": "test"},
        )
    ]

    assert events[0]["type"] == "ui_card"
    assert events[0]["content"]["question"] == "Which quarter?"


@pytest.mark.asyncio
async def test_agent_rejects_incomplete_research_tool_call(monkeypatch) -> None:
    """Incomplete research tool calls should loop back to the model, not retrieval."""
    calls = 0

    async def fake_stream_with_tools(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {"choices": [{"delta": {"content": "I'll use all four sources for RBC. "}}]}
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "tool-1",
                                    "type": "function",
                                    "function": {
                                        "name": "run_research",
                                        "arguments": (
                                            '{"question":"credit quality",'
                                            '"combinations":[{"bank_symbol":"RY-CA"}]}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return
        yield {
            "choices": [
                {
                    "delta": {
                        "content": "Which fiscal year and quarter should I use?",
                    }
                }
            ]
        }

    async def fake_run_research_tool(*_args, **_kwargs):
        raise AssertionError("research retrieval should not run")

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )
    monkeypatch.setattr(
        "aegis_agent.model.agents.tools.run_research_tool",
        fake_run_research_tool,
    )
    events = [
        event
        async for event in run_aegis_agent(
            [{"role": "user", "content": "What did RBC say about credit quality?"}],
            {"execution_id": "test"},
        )
    ]

    assert events == [
        {
            "type": "agent",
            "name": "aegis",
            "content": "Which fiscal year and quarter should I use?",
        }
    ]


@pytest.mark.asyncio
async def test_agent_research_shell_then_streams_body(monkeypatch) -> None:
    """Research answers should declare the final shell before body chunks stream."""
    calls = 0

    async def fake_stream_with_tools(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "research-1",
                                    "type": "function",
                                    "function": {
                                        "name": "run_research",
                                        "arguments": (
                                            '{"question":"CET1 capital",'
                                            '"sources":["investor_slides"],'
                                            '"combinations":[{"bank_symbol":"RY-CA",'
                                            '"fiscal_year":2026,"quarter":"Q1"}]}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return
        if calls == 2:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "final-1",
                                    "type": "function",
                                    "function": {
                                        "name": "start_final_response",
                                        "arguments": (
                                            '{"render_mode":"default_brief",'
                                            '"body_style":"default_brief",'
                                            '"summary":{"headline":"RBC CET1 was solid"},'
                                            '"tiles":[{"label":"CET1",'
                                            '"value":"13.2%","evidence_ids":["E1"]}]}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return
        yield {"choices": [{"delta": {"content": "RBC reported CET1 strength "}}]}
        yield {"choices": [{"delta": {"content": "supported by capital disclosures. [[E1]]"}}]}

    async def fake_run_research_tool(*_args, **_kwargs):
        return {"status": "success", "quick_summary": "Research complete", "findings": []}

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )
    monkeypatch.setattr(
        "aegis_agent.model.agents.tools.run_research_tool",
        fake_run_research_tool,
    )

    events = [
        event
        async for event in run_aegis_agent(
            [{"role": "user", "content": "Using investor slides, summarize RBC Q1 2026 CET1."}],
            {"execution_id": "test"},
        )
    ]

    assert [event["type"] for event in events] == [
        "final_response_start",
        "agent",
        "agent",
    ]
    assert all("I'll use all four sources" not in str(event) for event in events)
    assert events[0]["content"]["summary"]["headline"] == "RBC CET1 was solid"
    assert "RBC reported CET1" in events[1]["content"]
    assert "[[E1]]" in events[2]["content"]


@pytest.mark.asyncio
async def test_agent_streams_inline_shell_without_final_tool_call(monkeypatch) -> None:
    """After research, the final shell can stream inline with the body content."""
    calls = 0

    async def fake_stream_with_tools(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "research-1",
                                    "type": "function",
                                    "function": {
                                        "name": "run_research",
                                        "arguments": (
                                            '{"question":"CET1 capital",'
                                            '"sources":["investor_slides"],'
                                            '"combinations":[{"bank_symbol":"RY-CA",'
                                            '"fiscal_year":2026,"quarter":"Q1"}]}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return
        shell = (
            '<aegis_final_shell>{"render_mode":"default_brief",'
            '"body_style":"default_brief",'
            '"summary":{"headline":"RBC CET1 was solid"},'
            '"tiles":[{"label":"CET1","value":"13.2%","evidence_ids":["E1"]}]}'
            "</aegis_final_shell>"
        )
        yield {"choices": [{"delta": {"content": shell[:28]}}]}
        yield {"choices": [{"delta": {"content": shell[28:] + "RBC reported CET1 strength "}}]}
        yield {"choices": [{"delta": {"content": "supported by disclosures. [[E1]]"}}]}

    async def fake_run_research_tool(*_args, **_kwargs):
        return {"status": "success", "quick_summary": "Research complete", "findings": []}

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )
    monkeypatch.setattr(
        "aegis_agent.model.agents.tools.run_research_tool",
        fake_run_research_tool,
    )

    events = [
        event
        async for event in run_aegis_agent(
            [{"role": "user", "content": "Using investor slides, summarize RBC Q1 2026 CET1."}],
            {"execution_id": "test"},
        )
    ]

    assert [event["type"] for event in events] == [
        "final_response_start",
        "agent",
        "agent",
    ]
    assert events[0]["content"]["summary"]["headline"] == "RBC CET1 was solid"
    assert events[1]["content"] == "RBC reported CET1 strength "
    assert events[2]["content"] == "supported by disclosures. [[E1]]"


@pytest.mark.asyncio
async def test_agent_chart_slot_streams_loading_then_ready_artifact(monkeypatch) -> None:
    """Final-answer chart slots should become loading cards before worker hydration."""
    calls = 0

    async def fake_stream_with_tools(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "research-1",
                                    "type": "function",
                                    "function": {
                                        "name": "run_research",
                                        "arguments": (
                                            '{"question":"Compare CET1",'
                                            '"sources":["investor_slides"],'
                                            '"combinations":['
                                            '{"bank_symbol":"RY-CA","fiscal_year":2026,"quarter":"Q2"},'
                                            '{"bank_symbol":"TD-CA","fiscal_year":2026,"quarter":"Q2"}]}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return
        if calls == 2:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "audit-1",
                                    "type": "function",
                                    "function": {
                                        "name": "audit_chart_slots",
                                        "arguments": (
                                            '{"slots":[{"slot_id":"C1","title":"TD vs RBC CET1",'
                                            '"chart_type":"peer_rank_bar",'
                                            '"intent":"Compare CET1 ratio for TD and RBC",'
                                            '"banks":["TD-CA","RY-CA"],'
                                            '"periods":["Q2 2026"],'
                                            '"metrics":["CET1 ratio"]}]}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
            return
        shell = (
            '<aegis_final_shell>{"render_mode":"custom","summary":null,'
            '"tiles":[],"body_style":"user_requested_format"}</aegis_final_shell>'
        )
        slot = (
            '[[CHART_SLOT:{"slot_id":"C1","title":"TD vs RBC CET1",'
            '"chart_type":"peer_rank_bar","intent":"Compare CET1 ratio for TD and RBC",'
            '"banks":["TD-CA","RY-CA"],"periods":["Q2 2026"],"metrics":["CET1 ratio"]}]]'
        )
        yield {"choices": [{"delta": {"content": shell + "Lead\n" + slot + "\nTail"}}]}

    async def fake_run_research_tool(_arguments, context, *_args, **_kwargs):
        findings = [
            Finding(
                combo_label="Investor slides: RY-CA Q2 2026",
                summary="RY-CA CET1 ratio was 13.7%.",
                finding_type="quantitative",
                metric=MetricObservation(
                    metric_name="CET1 ratio",
                    metric_value="13.7",
                    unit="%",
                    period="Q2 2026",
                ),
                evidence_refs=[
                    EvidenceReference(
                        evidence_id="E1",
                        source_id="investor_slides",
                        source_label="Investor slides",
                        display_label="Investor slides E1",
                    )
                ],
            ),
            Finding(
                combo_label="Investor slides: TD-CA Q2 2026",
                summary="TD-CA CET1 ratio was 13.1%.",
                finding_type="quantitative",
                metric=MetricObservation(
                    metric_name="CET1 ratio",
                    metric_value="13.1",
                    unit="%",
                    period="Q2 2026",
                ),
                evidence_refs=[
                    EvidenceReference(
                        evidence_id="E2",
                        source_id="investor_slides",
                        source_label="Investor slides",
                        display_label="Investor slides E2",
                    )
                ],
            ),
        ]
        context["latest_research_result"] = ResearchResult(
            status="success",
            quick_summary="Research complete",
            findings=findings,
            evidence_registry={
                "investor_slides": {
                    "E1": findings[0].evidence_refs[0],
                    "E2": findings[1].evidence_refs[0],
                }
            },
        )
        return {"status": "success", "quick_summary": "Research complete", "findings": []}

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )
    monkeypatch.setattr(
        "aegis_agent.model.agents.tools.run_research_tool",
        fake_run_research_tool,
    )

    events = [
        event
        async for event in run_aegis_agent(
            [{"role": "user", "content": "Compare TD and RBC CET1 for Q2 2026."}],
            {"execution_id": "test"},
        )
    ]

    pending_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "chart_artifact" and event["content"]["status"] == "pending"
    )
    ready_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "chart_artifact" and event["content"]["status"] == "ready"
    )

    assert events[0]["type"] == "final_response_start"
    assert any(event.get("content") == "[[CHART:C1]]" for event in events)
    assert events[pending_index]["content"]["title"] == "TD vs RBC CET1"
    assert pending_index < ready_index
    assert events[ready_index]["content"]["spec"]["chart_type"] == "peer_rank_bar"


@pytest.mark.asyncio
async def test_agent_audits_chart_slots_then_runs_one_backfill_before_final(monkeypatch) -> None:
    """The agent should complete chart coverage before final-answer streaming."""
    calls = 0

    slot_args = (
        '{"slots":[{"slot_id":"C1","title":"TD vs RBC CET1",'
        '"chart_type":"peer_rank_bar",'
        '"intent":"Compare CET1 ratio for TD and RBC",'
        '"banks":["TD-CA","RY-CA"],'
        '"periods":["Q2 2026"],'
        '"metrics":["CET1 ratio"]}]}'
    )

    async def fake_stream_with_tools(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        tool_name = None
        tool_args = None
        tool_id = None
        if calls == 1:
            tool_name = "run_research"
            tool_id = "research-1"
            tool_args = (
                '{"question":"Compare CET1","sources":["investor_slides"],'
                '"combinations":[{"bank_symbol":"RY-CA","fiscal_year":2026,"quarter":"Q2"}]}'
            )
        elif calls == 2:
            tool_name = "audit_chart_slots"
            tool_id = "audit-1"
            tool_args = slot_args
        elif calls == 3:
            tool_name = "run_research"
            tool_id = "research-2"
            tool_args = (
                '{"question":"Find TD CET1 for chart backfill",'
                '"sources":["investor_slides"],'
                '"combinations":[{"bank_symbol":"TD-CA","fiscal_year":2026,"quarter":"Q2"}]}'
            )
        elif calls == 4:
            tool_name = "audit_chart_slots"
            tool_id = "audit-2"
            tool_args = slot_args

        if tool_name is not None:
            yield {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": tool_id,
                                    "type": "function",
                                    "function": {"name": tool_name, "arguments": tool_args},
                                }
                            ],
                        }
                    }
                ]
            }
            return

        shell = (
            '<aegis_final_shell>{"render_mode":"custom","summary":null,'
            '"tiles":[],"body_style":"user_requested_format"}</aegis_final_shell>'
        )
        slot = (
            '[[CHART_SLOT:{"slot_id":"C1","title":"TD vs RBC CET1",'
            '"chart_type":"peer_rank_bar","intent":"Compare CET1 ratio for TD and RBC",'
            '"banks":["TD-CA","RY-CA"],"periods":["Q2 2026"],"metrics":["CET1 ratio"]}]]'
        )
        yield {"choices": [{"delta": {"content": shell + "Lead\n" + slot + "\nTail"}}]}

    async def fake_run_research_tool(arguments, context, *_args, **_kwargs):
        symbol = arguments["combinations"][0]["bank_symbol"]
        value = "13.7" if symbol == "RY-CA" else "13.1"
        finding = Finding(
            combo_label=f"Investor slides: {symbol} Q2 2026",
            summary=f"{symbol} CET1 ratio was {value}%.",
            finding_type="quantitative",
            metric=MetricObservation(
                metric_name="CET1 ratio",
                metric_value=value,
                unit="%",
                period="Q2 2026",
            ),
            evidence_refs=[
                EvidenceReference(
                    source_id="investor_slides",
                    source_label="Investor slides",
                    display_label=f"{symbol} slide",
                )
            ],
        )
        result = ResearchResult(
            status="success",
            quick_summary=f"{symbol} found.",
            findings=[finding],
        )
        context["latest_research_result"] = _merge_latest_research_result(
            context.get("latest_research_result"), result
        )
        return result.model_dump(mode="json")

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )
    monkeypatch.setattr(
        "aegis_agent.model.agents.tools.run_research_tool",
        fake_run_research_tool,
    )

    events = [
        event
        async for event in run_aegis_agent(
            [{"role": "user", "content": "Compare TD and RBC CET1 for Q2 2026."}],
            {"execution_id": "test"},
        )
    ]

    first_agent_index = next(index for index, event in enumerate(events) if event["type"] == "agent")
    final_start_index = next(
        index for index, event in enumerate(events) if event["type"] == "final_response_start"
    )
    ready_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "chart_artifact" and event["content"]["status"] == "ready"
    )

    assert calls == 5
    assert final_start_index < first_agent_index
    assert events[first_agent_index]["content"] == "Lead\n"
    assert any(event.get("content") == "[[CHART:C1]]" for event in events)
    assert ready_index > first_agent_index


@pytest.mark.asyncio
async def test_agent_republishes_prior_chart_artifacts_for_followup(monkeypatch) -> None:
    """A follow-up chart request should have prior chart specs available to hydrate."""
    captured_messages = []

    async def fake_stream_with_tools(messages, *_args, **_kwargs):
        captured_messages.extend(messages)
        yield {"choices": [{"delta": {"content": "[[CHART:C2]]"}}]}

    prior_chart = {
        "chart_id": "C2",
        "chart_type": "trend_bar",
        "title": "RY-CA CET1 ratio by period",
        "subtitle": "Q1 2026 to Q3 2026 | %",
        "alt_text": "RY-CA CET1 ratio by period.",
        "status": "ready",
        "evidence_ids": ["E1"],
        "spec": {
            "chart_type": "trend_bar",
            "metric_name": "CET1 ratio",
            "unit": "%",
            "x_label": "Period",
            "y_label": "%",
            "facts": [],
        },
    }

    monkeypatch.setattr(
        "aegis_agent.model.agents.aegis_agent.stream_with_tools",
        fake_stream_with_tools,
    )

    events = [
        event
        async for event in run_aegis_agent(
            [
                {"role": "assistant", "content": "I can also show a bar chart."},
                {"role": "user", "content": "yes create the bar graph"},
            ],
            {
                "execution_id": "test",
                "prior_chart_artifacts": {"C2": prior_chart},
                "prior_evidence_registry": {
                    "investor_slides": {"E1": {"display_label": "Slide 1"}}
                },
            },
        )
    ]

    assert [event["type"] for event in events] == [
        "agent_status",
        "chart_artifact",
        "agent",
    ]
    assert events[0]["metadata"]["reused_evidence_registry"] is True
    assert events[1]["content"]["chart_id"] == "C2"
    assert events[2]["content"] == "[[CHART:C2]]"
    assert any(
        "Previously approved interactive chart options" in str(message.get("content"))
        and '"chart_id": "C2"' in str(message.get("content"))
        for message in captured_messages
    )
