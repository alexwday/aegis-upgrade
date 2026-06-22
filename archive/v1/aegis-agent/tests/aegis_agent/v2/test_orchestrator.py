"""Tests for the first V2 websocket orchestration loop."""

from __future__ import annotations

from datetime import datetime

import pytest

from aegis_agent.v2.orchestrator import V2SessionState, run_turn
from aegis_agent.v2.schemas import DataAvailabilityResponse, DataAvailabilityRow


@pytest.mark.asyncio
async def test_availability_turn_streams_tool_widget_and_message(monkeypatch) -> None:
    """Availability requests should stream the planned V2 lifecycle events."""
    async def fake_check_data_availability(_filters):
        return DataAvailabilityResponse(
            rows=[
                DataAvailabilityRow(
                    bank_id=1,
                    bank_name="Royal Bank of Canada",
                    bank_symbol="RY-CA",
                    bank_category="Canadian Banks",
                    bank_category_id="Canadian_Banks",
                    fiscal_year=2026,
                    quarter="Q1",
                    source_ids=["investor_slides"],
                    last_refreshed_at=datetime(2026, 6, 1, 12, 0),
                )
            ],
            fiscal_years=[2026],
            quarters=["Q1"],
            bank_categories=["Canadian Banks"],
        )

    monkeypatch.setattr(
        "aegis_agent.v2.orchestrator.check_data_availability",
        fake_check_data_availability,
    )

    state = V2SessionState(session_id="session_test")
    events = [event async for event in run_turn({"content": "what data is available?"}, state)]

    assert [event["type"] for event in events] == [
        "tool.started",
        "widget.created",
        "tool.completed",
        "widget.completed",
        "chat.message",
    ]
    assert state.latest_availability is not None
    assert events[3]["payload"]["widget"]["data"]["rows"][0]["bank_symbol"] == "RY-CA"
