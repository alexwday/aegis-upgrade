"""RTS subagent."""

from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List

from ....utils.logging import get_logger
from ....utils.monitor import add_monitor_entry
from .pipeline import SEARCH_TOP_K, format_retrieval_response, run_retrieval_pipeline

class QARequest:  # pylint: disable=too-few-public-methods
    """Compatibility request type for the legacy subagent streaming signature."""


async def rts_agent(
    conversation: List[Dict[str, str]],
    latest_message: str,
    bank_period_combinations: List[Dict[str, Any]],
    basic_intent: str,
    full_intent: str,
    database_id: str,
    context: Dict[str, Any],
    user_req: QARequest,
) -> AsyncGenerator[Dict[str, str], None]:
    """Process RTS queries.

    Args:
        conversation: Full chat history.
        latest_message: Most recent user message.
        bank_period_combinations: Bank and period filters selected upstream.
        basic_intent: Simple query interpretation.
        full_intent: Enriched query interpretation.
        database_id: Database identifier for this subagent.
        context: Runtime context such as execution_id and auth configuration.
        user_req: Original request object.

    Yields:
        Aegis subagent streaming dictionaries.
    """
    _ = conversation, user_req
    logger = get_logger()
    execution_id = context.get("execution_id")
    stage_start = datetime.now(timezone.utc)
    query_text = full_intent or basic_intent or latest_message

    logger.info(
        "subagent.rts.started",
        execution_id=execution_id,
        combinations=len(bank_period_combinations),
        query_preview=query_text[:160],
    )

    try:
        if not bank_period_combinations:
            yield _chunk(database_id, "No bank/period combinations were provided.")
            return

        results = await retrieve_rts(
            query_text=query_text,
            latest_message=latest_message,
            bank_period_combinations=bank_period_combinations,
            context=context,
        )
        response = format_retrieval_response(results)

        add_monitor_entry(
            stage_name="Subagent_rts",
            stage_start_time=stage_start,
            stage_end_time=datetime.now(timezone.utc),
            status="Success",
            decision_details=(
                f"Retrieved {len(results['chunks'])} evidence chunks and "
                f"{len(results['findings'])} findings from RTS"
            ),
            custom_metadata={
                "database_id": database_id,
                "combo_count": len(bank_period_combinations),
                "chunk_count": len(results["chunks"]),
                "finding_count": len(results["findings"]),
                "sub_queries": len(results["prepared_query"]["sub_queries"]),
                "keywords": len(results["prepared_query"]["keywords"]),
                "metrics": len(results["prepared_query"]["metrics"]),
            },
        )

        yield _chunk(database_id, response)

    except Exception as exc:  # pylint: disable=broad-except
        logger.error(
            "subagent.rts.error",
            execution_id=execution_id,
            error=str(exc),
            exc_info=True,
        )
        add_monitor_entry(
            stage_name="Subagent_rts",
            stage_start_time=stage_start,
            stage_end_time=datetime.now(timezone.utc),
            status="Error",
            error_message=str(exc),
            custom_metadata={"database_id": database_id, "error_type": type(exc).__name__},
        )
        yield _chunk(database_id, f"Error retrieving RTS: {exc}")


async def retrieve_rts(
    query_text: str,
    latest_message: str,
    bank_period_combinations: List[Dict[str, Any]],
    context: Dict[str, Any],
    top_k: int = SEARCH_TOP_K,
) -> Dict[str, Any]:
    """Retrieve and research RTS evidence."""
    return await run_retrieval_pipeline(
        query_text=query_text,
        latest_message=latest_message,
        bank_period_combinations=bank_period_combinations,
        context=context,
        search_top_k=top_k,
    )


def _chunk(database_id: str, content: str) -> Dict[str, str]:
    """Return a standard Aegis subagent stream chunk."""
    return {"type": "subagent", "name": database_id, "content": content}
