"""Database module entrypoint."""

from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from database.pipeline.chunking import run_chunking_stage
    from database.pipeline.embeddings import run_embedding_stage
    from database.pipeline.enrichment import run_enrichment_stage
    from database.pipeline.extraction import run_extraction_stage
    from database.pipeline.finalize import run_finalize_stage
    from database.pipeline.manifest import run_manifest_stage
    from database.utils.startup import run_startup
else:
    from .pipeline.chunking import run_chunking_stage
    from .pipeline.embeddings import run_embedding_stage
    from .pipeline.enrichment import run_enrichment_stage
    from .pipeline.extraction import run_extraction_stage
    from .pipeline.finalize import run_finalize_stage
    from .pipeline.manifest import run_manifest_stage
    from .utils.startup import run_startup


def main() -> int:
    """Run the full database artifact pipeline."""
    # Stage 1: startup checks.
    run_startup()

    # Stage 2: input/output manifest checks.
    run_manifest_stage()

    # Stage 3: XLSX extraction artifacts.
    run_extraction_stage()

    # Stage 4: token-counted sheet chunk artifacts.
    run_chunking_stage()

    # Stage 5: LLM enrichment artifacts.
    run_enrichment_stage()

    # Stage 6: final embedding artifacts.
    run_embedding_stage()

    # Stage 7: master outputs and progress cleanup.
    run_finalize_stage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
