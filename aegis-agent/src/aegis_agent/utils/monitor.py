"""
Process monitoring setup for tracking workflow execution.

Simple functions to collect monitoring data during workflow execution
and batch post to the database at completion.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ..connections.postgres_connector import insert_many
from .logging import get_logger
from .settings import config

logger = get_logger()

# Global list to store monitor entries during workflow execution
_monitor_entries: List[Dict[str, Any]] = []
_run_uuid: Optional[str] = None
_model_name: Optional[str] = None


def initialize_monitor(run_uuid: str, model_name: str) -> None:
    """
    Initialize monitoring for a workflow run.

    Args:
        run_uuid: Unique identifier for this workflow run
        model_name: Name of the model being executed (e.g., 'iris', 'aegis')
    """
    global _monitor_entries, _run_uuid, _model_name  # pylint: disable=global-statement
    # Module-level state required to accumulate entries across multiple function calls.

    _monitor_entries = []
    _run_uuid = run_uuid
    _model_name = model_name

    logger.info(
        "Process monitor initialized",
        run_uuid=run_uuid,
        model_name=model_name,
    )


def add_monitor_entry(  # pylint: disable=too-many-locals,too-many-arguments
    # pylint: disable=too-many-positional-arguments
    # Monitoring requires many params to capture comprehensive workflow metrics and metadata.
    stage_name: str,
    stage_start_time: datetime,
    stage_end_time: Optional[datetime] = None,
    status: str = "Success",
    llm_calls: Optional[List[Dict]] = None,
    decision_details: Optional[str] = None,
    error_message: Optional[str] = None,
    user_id: Optional[str] = None,
    custom_metadata: Optional[Dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> None:
    """
    Add a monitor entry for a completed stage.

    Args:
        stage_name: Name of the processing stage
        stage_start_time: When the stage started
        stage_end_time: When the stage ended (defaults to now)
        status: Status of the stage (Success, Failure, etc.)
        llm_calls: List of LLM call details for this stage
        decision_details: Details about decisions made
        error_message: Error message if stage failed
        user_id: Optional user identifier
        custom_metadata: Optional custom metadata
        notes: Optional notes
    """
    if not _run_uuid or not _model_name:
        logger.warning("Monitor not initialized, skipping entry")
        return

    # Use current time if end time not provided
    if stage_end_time is None:
        stage_end_time = datetime.now(timezone.utc)

    # Calculate duration in milliseconds
    # Even sub-millisecond operations will show as at least 1ms if they took any time
    time_delta = (stage_end_time - stage_start_time).total_seconds() * 1000
    duration_ms = max(1, int(time_delta)) if time_delta > 0 else 0

    # Calculate LLM totals if provided
    total_tokens = 0
    total_cost = Decimal("0")
    if llm_calls:
        for call in llm_calls:
            total_tokens += call.get("total_tokens", 0)
            total_cost += Decimal(str(call.get("cost", 0)))

    # Create entry with ALL fields (set to None if not provided)
    # This ensures consistent keys for SQLAlchemy batch inserts
    entry = {
        "run_uuid": _run_uuid,
        "model_name": _model_name,
        "stage_name": stage_name,
        "stage_start_time": stage_start_time,
        "stage_end_time": stage_end_time,
        "duration_ms": duration_ms,
        "status": status,
        "environment": config.environment,
        "llm_calls": None,
        "total_tokens": None,
        "total_cost": None,
        "decision_details": None,
        "error_message": None,
        "user_id": None,
        "custom_metadata": None,
        "notes": None,
    }

    # Update with actual values if provided
    if llm_calls:
        entry["llm_calls"] = llm_calls  # SQLAlchemy handles JSONB serialization
        entry["total_tokens"] = total_tokens
        entry["total_cost"] = total_cost

    if decision_details:
        entry["decision_details"] = decision_details

    if error_message:
        entry["error_message"] = error_message

    if user_id:
        entry["user_id"] = user_id

    if custom_metadata:
        entry["custom_metadata"] = custom_metadata

    if notes:
        entry["notes"] = notes

    _monitor_entries.append(entry)

    logger.debug(
        "Monitor entry added",
        stage_name=stage_name,
        status=status,
        duration_ms=duration_ms,
        total_entries=len(_monitor_entries),
    )


def create_stage_entry(
    stage_name: str,
    start_time: datetime,
    end_time: datetime,
    status: str = "Success",
    **kwargs,
) -> Dict[str, Any]:
    """
    Helper function to create a monitor entry dict.

    Args:
        stage_name: Name of the stage
        start_time: Stage start time
        end_time: Stage end time
        status: Stage status
        **kwargs: Additional fields (llm_calls, decision_details, etc.)

    Returns:
        Monitor entry dictionary
    """
    # Calculate duration in milliseconds
    time_delta = (end_time - start_time).total_seconds() * 1000
    duration_ms = max(1, int(time_delta)) if time_delta > 0 else 0

    entry = {
        "run_uuid": _run_uuid,
        "model_name": _model_name,
        "stage_name": stage_name,
        "stage_start_time": start_time,
        "stage_end_time": end_time,
        "duration_ms": duration_ms,
        "status": status,
        "environment": config.environment,
    }

    # Add any additional fields
    for key, value in kwargs.items():
        if value is not None:
            entry[key] = value

    return entry


def post_monitor_entries(execution_id: Optional[str] = None) -> int:
    """
    Post all collected monitor entries to the database.

    Args:
        execution_id: Optional execution ID for database operations

    Returns:
        Number of entries posted to database
    """
    global _monitor_entries  # pylint: disable=global-statement
    # Access global entries list to post accumulated monitoring data to database.

    if not _monitor_entries:
        logger.info("No monitor entries to post")
        return 0

    try:
        # Insert all entries to database
        rows_inserted = insert_many(
            "process_monitor_logs",
            _monitor_entries,
            execution_id=execution_id,
        )

        logger.info(
            "Monitor entries posted to database",
            entries_posted=rows_inserted,
            run_uuid=_run_uuid,
        )

        # Clear entries after successful post
        _monitor_entries = []

        return rows_inserted

    except Exception as e:
        logger.error(
            "Failed to post monitor entries",
            error=str(e),
            entry_count=len(_monitor_entries),
        )
        raise


async def post_monitor_entries_async(execution_id: Optional[str] = None) -> int:
    """
    Post all collected monitor entries to the database asynchronously.

    Args:
        execution_id: Optional execution ID for database operations

    Returns:
        Number of entries posted to database
    """
    global _monitor_entries  # pylint: disable=global-statement
    # Access global entries list to post accumulated monitoring data to database.

    if not _monitor_entries:
        logger.info("No monitor entries to post")
        return 0

    try:
        # Import async version
        from ..connections.postgres_connector import insert_many_async

        # Insert all entries to database
        rows_inserted = await insert_many_async(
            "process_monitor_logs",
            _monitor_entries,
            execution_id=execution_id,
        )

        logger.info(
            "Monitor entries posted to database",
            entries_posted=rows_inserted,
            run_uuid=_run_uuid,
        )

        # Clear entries after successful post
        _monitor_entries = []

        return rows_inserted

    except Exception as e:
        logger.error(
            "Failed to post monitor entries",
            error=str(e),
            entry_count=len(_monitor_entries),
        )
        raise


def get_monitor_entries() -> List[Dict[str, Any]]:
    """
    Get current monitor entries without posting.

    Returns:
        List of monitor entries
    """
    return _monitor_entries.copy()


def clear_monitor_entries() -> None:
    """Clear all monitor entries without posting."""
    global _monitor_entries  # pylint: disable=global-statement
    # Reset global entries list for testing or new monitoring session.
    _monitor_entries = []
    logger.debug("Monitor entries cleared")


def format_llm_call(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
    duration_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Format an LLM call record.

    Args:
        model: Model name
        prompt_tokens: Number of prompt tokens
        completion_tokens: Number of completion tokens
        cost: Cost in USD
        duration_ms: Optional duration in milliseconds

    Returns:
        Formatted LLM call dictionary
    """
    call = {
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost": cost,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if duration_ms:
        call["duration_ms"] = duration_ms

    return call
