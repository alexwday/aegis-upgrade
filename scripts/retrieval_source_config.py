"""Shared source registry for root-level Aegis retrieval table scripts."""

from __future__ import annotations

import argparse
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
AEGIS_PIPELINE_ROOT = PROJECT_ROOT / "aegis-pipeline"
PIPELINE_ROOT = AEGIS_PIPELINE_ROOT / "sources"


@dataclass(frozen=True)
class RetrievalSource:
    """Static metadata for one pipeline source."""

    key: str
    label: str
    root: Path
    data_table: str
    embeddings_table: str
    data_staging_table: str
    embeddings_staging_table: str

    @property
    def application_slug(self) -> str:
        """Return a hyphenated source slug suitable for PostgreSQL app names."""
        return self.key.replace("_", "-")


@dataclass(frozen=True)
class SourceModules:
    """Imported modules for a selected source package."""

    finalize: ModuleType
    manifest: ModuleType
    config_setup: ModuleType


SOURCES: dict[str, RetrievalSource] = {
    "investor_slides": RetrievalSource(
        key="investor_slides",
        label="investor slide",
        root=PIPELINE_ROOT / "investor_slides",
        data_table="aegis-investor-slides-data",
        embeddings_table="aegis-investor-slides-embeddings",
        data_staging_table="aegis_investor_slides_data_load_stage",
        embeddings_staging_table="aegis_investor_slides_embeddings_load_stage",
    ),
    "supplementary_financials": RetrievalSource(
        key="supplementary_financials",
        label="financial supplement",
        root=PIPELINE_ROOT / "supplementary_financials",
        data_table="aegis-financial-supp-data",
        embeddings_table="aegis-financial-supp-embeddings",
        data_staging_table="aegis_financial_supp_data_load_stage",
        embeddings_staging_table="aegis_financial_supp_embeddings_load_stage",
    ),
    "rts": RetrievalSource(
        key="rts",
        label="RTS",
        root=PIPELINE_ROOT / "rts",
        data_table="aegis-rts-data",
        embeddings_table="aegis-rts-embeddings",
        data_staging_table="aegis_rts_data_load_stage",
        embeddings_staging_table="aegis_rts_embeddings_load_stage",
    ),
    "pillar3": RetrievalSource(
        key="pillar3",
        label="Pillar 3",
        root=PIPELINE_ROOT / "pillar3",
        data_table="aegis-pillar3-data",
        embeddings_table="aegis-pillar3-embeddings",
        data_staging_table="aegis_pillar3_data_load_stage",
        embeddings_staging_table="aegis_pillar3_embeddings_load_stage",
    ),
    "transcripts": RetrievalSource(
        key="transcripts",
        label="earnings transcript",
        root=PIPELINE_ROOT / "transcripts",
        data_table="aegis-earnings-transcripts-data",
        embeddings_table="aegis-earnings-transcripts-embeddings",
        data_staging_table="aegis_earnings_transcripts_data_load_stage",
        embeddings_staging_table="aegis_earnings_transcripts_embeddings_load_stage",
    ),
    "event_transcripts": RetrievalSource(
        key="event_transcripts",
        label="event transcript",
        root=PIPELINE_ROOT / "event_transcripts",
        data_table="aegis-event-transcripts-data",
        embeddings_table="aegis-event-transcripts-embeddings",
        data_staging_table="aegis_event_transcripts_data_load_stage",
        embeddings_staging_table="aegis_event_transcripts_embeddings_load_stage",
    ),
}


def source_keys() -> tuple[str, ...]:
    """Return supported source names in stable CLI order."""
    return tuple(SOURCES)


def select_source(argv: list[str]) -> RetrievalSource:
    """Pre-parse --source so source-specific defaults are available."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--source", choices=source_keys())
    namespace, _ = parser.parse_known_args(argv)
    if namespace.source:
        return SOURCES[namespace.source]
    if any(arg in {"-h", "--help"} for arg in argv):
        return SOURCES["investor_slides"]

    choices = ", ".join(source_keys())
    raise SystemExit(f"Choose --source ({choices}).")


def load_source_modules(source: RetrievalSource) -> SourceModules:
    """Import modules for the selected source pipeline."""
    if not source.root.is_dir():
        raise FileNotFoundError(f"Source directory not found: {source.root}")

    _clear_source_modules()
    _prepare_source_path(source)
    source_context = importlib.import_module("utils.source_context")
    source_context.set_active_source(source.key)
    return SourceModules(
        finalize=importlib.import_module("pipeline.finalize"),
        manifest=importlib.import_module("pipeline.manifest"),
        config_setup=importlib.import_module("utils.config_setup"),
    )


def _clear_source_modules() -> None:
    """Clear prior source imports before switching source packages."""
    for module_name in list(sys.modules):
        clear_exact = {"database", "pipeline", "utils", "connections"}
        clear_prefixes = ("database.", "pipeline.", "utils.", "connections.")
        if module_name in clear_exact or module_name.startswith(clear_prefixes):
            del sys.modules[module_name]


def _prepare_source_path(source: RetrievalSource) -> None:
    """Put the shared pipeline root and selected source root first on sys.path."""
    shared_path = str(AEGIS_PIPELINE_ROOT)
    while shared_path in sys.path:
        sys.path.remove(shared_path)
    for configured_source in SOURCES.values():
        source_path = str(configured_source.root)
        while source_path in sys.path:
            sys.path.remove(source_path)
    sys.path.insert(0, shared_path)
    sys.path.insert(0, str(source.root))
