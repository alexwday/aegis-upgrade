"""
Single conversational Aegis agent built on Chat Completions tool calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional

from ...connections.llm_connector import stream_with_tools
from ...utils.logging import get_logger
from ...utils.prompt_loader import load_prompt_from_db
from .schemas import DEFAULT_DOCUMENT_SOURCES
from .tools import AGENT_TOOLS, dispatch_tool_call


MAX_AGENT_LOOPS = 6

SOURCE_LABELS = {
    "transcripts": "transcripts",
    "event_transcripts": "event transcripts",
    "investor_slides": "investor slides",
    "supplementary_financials": "supplementary financials",
    "rts": "reports to shareholders",
    "pillar3": "Pillar 3",
}


def _load_system_prompt() -> str:
    """Load the agent system prompt from PostgreSQL."""
    prompt_data = load_prompt_from_db(
        "aegis_agent",
        "system",
        compose_with_globals=False,
    )
    system_prompt = prompt_data.get("system_prompt")
    if not system_prompt:
        raise ValueError("Prompt aegis/aegis_agent/system has no system_prompt")
    return str(system_prompt)


def _validated_sources(raw_sources: Any) -> List[str]:
    """Return known source IDs while preserving the configured order."""
    if not isinstance(raw_sources, list):
        return []
    seen = set()
    valid_sources = set(DEFAULT_DOCUMENT_SOURCES)
    selected: List[str] = []
    for source in raw_sources:
        normalized = str(source or "").strip()
        if normalized in valid_sources and normalized not in seen:
            seen.add(normalized)
            selected.append(normalized)
    return selected


def _source_scope_message(context: Dict[str, Any]) -> str:
    """Build a dynamic source-scope instruction for the current turn."""
    selected_sources = _validated_sources(context.get("source_filter"))
    default_sources = _validated_sources(context.get("db_names")) or list(DEFAULT_DOCUMENT_SOURCES)

    if selected_sources:
        source_list = ", ".join(selected_sources)
        label_list = ", ".join(SOURCE_LABELS[source] for source in selected_sources)
        return (
            "User-selected source filter for this turn: "
            f"{source_list}. Only call run_research with those source IDs and describe the "
            f"research scope as using only: {label_list}."
        )

    source_list = ", ".join(default_sources)
    label_list = ", ".join(SOURCE_LABELS[source] for source in default_sources)
    return (
        "No source filter is selected for this turn. If the user asks for all sources, "
        f"call run_research with all {len(default_sources)} sources: {source_list}. "
        f"Describe them as: {label_list}."
    )


def _messages_for_agent(
    conversation_messages: Iterable[Dict[str, str]],
    context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Build Chat Completions messages for the agent loop."""
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _load_system_prompt()},
        {"role": "system", "content": _source_scope_message(context)},
    ]
    for message in conversation_messages:
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    return messages


def _tool_call_from_delta(existing: Dict[str, Any], delta: Dict[str, Any]) -> Dict[str, Any]:
    """Merge one streamed tool-call delta into an accumulated tool call."""
    if delta.get("id"):
        existing["id"] = delta["id"]
    if delta.get("type"):
        existing["type"] = delta["type"]

    function_delta = delta.get("function") or {}
    function = existing.setdefault("function", {"name": "", "arguments": ""})
    if function_delta.get("name"):
        function["name"] += function_delta["name"]
    if function_delta.get("arguments"):
        function["arguments"] += function_delta["arguments"]
    return existing


async def _stream_model_step(
    messages: List[Dict[str, Any]],
    context: Dict[str, Any],
    stream_content: bool = False,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Stream one model step and yield events plus the accumulated assistant message."""
    content_parts: List[str] = []
    tool_calls_by_index: Dict[int, Dict[str, Any]] = {}

    async for chunk in stream_with_tools(
        messages=messages,
        tools=AGENT_TOOLS,
        context=context,
        llm_params={"tool_choice": "auto"},
    ):
        choice = (chunk.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}

        content_delta = delta.get("content")
        if content_delta:
            content_parts.append(content_delta)
            if stream_content:
                yield {"event": {"type": "agent", "name": "aegis", "content": content_delta}}

        for tool_delta in delta.get("tool_calls") or []:
            index = int(tool_delta.get("index", 0))
            current = tool_calls_by_index.setdefault(
                index,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            _tool_call_from_delta(current, tool_delta)

    assistant_message: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts),
    }
    tool_calls = [
        tool_calls_by_index[index]
        for index in sorted(tool_calls_by_index)
        if tool_calls_by_index[index].get("function", {}).get("name")
    ]
    if tool_calls:
        assistant_message["tool_calls"] = tool_calls
    elif content_parts and not stream_content:
        yield {"event": {"type": "agent", "name": "aegis", "content": "".join(content_parts)}}

    yield {"assistant_message": assistant_message}


async def _drain_tool_events(
    tasks: List[asyncio.Task],
    output_queue: asyncio.Queue,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Yield queued tool events while tool tasks are running."""
    while True:
        pending = [task for task in tasks if not task.done()]
        if not pending and output_queue.empty():
            break

        get_task = asyncio.create_task(output_queue.get())
        wait_set = set(pending + [get_task])
        done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

        if get_task in done:
            yield get_task.result()
        else:
            get_task.cancel()
            try:
                await get_task
            except asyncio.CancelledError:
                pass

        while not output_queue.empty():
            yield output_queue.get_nowait()


async def _run_tool_calls(
    tool_calls: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run tool calls concurrently and yield intermediate UI events plus final results."""
    output_queue: asyncio.Queue = asyncio.Queue()
    tasks = [
        asyncio.create_task(dispatch_tool_call(tool_call, context, output_queue))
        for tool_call in tool_calls
    ]

    async for event in _drain_tool_events(tasks, output_queue):
        yield {"event": event}

    results = await asyncio.gather(*tasks)
    for tool_call, result in zip(tool_calls, results):
        yield {"tool_call": tool_call, "result": result}


async def run_aegis_agent(
    conversation_messages: List[Dict[str, str]],
    context: Dict[str, Any],
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Run one conversational turn of the single Aegis agent.

    The agent loops over Chat Completions responses, executes requested tools,
    feeds structured tool results back to the model, and streams UI events.
    """
    logger = get_logger()
    messages = _messages_for_agent(conversation_messages, context)
    final_response_started = False

    for loop_index in range(MAX_AGENT_LOOPS):
        logger.info(
            "agent.loop.started",
            execution_id=context.get("execution_id"),
            loop_index=loop_index,
            message_count=len(messages),
        )
        assistant_message: Dict[str, Any] = {}
        async for item in _stream_model_step(
            messages,
            context,
            stream_content=final_response_started,
        ):
            if "event" in item:
                yield item["event"]
            else:
                assistant_message = item["assistant_message"]

        tool_calls = assistant_message.get("tool_calls") or []

        if not tool_calls:
            return

        messages.append(assistant_message)

        tool_results: List[Dict[str, Any]] = []
        async for item in _run_tool_calls(tool_calls, context):
            if "event" in item:
                if item["event"].get("type") == "final_response_start":
                    final_response_started = True
                yield item["event"]
            else:
                tool_results.append(item)

        awaiting_user = False
        for item in tool_results:
            tool_call = item["tool_call"]
            result = item["result"]
            if result.get("status") == "awaiting_user":
                awaiting_user = True
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        if awaiting_user:
            return

    yield {
        "type": "error",
        "name": "aegis",
        "content": "The agent reached its tool-loop limit before producing a final answer.",
    }
