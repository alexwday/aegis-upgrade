"""
Single conversational Aegis agent built on Chat Completions tool calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator, Dict, Iterable, List, Mapping, Optional

from ...connections.llm_connector import stream_with_tools
from ...utils.logging import get_logger
from ...utils.prompt_loader import load_prompt_from_db
from .chart_slots import ChartSlotStreamProcessor, chart_slot_instruction_text
from .charts import chart_instruction_text_from_artifacts
from .schemas import DEFAULT_DOCUMENT_SOURCES, FinalResponseShell
from .tools import AGENT_TOOLS, dispatch_tool_call


MAX_AGENT_LOOPS = 6
FINAL_SHELL_OPEN = "<aegis_final_shell>"
FINAL_SHELL_CLOSE = "</aegis_final_shell>"

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


def _final_response_protocol_message(context: Dict[str, Any]) -> str:
    """Build dynamic final-answer streaming instructions."""
    prior_chart_instruction = str(context.get("chart_instruction") or "").strip()
    chart_instruction_parts = []
    if prior_chart_instruction:
        chart_instruction_parts.append(prior_chart_instruction)
    chart_instruction_parts.append(chart_slot_instruction_text())
    chart_instruction = "\n".join(chart_instruction_parts)
    return (
        "Final response streaming protocol: after research is complete, do not call "
        "start_final_response for normal final answers. Instead, begin the final assistant "
        f"message with `{FINAL_SHELL_OPEN}` followed by a single JSON object matching the "
        "final response shell schema, then "
        f"`{FINAL_SHELL_CLOSE}`, then immediately continue with the markdown body. "
        "The shell JSON must contain render_mode, summary, tiles, and body_style. "
        "Use render_mode default_brief and body_style default_brief for the default "
        "research brief; use custom/user_requested_format when the user requested a "
        "specific format; use no_available_data/user_requested_format when no data is available. "
        "Do not wrap the body in JSON or code fences. "
        f"{chart_instruction}"
    )


def _messages_for_agent(
    conversation_messages: Iterable[Dict[str, str]],
    context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Build Chat Completions messages for the agent loop."""
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": _load_system_prompt()},
        {"role": "system", "content": _source_scope_message(context)},
        {"role": "system", "content": _final_response_protocol_message(context)},
    ]
    for message in conversation_messages:
        role = message.get("role")
        content = message.get("content")
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    return messages


