"""Tests for the V2 process monitor taxonomy."""

from __future__ import annotations

from aegis_agent.v2.process_monitor import (
    PROCESS_MONITOR_SCHEMA_VERSION,
    STATUS_FAILURE,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    process_stage_from_event,
    turn_process_stage,
)


def test_tool_event_maps_to_stable_stage() -> None:
    """Tool events should use a stable V2 stage namespace."""
    stage = process_stage_from_event(
        {
            "type": "tool.started",
            "event_id": "event-1",
            "payload": {
                "tool_id": "tool-1",
                "name": "quick_research",
                "chunk_limit": 80,
                "sources": ["rts"],
            },
        },
        conversation_id="conversation-1",
        run_uuid="run-1",
    )

    assert stage.stage_name == "V2_Tool_Quick_Research_Started"
    assert stage.status == STATUS_RUNNING
    assert stage.custom_metadata["schema_version"] == PROCESS_MONITOR_SCHEMA_VERSION
    assert stage.custom_metadata["stage_category"] == "tool"
    assert stage.custom_metadata["stage_subject"] == "quick_research"
    assert stage.custom_metadata["stage_action"] == "started"
    assert stage.custom_metadata["payload_summary"]["chunk_limit"] == 80


def test_artifact_event_summarizes_without_html_content() -> None:
    """Artifact process metadata should not store large HTML bodies."""
    stage = process_stage_from_event(
        {
            "type": "artifact.created",
            "event_id": "event-2",
            "payload": {
                "artifact": {
                    "id": "artifact-1",
                    "kind": "quick_search",
                    "title": "Quick search",
                    "html": "<html>large body</html>",
                    "evidence_ids": ["E1", "E2"],
                }
            },
        },
        conversation_id="conversation-1",
        run_uuid="run-1",
    )

    assert stage.stage_name == "V2_Artifact_Quick_Search_Created"
    assert stage.status == STATUS_SUCCESS
    artifact_summary = stage.custom_metadata["payload_summary"]["artifact"]
    assert artifact_summary == {
        "id": "artifact-1",
        "kind": "quick_search",
        "title": "Quick search",
        "evidence_count": 2,
    }
    assert "html" not in str(stage.custom_metadata)


def test_failed_event_and_turn_stage_taxonomy() -> None:
    """Failures and turn lifecycle rows should share the same schema version."""
    failed = process_stage_from_event(
        {
            "type": "tool.failed",
            "payload": {"name": "deep_research", "error": "missing table"},
        },
        conversation_id="conversation-1",
        run_uuid="run-1",
    )
    accepted = turn_process_stage(
        action="user_message_received",
        status=STATUS_SUCCESS,
        conversation_id="conversation-1",
        run_uuid="run-1",
        details="accepted",
        payload={
            "has_filters": True,
            "has_context": False,
            "model_selection": "large",
            "search_selection": "deep",
        },
    )

    assert failed.stage_name == "V2_Tool_Deep_Research_Failed"
    assert failed.status == STATUS_FAILURE
    assert failed.error_message == "missing table"
    assert accepted.stage_name == "V2_Turn_Websocket_User_Message_Received"
    assert accepted.custom_metadata["payload_summary"]["search_selection"] == "deep"
