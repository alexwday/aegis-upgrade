"""Shared source metadata for Aegis V2 tools."""

from __future__ import annotations

from typing import Iterable


SOURCE_LABELS: dict[str, str] = {
    "transcripts": "Transcripts",
    "event_transcripts": "Event transcripts",
    "investor_slides": "Investor slides",
    "supplementary_financials": "Supplementary financials",
    "rts": "Reports to shareholders",
    "pillar3": "Pillar 3",
}

SOURCE_TABLES: dict[str, str] = {
    "transcripts": "aegis-earnings-transcripts-data",
    "event_transcripts": "aegis-event-transcripts-data",
    "investor_slides": "aegis-investor-slides-data",
    "supplementary_financials": "aegis-financial-supp-data",
    "rts": "aegis-rts-data",
    "pillar3": "aegis-pillar3-data",
}

SOURCE_IDS: tuple[str, ...] = tuple(SOURCE_LABELS)


def source_label(source_id: str) -> str:
    """Return a display label for one source id."""
    return SOURCE_LABELS.get(source_id, source_id.replace("_", " ").title())


def normalize_source_ids(values: Iterable[str] | None) -> list[str]:
    """Return known source ids in stable order."""
    requested = {str(value).strip() for value in values or [] if str(value).strip()}
    if not requested:
        return list(SOURCE_IDS)
    return [source_id for source_id in SOURCE_IDS if source_id in requested]
