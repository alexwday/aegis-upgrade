"""
Structured UI card helpers for agent clarification turns.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from uuid import uuid4

from .schemas import ChoiceCard, ChoiceOption


def build_choice_card_event(
    question: str,
    options: List[Dict[str, Any]],
    allow_free_text: bool = True,
    card_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a websocket event for a structured choice card."""
    card = ChoiceCard(
        card_id=card_id or f"choice-{uuid4().hex[:10]}",
        question=question,
        options=[ChoiceOption(**option) for option in options],
        allow_free_text=allow_free_text,
        metadata=metadata or {},
    )
    return {
        "type": "ui_card",
        "name": "choice_card",
        "content": card.model_dump(mode="json"),
    }
