"""
Progress log primitives for long-running Aegis agent tools.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from .schemas import ProgressEvent


class ResearchProgressStore:
    """Thread-safe async progress log for one research tool call."""

    def __init__(self, output_queue: Optional[asyncio.Queue] = None) -> None:
        """Initialize the store."""
        self._events: List[ProgressEvent] = []
        self._lock = asyncio.Lock()
        self._complete = asyncio.Event()
        self._changed = asyncio.Event()
        self._output_queue = output_queue

    async def add(
        self,
        source: str,
        stage: str,
        status: str,
        message: str,
        combo_label: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        visible: bool = False,
    ) -> ProgressEvent:
        """Add one progress event and optionally emit it to the UI queue."""
        event = ProgressEvent(
            source=source,
            stage=stage,
            status=status,
            message=message,
            combo_label=combo_label,
            metadata=metadata or {},
        )
        async with self._lock:
            self._events.append(event)
            self._changed.set()

        if visible and self._output_queue is not None:
            await self._output_queue.put(
                {
                    "type": "agent_status",
                    "name": source,
                    "content": message,
                    "metadata": event.model_dump(mode="json"),
                }
            )

        return event

    async def snapshot(self) -> List[ProgressEvent]:
        """Return a stable copy of current progress events."""
        async with self._lock:
            return list(self._events)

    def mark_complete(self) -> None:
        """Mark the tool call complete."""
        self._complete.set()
        self._changed.set()

    async def wait_complete(self) -> None:
        """Wait until the tool call is complete."""
        await self._complete.wait()

    async def wait_changed(self) -> None:
        """Wait until progress changes or the tool call completes."""
        await self._changed.wait()
        self._changed.clear()

    @property
    def is_complete(self) -> bool:
        """Whether the owning tool has completed."""
        return self._complete.is_set()


async def emit_event(output_queue: Optional[asyncio.Queue], event: Dict[str, Any]) -> None:
    """Put an event on the output queue when one is available."""
    if output_queue is not None:
        await output_queue.put(event)
