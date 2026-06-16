"""Source metadata and active-source detection for shared pipeline helpers."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PIPELINE_ROOT.parent
SOURCES_ROOT = PIPELINE_ROOT / "sources"
SOURCE_ENV_VAR = "AEGIS_PIPELINE_SOURCE"


@dataclass(frozen=True)
class SourceContext:
    """Static runtime metadata for one Aegis pipeline source."""

    key: str
    data_source: str
    prompt_layer: str
    document_input_dir: str
    master_table: str
    application_slug: str
    pdf_raw_ocr_model: str = "gpt-5.4"
    allow_tool_reasoning_effort: bool = False
    has_extraction_model_alias: bool = True

    @property
    def root(self) -> Path:
        return SOURCES_ROOT / self.key

    @property
    def env_path(self) -> Path:
        return self.root / ".env"

    @property
    def default_input_path(self) -> Path:
        return PROJECT_ROOT / self.document_input_dir

    @property
    def default_output_path(self) -> Path:
        return self.root / "data-output" / "master"

    @property
    def tokenizer_cache_path(self) -> Path:
        return PIPELINE_ROOT / "tokenizer-cache"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"


SOURCES: dict[str, SourceContext] = {
    "investor_slides": SourceContext(
        key="investor_slides",
        data_source="investor-slides",
        prompt_layer="investor_slides",
        document_input_dir="aegis-documents/investor_slides",
        master_table="investor_slides_master_data",
        application_slug="investor-slides",
        pdf_raw_ocr_model="gpt-5.4",
        allow_tool_reasoning_effort=True,
    ),
    "supplementary_financials": SourceContext(
        key="supplementary_financials",
        data_source="financial-supp",
        prompt_layer="supplementary_financials",
        document_input_dir="aegis-documents/supplementary_financials",
        master_table="financial_supp_master_data",
        application_slug="financial-supp",
        has_extraction_model_alias=False,
    ),
    "rts": SourceContext(
        key="rts",
        data_source="rts",
        prompt_layer="rts",
        document_input_dir="aegis-documents/rts",
        master_table="rts_master_data",
        application_slug="rts",
        pdf_raw_ocr_model="gpt-5-mini",
    ),
    "pillar3": SourceContext(
        key="pillar3",
        data_source="pillar3",
        prompt_layer="pillar3",
        document_input_dir="aegis-documents/pillar3",
        master_table="pillar3_master_data",
        application_slug="pillar3",
        has_extraction_model_alias=False,
    ),
    "transcripts": SourceContext(
        key="transcripts",
        data_source="earnings-transcripts",
        prompt_layer="transcripts",
        document_input_dir="aegis-documents/transcripts",
        master_table="earnings_transcripts_master_data",
        application_slug="earnings-transcripts",
    ),
    "event_transcripts": SourceContext(
        key="event_transcripts",
        data_source="event-transcripts",
        prompt_layer="event_transcripts",
        document_input_dir="aegis-documents/event_transcripts",
        master_table="event_transcripts_master_data",
        application_slug="event-transcripts",
    ),
}

_active_source_key: str | None = None


def source_keys() -> tuple[str, ...]:
    """Return source keys in stable CLI order."""
    return tuple(SOURCES)


def set_active_source(source_key: str | Path) -> SourceContext:
    """Set and return the active source context for this process."""
    global _active_source_key
    key = _normalize_source_key(source_key)
    _active_source_key = key
    os.environ[SOURCE_ENV_VAR] = key
    return SOURCES[key]


def get_source_context() -> SourceContext:
    """Return active source metadata, inferring the source when needed."""
    key = _active_source_key or _source_from_env() or _source_from_cwd() or _source_from_sys_path()
    if key is None:
        choices = ", ".join(source_keys())
        raise RuntimeError(
            "Unable to determine active Aegis pipeline source. "
            f"Set {SOURCE_ENV_VAR} to one of: {choices}."
        )
    return SOURCES[key]


def _normalize_source_key(source_key: str | Path) -> str:
    value = str(source_key)
    key = Path(value).name if "/" in value else value
    if key not in SOURCES:
        choices = ", ".join(source_keys())
        raise ValueError(f"Unknown Aegis pipeline source {key!r}; choose one of: {choices}")
    return key


def _source_from_env() -> str | None:
    key = os.environ.get(SOURCE_ENV_VAR, "").strip()
    return key if key in SOURCES else None


def _source_from_cwd() -> str | None:
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        return None
    return _source_from_path(cwd)


def _source_from_sys_path() -> str | None:
    for entry in sys.path:
        if not entry:
            continue
        try:
            path = Path(entry).expanduser().resolve()
        except OSError:
            continue
        key = _source_from_path(path)
        if key is not None:
            return key
    return None


def _source_from_path(path: Path) -> str | None:
    for key, context in SOURCES.items():
        root = context.root.resolve()
        if path == root or root in path.parents:
            return key
    return None
