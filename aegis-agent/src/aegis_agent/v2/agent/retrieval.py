"""Source adapters for quick V2 evidence retrieval."""

from __future__ import annotations

import asyncio
import importlib
import re
from dataclasses import dataclass, field
from typing import Any

from ..sources import SOURCE_TABLES, source_label
from .llm_context import build_llm_context
from .models import EvidenceChunk, NormalizedTurn


QUICK_SEARCH_CHUNK_LIMIT = 60
QUICK_SEARCH_MAX_COMBINATIONS = 12
QUICK_SEARCH_MAX_SOURCES = 3
QUICK_SEARCH_MAX_CHUNKS_PER_COMBO = 20


@dataclass(frozen=True)
class RetrievalResult:
    """Normalized retrieval output and non-fatal source gaps."""

    chunks: list[EvidenceChunk] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)


V1_PIPELINE_MODULES = {
    "rts": "aegis_agent.model.subagents.rts.pipeline",
    "pillar3": "aegis_agent.model.subagents.pillar3.pipeline",
    "supplementary_financials": "aegis_agent.model.subagents.supplementary_financials.pipeline",
    "investor_slides": "aegis_agent.model.subagents.investor_slides.pipeline",
    "transcripts": "aegis_agent.model.subagents.transcripts.pipeline",
    "event_transcripts": "aegis_agent.model.subagents.event_transcripts.pipeline",
}


def _terms(query: str) -> list[str]:
    """Extract simple searchable terms from the user query."""
    stop = {
        "a",
        "about",
        "and",
        "are",
        "bank",
        "banks",
        "can",
        "for",
        "from",
        "how",
        "in",
        "is",
        "me",
        "of",
        "on",
        "or",
        "show",
        "the",
        "to",
        "what",
        "with",
    }
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_%-]{2,}", query.lower())
    result: list[str] = []
    for word in words:
        if word in stop or word in result:
            continue
        result.append(word)
    return result[:10]


def _score_text(text: str, query_terms: list[str]) -> float:
    """Score one candidate chunk using deterministic term matches."""
    haystack = text.lower()
    if not query_terms:
        return 1.0
    score = 0.0
    for term in query_terms:
        if term in haystack:
            score += 1.0
    return score


def _record_score(record: dict[str, Any], fallback: float) -> float:
    """Return the fused V1 retrieval score when present."""
    for key in ("score", "raw_score"):
        try:
            return float(record[key])
        except (KeyError, TypeError, ValueError):
            continue
    return fallback


def _chunk_from_standard_record(
    source_id: str, record: dict[str, Any], query_terms: list[str]
) -> EvidenceChunk:
    """Normalize a row from the V1 document-style retrieval tables."""
    content = str(record.get("chunk_content") or "")
    chunk_id = str(
        record.get("chunk_id") or record.get("file_id") or f"{source_id}:{len(content)}"
    )
    page_number = record.get("page_number")
    if page_number is not None:
        try:
            page_number = int(page_number)
        except (TypeError, ValueError):
            page_number = None
    reference = {
        "source_name": source_id,
        "table": SOURCE_TABLES.get(source_id),
        "file_id": record.get("file_id"),
        "chunk_id": chunk_id,
        "page_number": page_number,
        "bank_ticker": record.get("bank"),
        "fiscal_year": record.get("fiscal_year"),
        "quarter": record.get("quarter"),
    }
    return EvidenceChunk(
        source_name=source_id,
        source_display_name=source_label(source_id),
        bank_ticker=str(record.get("bank") or "") or None,
        fiscal_year=(
            int(record["fiscal_year"])
            if record.get("fiscal_year") is not None
            else None
        ),
        quarter=str(record.get("quarter") or "") or None,
        file_id=str(record.get("file_id") or "") or None,
        file_name=str(record.get("filename") or "") or None,
        page_number=page_number,
        section_name=str(record.get("name") or "") or None,
        chunk_id=chunk_id,
        chunk_content=content,
        score=_record_score(
            record,
            _score_text(
                " ".join(
                    [
                        content,
                        str(record.get("filename") or ""),
                        str(record.get("name") or ""),
                    ]
                ),
                query_terms,
            ),
        ),
        reference_payload=reference,
    )


async def _research_context(turn: NormalizedTurn) -> dict[str, Any]:
    """Build strict LLM context for quick retrieval."""
    context = await build_llm_context(turn.run_uuid or "v2-quick-research", "quick search")
    _stamp_research_context(context, turn)
    return context


def _stamp_research_context(
    context: dict[str, Any],
    turn: NormalizedTurn,
    source_ids: list[str] | None = None,
) -> None:
    """Attach per-turn research settings to a shared LLM context."""
    context["source_filter"] = source_ids or turn.source_ids
    context["v2_model_plan"] = turn.model_plan


