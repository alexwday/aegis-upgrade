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


QUICK_SEARCH_CHUNK_LIMIT = 80


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
    context["source_filter"] = turn.source_ids
    context["v2_model_plan"] = turn.model_plan
    return context


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


async def _strict_rerank_candidates(
    module: Any,
    *,
    query: str,
    combo: dict[str, Any],
    candidates: list[dict[str, Any]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run V1 metadata reranking without keep-all fallback."""
    if not candidates:
        return []
    parsed, _usage = await module.call_tool_prompt(
        prompt_name="rerank",
        replacements={
            "user_input": query,
            "research_scope": module.format_scope([combo]),
            "candidates": module.format_rerank_candidates(candidates),
        },
        context=context,
        max_tokens=800,
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


async def _retrieve_mature_source(
    source_id: str,
    turn: NormalizedTurn,
    *,
    combinations: list[dict[str, Any]],
    context: dict[str, Any],
    search_top_k: int,
) -> list[EvidenceChunk]:
    """Run V1 query prep, hybrid search, rerank, and gap-fill without research."""
    module = _pipeline_module(source_id)
    prepared = await _strict_prepare_query(module, turn, combinations, context)
    search_semaphore = asyncio.Semaphore(module.MAX_PARALLEL_SEARCH_QUERIES)
    query_terms = _terms(turn.content)
    chunks: list[EvidenceChunk] = []
    for combo in combinations:
        candidates = await module.multi_strategy_search(
            combo=combo,
            prepared=prepared,
            top_k=search_top_k,
            search_semaphore=search_semaphore,
        )
        rerank_pool = candidates[: module.RERANK_CANDIDATE_LIMIT]
        if len(candidates) > search_top_k:
            reranked = await _strict_rerank_candidates(
                module,
                query=prepared["rewritten_query"],
                combo=combo,
                candidates=rerank_pool,
                context=context,
            )
        else:
            reranked = candidates
        reranked = reranked[:search_top_k]
        expanded = await _gap_fill_chunks(
            module, reranked, search_semaphore=search_semaphore
        )
        expanded = module.cap_gap_filled_chunks(expanded, reranked, search_top_k)
        chunks.extend(
            _chunk_from_standard_record(source_id, chunk, query_terms)
            for chunk in expanded
        )
    return chunks


async def retrieve_quick_evidence(
    turn: NormalizedTurn,
    limit: int = QUICK_SEARCH_CHUNK_LIMIT,
    llm_context: dict[str, Any] | None = None,
) -> RetrievalResult:
    """Retrieve and rank quick-search chunks using mature V1 retrieval primitives."""
    selected_sources = turn.source_ids
    if not selected_sources:
        raise RuntimeError("Quick search requires at least one selected data source.")
    combinations = _bank_period_combinations(turn)
    context = llm_context or await _research_context(turn)
    context.setdefault("source_filter", turn.source_ids)
    context.setdefault("v2_model_plan", turn.model_plan)
    per_source_limit = max(
        8, min(limit, (limit // max(len(selected_sources) * len(combinations), 1)) + 8)
    )
    all_chunks: list[EvidenceChunk] = []
    for source_id in selected_sources:
        all_chunks.extend(
            await _retrieve_mature_source(
                source_id,
                turn,
                combinations=combinations,
                context=context,
                search_top_k=per_source_limit,
            )
        )

    ranked = sorted(
        all_chunks,
        key=lambda chunk: (
            float(chunk.score or 0),
            chunk.fiscal_year or 0,
            chunk.quarter or "",
            chunk.source_name,
        ),
        reverse=True,
    )
    return RetrievalResult(chunks=ranked[:limit], gaps=[])