def _ready_prior_chart_artifacts(context: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Return ready chart artifacts carried over from the previous research turn."""
    raw_artifacts = context.get("prior_chart_artifacts")
    if not isinstance(raw_artifacts, Mapping):
        return {}

    artifacts: Dict[str, Dict[str, Any]] = {}
    for raw_chart_id, raw_artifact in raw_artifacts.items():
        if not isinstance(raw_artifact, Mapping):
            continue
        chart_id = str(raw_artifact.get("chart_id") or raw_chart_id).strip()
        if not chart_id or str(raw_artifact.get("status") or "ready") != "ready":
            continue
        artifacts[chart_id] = dict(raw_artifact)
    return artifacts


def _prime_prior_chart_context(context: Dict[str, Any]) -> None:
    """Make prior approved charts available to follow-up turns."""
    artifacts = _ready_prior_chart_artifacts(context)
    if not artifacts:
        return

    if not str(context.get("chart_instruction") or "").strip():
        context["chart_instruction"] = chart_instruction_text_from_artifacts(artifacts)
    current_artifacts: Dict[str, Dict[str, Any]] = context.setdefault("chart_artifacts", {})
    current_artifacts.update(artifacts)

    event_queue: Optional[asyncio.Queue] = context.get("background_event_queue")
    if event_queue is None:
        return

    prior_registry = context.get("prior_evidence_registry")
    if isinstance(prior_registry, Mapping):
        event_queue.put_nowait(
            {
                "type": "agent_status",
                "name": "aegis",
                "content": "Evidence registry ready.",
                "metadata": {
                    "evidence_registry": dict(prior_registry),
                    "internal": True,
                    "reused_evidence_registry": True,
                },
            }
        )

    for artifact in artifacts.values():
        event_queue.put_nowait(
            {
                "type": "chart_artifact",
                "name": "aegis",
                "content": artifact,
            }
        )


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
    parse_final_shell: bool = False,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Stream one model step and yield events plus the accumulated assistant message."""
    content_parts: List[str] = []
    tool_calls_by_index: Dict[int, Dict[str, Any]] = {}
    shell_parser = _FinalShellStreamParser() if parse_final_shell else None
    slot_processor = ChartSlotStreamProcessor(context) if stream_content else None

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
                if shell_parser is not None:
                    for event in shell_parser.push(content_delta):
                        for processed_event in _process_chart_slot_event(event, slot_processor):
                            yield {"event": processed_event}
                else:
                    event = {"type": "agent", "name": "aegis", "content": content_delta}
                    for processed_event in _process_chart_slot_event(event, slot_processor):
                        yield {"event": processed_event}
        async for event in _drain_ready_background_events(context):
            yield {"event": event}

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
    elif stream_content and shell_parser is not None:
        for event in shell_parser.finish():
            for processed_event in _process_chart_slot_event(event, slot_processor):
                yield {"event": processed_event}
    if stream_content and slot_processor is not None:
        for event in slot_processor.finish():
            yield {"event": event}

    yield {"assistant_message": assistant_message}


def _process_chart_slot_event(
    event: Dict[str, Any],
    slot_processor: Optional[ChartSlotStreamProcessor],
) -> List[Dict[str, Any]]:
    """Replace chart slots in streamed agent events and emit pending chart artifacts."""
    if slot_processor is None or event.get("type") != "agent":
        return [event]
    return slot_processor.push(str(event.get("content") or ""))


class _FinalShellStreamParser:
    """Parse an optional leading final shell block from streamed content."""

    def __init__(self) -> None:
        self.buffer = ""
        self.started = False
        self.done = False

    def push(self, content_delta: str) -> List[Dict[str, Any]]:
        """Return websocket events produced by one final-answer content delta."""
        if self.done:
            return [{"type": "agent", "name": "aegis", "content": content_delta}]

        self.buffer += content_delta
        stripped = self.buffer.lstrip()
        if not stripped:
            return []

        if not self.started:
            if FINAL_SHELL_OPEN.startswith(stripped):
                return []
            if not stripped.startswith(FINAL_SHELL_OPEN):
                self.done = True
                fallback = self.buffer
                self.buffer = ""
                return [
                    {
                        "type": "final_response_start",
                        "name": "aegis",
                        "content": _default_final_shell(),
                    },
                    {"type": "agent", "name": "aegis", "content": fallback},
                ]
            self.started = True

        close_index = stripped.find(FINAL_SHELL_CLOSE)
        if close_index < 0:
            return []

        shell_text = stripped[len(FINAL_SHELL_OPEN) : close_index].strip()
        body_text = stripped[close_index + len(FINAL_SHELL_CLOSE) :]
        self.done = True
        self.buffer = ""
        events = [
            {
                "type": "final_response_start",
                "name": "aegis",
                "content": _parse_final_shell_payload(shell_text),
            }
        ]
        if body_text:
            events.append({"type": "agent", "name": "aegis", "content": body_text})
        return events

    def finish(self) -> List[Dict[str, Any]]:
        """Flush any buffered text when the model ends without a complete shell."""
        if self.done or not self.buffer:
            return []
        fallback = self.buffer
        self.buffer = ""
        self.done = True
        return [
            {
                "type": "final_response_start",
                "name": "aegis",
                "content": _default_final_shell(),
            },
            {"type": "agent", "name": "aegis", "content": fallback},
        ]


def _parse_final_shell_payload(raw_shell: str) -> Dict[str, Any]:
    """Parse and validate the streamed final shell JSON."""
    try:
        shell = FinalResponseShell.model_validate(json.loads(raw_shell))
        return shell.model_dump(mode="json")
    except Exception:  # pylint: disable=broad-exception-caught
        return _default_final_shell()


def _default_final_shell() -> Dict[str, Any]:
    """Return a conservative final response shell for parser fallback."""
    return {
        "render_mode": "custom",
        "summary": None,
        "tiles": [],
        "body_style": "user_requested_format",
    }


async def _drain_ready_background_events(context: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
    """Yield background chart events already available without blocking streaming."""
    queue = context.get("background_event_queue")
    if queue is None:
        return
    while not queue.empty():
        yield queue.get_nowait()


async def _drain_chart_worker_events(context: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
    """Drain chart worker events until all launched chart workers have completed."""
    queue: Optional[asyncio.Queue] = context.get("background_event_queue")
    tasks: List[asyncio.Task] = [
        task for task in context.get("chart_worker_tasks", []) if isinstance(task, asyncio.Task)
    ]
    if queue is None or not tasks:
        return

    while True:
        while not queue.empty():
            yield queue.get_nowait()

        pending = [task for task in tasks if not task.done()]
        if not pending:
            break

        done, _pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                task.result()
            except Exception as exc:  # pylint: disable=broad-exception-caught
                get_logger().warning(
                    "chart_slot.worker_task_failed",
                    execution_id=context.get("execution_id"),
                    error=str(exc),
                )

    while not queue.empty():
        yield queue.get_nowait()


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
    context.setdefault("background_event_queue", asyncio.Queue())
    _prime_prior_chart_context(context)
    messages = _messages_for_agent(conversation_messages, context)
    final_response_started = False
    final_stream_allowed = False

    async for event in _drain_ready_background_events(context):
        yield event

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
            stream_content=final_response_started or final_stream_allowed,
            parse_final_shell=final_stream_allowed and not final_response_started,
        ):
            if "event" in item:
                yield item["event"]
            else:
                assistant_message = item["assistant_message"]

        tool_calls = assistant_message.get("tool_calls") or []

        if not tool_calls:
            async for event in _drain_ready_background_events(context):
                yield event
            async for event in _drain_chart_worker_events(context):
                yield event
            return

        messages.append(assistant_message)

        tool_results: List[Dict[str, Any]] = []
        async for item in _run_tool_calls(tool_calls, context):
            if "event" in item:
                if item["event"].get("type") == "final_response_start":
                    final_response_started = True
                    final_stream_allowed = False
                yield item["event"]
            else:
                tool_results.append(item)

        awaiting_user = False
        research_completed = False
        for item in tool_results:
            tool_call = item["tool_call"]
            result = item["result"]
            if result.get("status") == "awaiting_user":
                awaiting_user = True
            if tool_call.get("function", {}).get("name") == "run_research" and result.get(
                "status"
            ) not in {"needs_clarification", "error"}:
                research_completed = True
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        if awaiting_user:
            return

        if research_completed and not final_response_started:
            final_stream_allowed = True
            messages.append({"role": "system", "content": _final_response_protocol_message(context)})

    yield {
        "type": "error",
        "name": "aegis",
        "content": "The agent reached its tool-loop limit before producing a final answer.",
    }