def _bank_period_combinations(turn: NormalizedTurn) -> list[dict[str, Any]]:
    """Build scoped bank-period combinations for the mature V1 retrieval path."""
    if not turn.bank_symbols or not turn.fiscal_years or not turn.quarters:
        raise RuntimeError(
            "Quick search requires selected bank, fiscal year, and quarter context."
        )
    return [
        {
            "bank_symbol": bank,
            "fiscal_year": year,
            "quarter": quarter,
        }
        for bank in turn.bank_symbols
        for year in turn.fiscal_years
        for quarter in turn.quarters
    ]


def _quick_source_ids(source_ids: list[str]) -> list[str]:
    """Return the bounded source set used by quick search."""
    return source_ids[:QUICK_SEARCH_MAX_SOURCES]


def _combo_budget(combo_count: int, limit: int) -> int:
    """Return the per-combo quick evidence budget."""
    return min(
        QUICK_SEARCH_MAX_CHUNKS_PER_COMBO,
        max(1, limit // max(combo_count, 1)),
    )


def _pipeline_module(source_id: str) -> Any:
    """Load the V1 pipeline module for a selected source."""
    module_name = V1_PIPELINE_MODULES.get(source_id)
    if not module_name:
        raise RuntimeError(
            f"{source_label(source_id)} is not supported for quick search."
        )
    return importlib.import_module(module_name)


async def _strict_prepare_query(
    module: Any,
    turn: NormalizedTurn,
    combinations: list[dict[str, Any]],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Run V1 query prep and embeddings without deterministic fallbacks."""
    prompt_input = f"{turn.content}\n\nLatest user message: {turn.content}".strip()
    parsed, usage = await module.call_tool_prompt(
        prompt_name="query_prep",
        replacements={
            "user_input": prompt_input,
            "research_scope": module.format_scope(combinations),
        },
        context=context,
        max_tokens=1200,
    )
    prepared = module.normalize_prepared_query(parsed, prompt_input)
    prepared["usage"] = usage

    inputs = [("rewritten", prepared["rewritten_query"])]
    inputs.extend(
        (f"sub_query_{index}", query)
        for index, query in enumerate(prepared["sub_queries"])
    )
    if prepared["keywords"]:
        inputs.append(("keywords", " ".join(prepared["keywords"])))
    if prepared["metrics"]:
        inputs.append(("metrics", " ".join(prepared["metrics"])))
    inputs.append(("hyde", prepared["hyde_answer"]))

    response = await module.embed_batch(
        input_texts=[value for _, value in inputs],
        context=context,
    )
    embeddings: dict[str, list[float]] = {}
    for (name, _), item in zip(inputs, response.get("data", [])):
        embedding = item.get("embedding", []) if isinstance(item, dict) else []
        if embedding:
            embeddings[name] = embedding
    if not embeddings.get("rewritten"):
        raise RuntimeError("Quick search query embedding failed.")
    prepared["embeddings"] = embeddings
    prepared["embedding_usage"] = response.get("metrics", {})
    return prepared


async def _gap_fill_chunks(
    module: Any,
    chunks: list[dict[str, Any]],
    *,
    search_semaphore: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Run the source-appropriate V1 gap fill step."""
    if hasattr(module, "gap_fill_one_page_gaps"):
        return await module.gap_fill_one_page_gaps(
            chunks, search_semaphore=search_semaphore
        )
    return await module.gap_fill_one_sheet_gaps(
        chunks, search_semaphore=search_semaphore
    )


async def _retrieve_source_combo_candidates(
    source_id: str,
    combo: dict[str, Any],
    *,
    prepared: dict[str, Any],
    context: dict[str, Any],
    search_top_k: int,
) -> list[dict[str, Any]]:
    """Run one source's V1 hybrid search for one bank-period combo."""
    module = _pipeline_module(source_id)
    search_semaphore = asyncio.Semaphore(module.MAX_PARALLEL_SEARCH_QUERIES)
    candidates = await module.multi_strategy_search(
        combo=combo,
        prepared=prepared,
        top_k=search_top_k,
        search_semaphore=search_semaphore,
    )
    anchors = candidates[:search_top_k]
    expanded = await _gap_fill_chunks(
        module, anchors, search_semaphore=search_semaphore
    )
    expanded = module.cap_gap_filled_chunks(expanded, anchors, search_top_k)
    chunks: list[dict[str, Any]] = []
    for chunk in expanded:
        normalized = dict(chunk)
        normalized["_quick_source_id"] = source_id
        chunks.append(normalized)
    return chunks


async def _retrieve_combo_candidates(
    source_ids: list[str],
    combo: dict[str, Any],
    *,
    prepared: dict[str, Any],
    context: dict[str, Any],
    search_top_k: int,
) -> list[dict[str, Any]]:
    """Retrieve the top quick candidates for one combo across all selected sources."""
    source_results = await asyncio.gather(
        *[
            _retrieve_source_combo_candidates(
                source_id,
                combo,
                prepared=prepared,
                context=context,
                search_top_k=search_top_k,
            )
            for source_id in source_ids
        ]
    )
    candidates = [candidate for rows in source_results for candidate in rows]
    return _cap_raw_candidates_by_combo(
        _rank_raw_candidates(candidates),
        per_combo_limit=search_top_k,
        total_limit=search_top_k,
    )


def _raw_source_id(candidate: dict[str, Any]) -> str:
    """Return the quick source id stamped onto a raw candidate."""
    return str(candidate.get("_quick_source_id") or "")


def _candidate_dedupe_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    """Return a source-aware raw candidate key."""
    return (
        _raw_source_id(candidate),
        str(candidate.get("file_id") or ""),
        str(candidate.get("chunk_id") or ""),
    )


def _combo_key_from_candidate(candidate: dict[str, Any]) -> tuple[str, str, str]:
    """Return the bank-period key for per-combo quick caps."""
    return (
        str(candidate.get("bank") or ""),
        str(candidate.get("fiscal_year") or ""),
        str(candidate.get("quarter") or "").upper(),
    )


def _rank_raw_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedupe and rank quick candidates using fused V1 scores."""
    deduped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in candidates:
        key = _candidate_dedupe_key(candidate)
        existing = deduped.get(key)
        if existing is None or _record_score(candidate, 0.0) > _record_score(
            existing, 0.0
        ):
            deduped[key] = candidate
    return sorted(
        deduped.values(),
        key=lambda candidate: (
            _record_score(candidate, 0.0),
            str(candidate.get("fiscal_year") or ""),
            str(candidate.get("quarter") or ""),
            _raw_source_id(candidate),
        ),
        reverse=True,
    )


def _cap_raw_candidates_by_combo(
    candidates: list[dict[str, Any]], *, per_combo_limit: int, total_limit: int
) -> list[dict[str, Any]]:
    """Apply per-combo and total quick evidence budgets."""
    selected: list[dict[str, Any]] = []
    combo_counts: dict[tuple[str, str, str], int] = {}
    for candidate in candidates:
        combo_key = _combo_key_from_candidate(candidate)
        if combo_counts.get(combo_key, 0) >= per_combo_limit:
            continue
        selected.append(candidate)
        combo_counts[combo_key] = combo_counts.get(combo_key, 0) + 1
        if len(selected) >= total_limit:
            break
    return selected


async def _strict_rerank_merged_candidates(
    module: Any,
    *,
    query: str,
    combinations: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run one metadata rerank pass after quick candidates are merged."""
    if not candidates:
        return []
    parsed, _usage = await module.call_tool_prompt(
        prompt_name="rerank",
        replacements={
            "user_input": query,
            "research_scope": module.format_scope(combinations),
            "candidates": module.format_rerank_candidates(candidates),
        },
        context=context,
        max_tokens=1200,
    )
    valid_remove = module.normalize_remove_indices(
        parsed.get("remove_indices", []), len(candidates)
    )
    valid_remove = module.apply_min_keep_floor(candidates, valid_remove)
    return [
        candidate
        for index, candidate in enumerate(candidates)
        if index not in valid_remove
    ]


async def retrieve_quick_evidence(
    turn: NormalizedTurn,
    limit: int = QUICK_SEARCH_CHUNK_LIMIT,
    llm_context: dict[str, Any] | None = None,
) -> RetrievalResult:
    """Retrieve and rank quick-search chunks using mature V1 retrieval primitives."""
    selected_sources = _quick_source_ids(turn.source_ids)
    if not selected_sources:
        raise RuntimeError("Quick search requires at least one selected data source.")
    combinations = _bank_period_combinations(turn)
    if len(combinations) > QUICK_SEARCH_MAX_COMBINATIONS:
        raise RuntimeError(
            "Quick search supports up to 12 bank-period combinations. "
            "Use fewer banks/periods or switch to deep search."
        )
    context = llm_context or await _research_context(turn)
    _stamp_research_context(context, turn, selected_sources)
    per_combo_limit = _combo_budget(len(combinations), limit)
    prep_module = _pipeline_module(selected_sources[0])
    prepared = await _strict_prepare_query(prep_module, turn, combinations, context)
    combo_results = await asyncio.gather(
        *[
            _retrieve_combo_candidates(
                selected_sources,
                combo,
                prepared=prepared,
                context=context,
                search_top_k=per_combo_limit,
            )
            for combo in combinations
        ]
    )
    all_candidates = [candidate for rows in combo_results for candidate in rows]

    merged = _cap_raw_candidates_by_combo(
        _rank_raw_candidates(all_candidates),
        per_combo_limit=per_combo_limit,
        total_limit=limit,
    )
    reranked = await _strict_rerank_merged_candidates(
        prep_module,
        query=prepared["rewritten_query"],
        combinations=combinations,
        candidates=merged,
        context=context,
    )
    capped = _cap_raw_candidates_by_combo(
        reranked,
        per_combo_limit=per_combo_limit,
        total_limit=limit,
    )
    query_terms = _terms(turn.content)
    return RetrievalResult(
        chunks=[
            _chunk_from_standard_record(_raw_source_id(candidate), candidate, query_terms)
            for candidate in capped
        ],
        gaps=[],
    )
