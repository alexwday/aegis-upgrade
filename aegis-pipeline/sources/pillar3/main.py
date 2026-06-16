"""Source pipeline entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent
PIPELINE_ROOT = SOURCE_ROOT.parents[1]
for path in (PIPELINE_ROOT, SOURCE_ROOT):
    path_value = str(path)
    if path_value not in sys.path:
        sys.path.insert(0, path_value)

from utils.source_context import set_active_source  # noqa: E402

set_active_source(SOURCE_ROOT.name)

from pipeline.chunking import run_chunking_stage  # noqa: E402
from pipeline.embeddings import run_embedding_stage  # noqa: E402
from pipeline.enrichment import run_enrichment_stage  # noqa: E402
from pipeline.extraction import run_extraction_stage  # noqa: E402
from pipeline.finalize import run_finalize_stage  # noqa: E402
from pipeline.manifest import run_manifest_stage  # noqa: E402
from utils.startup import run_startup  # noqa: E402


def main() -> int:
    """Run the full source artifact pipeline."""
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
