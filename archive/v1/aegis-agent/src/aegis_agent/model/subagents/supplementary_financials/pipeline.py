"""Staged retrieval pipeline for supplementary financials."""

import asyncio
import json
import re
from time import perf_counter
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text

from ....connections.llm_connector import complete_with_tools, embed_batch
from ....connections.postgres_connector import get_connection
from ....utils.logging import get_logger
from ....utils.prompt_loader import load_prompt_from_db
from ....utils.settings import config

DATA_TABLE = 'public."aegis-financial-supp-data"'
EMBEDDINGS_TABLE = 'public."aegis-financial-supp-embeddings"'
SUPPLEMENTARY_FINANCIALS_MODEL_TIER = "small"

SEARCH_TOP_K = 20
BM25_TOP_K = 20
BM25_TERM_CAP = 10
CONTAINMENT_LIMIT = 50
RERANK_CANDIDATE_LIMIT = 30
RERANK_MIN_KEEP = 10
GAP_FILL_MAX_UNITS = 2
RESEARCH_MAX_ITERATIONS = 2
RESEARCH_CONFIDENCE_STOP_THRESHOLD = 0.8
RESEARCH_ADDITIONAL_SEARCH_TOP_K = 10
MAX_ADDITIONAL_QUERIES = 3
MAX_PARALLEL_COMBOS_PER_PERIOD = 6
MAX_PARALLEL_SEARCH_QUERIES = 18

SearchBatch = Tuple[str, List[Dict[str, Any]], bool, float]
SearchFactory = Callable[[], Awaitable[List[Dict[str, Any]]]]

FUSION_WEIGHTS = {
    "content_vector": 0.22,
    "hyde_vector": 0.18,
    "subquery_vector": 0.14,
    "keyword_vector": 0.10,
    "metric_vector": 0.12,
    "section_summary": 0.09,
    "bm25": 0.10,
    "keyword_array": 0.025,
    "metric_array": 0.025,
}

STOPWORDS = {
    "about",
    "across",
    "after",
    "also",
    "and",
    "are",
    "bank",
    "banks",
    "can",
    "compare",
    "did",
    "does",
    "for",
    "from",
    "give",
    "has",
    "have",
    "how",
    "into",
    "its",
    "latest",
    "me",
    "of",
    "on",
    "please",
    "q1",
    "q2",
    "q3",
    "q4",
    "show",
    "tell",
    "the",
    "their",
    "this",
    "through",
    "trend",
    "was",
    "were",
    "what",
    "with",
    "year",
}

METRIC_TERMS = (
    "adjusted net income",
    "allowance for credit losses",
    "assets under administration",
    "assets under management",
    "book value per share",
    "capital ratio",
    "cet1",
    "common equity tier 1",
    "diluted eps",
    "efficiency ratio",
    "expenses",
    "impaired loans",
    "leverage ratio",
    "net income",
    "net interest income",
    "net interest margin",
    "non-interest expense",
    "non-interest income",
    "provision for credit losses",
    "return on equity",
    "revenue",
    "risk-weighted assets",
    "roe",
    "rwa",
    "total assets",
    "total revenue",
)

PERCENT_RANGE_RE = re.compile(r"\b\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?%")
PERCENT_RE = re.compile(r"\b\d+(?:\.\d+)?%")
BASIS_POINTS_RE = re.compile(r"\b\d+(?:\.\d+)?\s*bps?\b", re.IGNORECASE)
CURRENCY_AMOUNT_RE = re.compile(
    r"\b(?:CAD|USD|C\$|\$)\s*\d[\d,]*(?:\.\d+)?" r"(?:\s*(?:million|billion|bn|mm))?\b",
    re.IGNORECASE,
)
SCALED_AMOUNT_RE = re.compile(
    r"\b\d[\d,]*(?:\.\d+)?\s*(?:million|billion|bn|mm)\b",
    re.IGNORECASE,
)
MULTISPACE_RE = re.compile(r"\s{2,}")
NON_ALNUM_RE = re.compile(r"[^a-z0-9]+", re.IGNORECASE)
SHEET_ID_RE = re.compile(r"sheet_(\d+)(?:\.(\d+))?", re.IGNORECASE)


async def run_retrieval_pipeline(
    query_text: str,
    latest_message: str,
    bank_period_combinations: List[Dict[str, Any]],
    context: Dict[str, Any],
    search_top_k: int = SEARCH_TOP_K,
) -> Dict[str, Any]:
    """Run query prep, hybrid search, rerank, gap fill, and research."""
    logger = get_logger()
    start_time = perf_counter()
    prepared = await prepare_query(
        query_text=query_text,
        latest_message=latest_message,
        bank_period_combinations=bank_period_combinations,
        context=context,
    )
    combo_results = []
    search_semaphore = asyncio.Semaphore(MAX_PARALLEL_SEARCH_QUERIES)

    for period_group in group_combinations_by_period(bank_period_combinations):
        for combo_batch in chunk_sequence(period_group, MAX_PARALLEL_COMBOS_PER_PERIOD):
            combo_results.extend(
                await asyncio.gather(
                    *[
                        process_combo_retrieval(
                            combo=combo,
                            prepared=prepared,
                            context=context,
                            search_top_k=search_top_k,
                            search_semaphore=search_semaphore,
                        )
                        for combo in combo_batch
                    ]
                )
            )

    chunks = []
    findings = []
    for combo_result in combo_results:
        chunks.extend(combo_result.get("expanded_chunks", []))
        findings.extend(combo_result.get("findings", []))

    logger.info(
        "subagent.supplementary_financials.pipeline_complete",
        execution_id=context.get("execution_id"),
        combo_count=len(bank_period_combinations),
        finding_count=len(findings),
        chunk_count=len(chunks),
        wall_time_seconds=round(perf_counter() - start_time, 3),
    )

    return {
        "query_text": query_text,
        "prepared_query": redact_embeddings(prepared),
        "combo_results": combo_results,
        "chunks": chunks,
        "findings": findings,
        "metrics": {
            "wall_time_seconds": round(perf_counter() - start_time, 3),
            "combo_count": len(combo_results),
            "chunk_count": len(chunks),
            "finding_count": len(findings),
        },
    }


async def process_combo_retrieval(
    combo: Dict[str, Any],
    prepared: Dict[str, Any],
    context: Dict[str, Any],
    search_top_k: int,
    search_semaphore: asyncio.Semaphore,
) -> Dict[str, Any]:
    """Run retrieval and research for one bank-period combination."""
    combo_start = perf_counter()
    candidates = await multi_strategy_search(
        combo=combo,
        prepared=prepared,
        top_k=search_top_k,
        search_semaphore=search_semaphore,
    )
    if not candidates:
        return {
            "combo": combo,
            "availability": None,
            "search_candidates": [],
            "reranked_chunks": [],
            "expanded_chunks": [],
            "findings": [],
            "research_iterations": [],
            "metrics": {
                "wall_time_seconds": round(perf_counter() - combo_start, 3),
                "skipped": "no_search_candidates",
            },
        }

    rerank_pool = candidates[:RERANK_CANDIDATE_LIMIT]
    if len(candidates) > search_top_k:
        reranked = await rerank_candidates(
            query=prepared["rewritten_query"],
            combo=combo,
            candidates=rerank_pool,
            context=context,
        )
    else:
        reranked = candidates
    reranked = reranked[:search_top_k]
    expanded = await gap_fill_one_sheet_gaps(reranked, search_semaphore=search_semaphore)
    expanded = cap_gap_filled_chunks(expanded, reranked, search_top_k)
    if expanded:
        research = await run_research_loop(
            prepared=prepared,
            combo=combo,
            initial_chunks=expanded,
            context=context,
            search_semaphore=search_semaphore,
        )
    else:
        research = {"chunks": [], "findings": [], "iterations": []}
    return {
        "combo": combo,
        "availability": None,
        "search_candidates": candidates,
        "reranked_chunks": reranked,
        "expanded_chunks": research["chunks"],
        "findings": research["findings"],
        "research_iterations": research["iterations"],
        "metrics": {
            "wall_time_seconds": round(perf_counter() - combo_start, 3),
            "search_candidates": len(candidates),
            "reranked_chunks": len(reranked),
            "expanded_chunks": len(research["chunks"]),
            "findings": len(research["findings"]),
            "research_iterations": len(research["iterations"]),
        },
    }


async def prepare_query(
    query_text: str,
    latest_message: str,
    bank_period_combinations: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> Dict[str, Any]:
    """Prepare the query facets and embeddings used by search."""
    logger = get_logger()
    scope_text = format_scope(bank_period_combinations)
    prompt_input = f"{query_text}\n\nLatest user message: {latest_message}".strip()

    try:
        parsed, usage = await call_tool_prompt(
            prompt_name="query_prep",
            replacements={
                "user_input": prompt_input,
                "research_scope": scope_text,
            },
            context=context,
            max_tokens=1200,
        )
        prepared = normalize_prepared_query(parsed, prompt_input)
        prepared["usage"] = usage
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "subagent.supplementary_financials.query_prep_fallback",
            execution_id=context.get("execution_id"),
            error=str(exc),
        )
        prepared = fallback_prepared_query(prompt_input)

    await embed_prepared_query(prepared, context)
    return prepared


def normalize_prepared_query(parsed: Dict[str, Any], original_query: str) -> Dict[str, Any]:
    """Validate and cap query-prep output."""
    deterministic_terms = extract_search_terms(original_query)
    metrics = merge_unique_terms(
        parsed.get("metrics", []),
        deterministic_terms["metrics"],
        max_items=8,
    )
    keywords = merge_unique_terms(
        parsed.get("keywords", []),
        deterministic_terms["keywords"],
        max_items=8,
    )
    return {
        "original_query": original_query,
        "rewritten_query": str(parsed.get("rewritten_query") or original_query).strip(),
        "sub_queries": limit_unique_texts(parsed.get("sub_queries", []), 3),
        "keywords": keywords,
        "metrics": metrics,
        "hyde_answer": soften_hyde_answer(str(parsed.get("hyde_answer") or original_query)),
        "embeddings": {},
        "usage": {},
    }


def fallback_prepared_query(query_text: str) -> Dict[str, Any]:
    """Build a deterministic query-prep fallback if the LLM is unavailable."""
    terms = extract_search_terms(query_text)
    return {
        "original_query": query_text,
        "rewritten_query": query_text,
        "sub_queries": [],
        "keywords": terms["keywords"][:8],
        "metrics": terms["metrics"][:8],
        "hyde_answer": soften_hyde_answer(
            "The source should disclose the requested financial metrics, period "
            "comparisons, and any relevant table labels for the selected banks."
        ),
        "embeddings": {},
        "usage": {},
    }


async def embed_prepared_query(prepared: Dict[str, Any], context: Dict[str, Any]) -> None:
    """Batch embed all prepared query facets."""
    logger = get_logger()
    inputs = [("rewritten", prepared["rewritten_query"])]
    inputs.extend(
        (f"sub_query_{index}", query) for index, query in enumerate(prepared["sub_queries"])
    )
    if prepared["keywords"]:
        inputs.append(("keywords", " ".join(prepared["keywords"])))
    if prepared["metrics"]:
        inputs.append(("metrics", " ".join(prepared["metrics"])))
    inputs.append(("hyde", prepared["hyde_answer"]))

    try:
        response = await embed_batch(
            input_texts=[value for _, value in inputs],
            context=context,
        )
        embeddings = {}
        for (name, _), item in zip(inputs, response.get("data", [])):
            embeddings[name] = item.get("embedding", [])
        prepared["embeddings"] = embeddings
        prepared["embedding_usage"] = response.get("metrics", {})
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "subagent.supplementary_financials.query_embedding_skipped",
            execution_id=context.get("execution_id"),
            error=str(exc),
        )
        prepared["embeddings"] = {}


async def multi_strategy_search(
    combo: Dict[str, Any],
    prepared: Dict[str, Any],
    top_k: int,
    search_semaphore: Optional[asyncio.Semaphore] = None,
) -> List[Dict[str, Any]]:
    """Run all vector, BM25, and metadata-match strategies and fuse scores."""
    search_semaphore = search_semaphore or asyncio.Semaphore(MAX_PARALLEL_SEARCH_QUERIES)
    search_factories: List[Tuple[str, SearchFactory, bool, float]] = []
    embeddings = prepared.get("embeddings", {})
    if embeddings.get("rewritten"):
        search_factories.append(
            (
                "content_vector",
                lambda vector=embeddings["rewritten"]: search_embedding_type(
                    combo, vector, "content", top_k
                ),
                True,
                1.0,
            )
        )
        search_factories.append(
            (
                "section_summary",
                lambda vector=embeddings["rewritten"]: search_section_summary(combo, vector, top_k),
                True,
                1.0,
            )
        )
    if embeddings.get("hyde"):
        search_factories.append(
            (
                "hyde_vector",
                lambda vector=embeddings["hyde"]: search_embedding_type(
                    combo, vector, "content", top_k
                ),
                True,
                1.0,
            )
        )
    sub_queries = prepared.get("sub_queries", [])
    subquery_scale = 1.0 / len(sub_queries) if sub_queries else 1.0
    for index, _query in enumerate(sub_queries):
        vector = embeddings.get(f"sub_query_{index}")
        if vector:
            search_factories.append(
                (
                    "subquery_vector",
                    lambda vector=vector: search_embedding_type(combo, vector, "content", top_k),
                    True,
                    subquery_scale,
                )
            )
    if embeddings.get("keywords"):
        search_factories.append(
            (
                "keyword_vector",
                lambda vector=embeddings["keywords"]: search_embedding_type(
                    combo, vector, "keyword", top_k
                ),
                True,
                1.0,
            )
        )
    if embeddings.get("metrics"):
        search_factories.append(
            (
                "metric_vector",
                lambda vector=embeddings["metrics"]: search_embedding_type(
                    combo, vector, "metric", top_k
                ),
                True,
                1.0,
            )
        )

    bm25_query = build_bm25_query(prepared)
    if bm25_query:
        search_factories.append(
            ("bm25", lambda query=bm25_query: bm25_search(combo, query, BM25_TOP_K), False, 1.0)
        )
    if prepared.get("keywords"):
        search_factories.append(
            (
                "keyword_array",
                lambda terms=prepared["keywords"]: jsonb_containment_search(
                    combo, "keywords", terms, CONTAINMENT_LIMIT
                ),
                False,
                1.0,
            )
        )
    if prepared.get("metrics"):
        search_factories.append(
            (
                "metric_array",
                lambda terms=prepared["metrics"]: jsonb_containment_search(
                    combo, "metrics", terms, CONTAINMENT_LIMIT
                ),
                False,
                1.0,
            )
        )

    batches = await asyncio.gather(
        *[
            run_search_batch(strategy_name, factory, invert, scale, search_semaphore)
            for strategy_name, factory, invert, scale in search_factories
        ]
    )
    return fuse_strategy_batches(batches)


async def run_search_batch(
    strategy_name: str,
    search_factory: SearchFactory,
    invert: bool,
    scale: float,
    search_semaphore: asyncio.Semaphore,
) -> SearchBatch:
    """Run one search strategy behind the shared retrieval concurrency limit."""
    async with search_semaphore:
        hits = await search_factory()
    return strategy_name, hits, invert, scale


async def search_embedding_type(
    combo: Dict[str, Any],
    embedding_vector: List[float],
    embedding_type: str,
    top_k: int,
) -> List[Dict[str, Any]]:
    """Search content-unit embeddings and return source chunks."""
    if not embedding_vector:
        return []
    query = text(
        f"""
        SELECT
            d.source_type,
            d.fiscal_year,
            d.quarter,
            d.bank,
            d.filename,
            d.file_id,
            d.file_type,
            d.file_path,
            d.page_number,
            d.name,
            d.summary,
            d.chunk_id,
            d.chunk_content,
            d.keywords,
            d.metrics,
            e.embedding <=> CAST(:embedding AS vector) AS raw_score
        FROM {EMBEDDINGS_TABLE} e
        JOIN {DATA_TABLE} d
          ON d.file_id = e.file_id
         AND d.chunk_id = COALESCE(e.chunk_id, e.content_unit_id)
        WHERE d.bank = :bank_symbol
          AND d.fiscal_year = :fiscal_year
          AND d.quarter = :quarter
          AND e.bank = :bank_symbol
          AND e.fiscal_year = :fiscal_year
          AND e.quarter = :quarter
          AND e.embedding_type = :embedding_type
          AND e.embedding IS NOT NULL
        ORDER BY e.embedding <=> CAST(:embedding AS vector)
        LIMIT :top_k
        """
    )
    params = combo_params(combo)
    params.update(
        {
            "embedding": format_vector(embedding_vector),
            "embedding_type": embedding_type,
            "top_k": top_k,
        }
    )
    async with get_connection() as conn:
        result = await conn.execute(query, params)
        return [row_to_candidate(row, float(row.raw_score or 0.0)) for row in result]


async def search_section_summary(
    combo: Dict[str, Any],
    embedding_vector: List[float],
    top_k: int,
) -> List[Dict[str, Any]]:
    """Search section-summary embeddings and map section hits to chunks."""
    if not embedding_vector:
        return []
    query = text(
        f"""
        SELECT
            d.source_type,
            d.fiscal_year,
            d.quarter,
            d.bank,
            d.filename,
            d.file_id,
            d.file_type,
            d.file_path,
            d.page_number,
            d.name,
            d.summary,
            d.chunk_id,
            d.chunk_content,
            d.keywords,
            d.metrics,
            e.embedding <=> CAST(:embedding AS vector) AS raw_score
        FROM {EMBEDDINGS_TABLE} e
        JOIN LATERAL jsonb_array_elements_text(e.content_unit_ids) ids(content_unit_id)
          ON TRUE
        JOIN {DATA_TABLE} d
          ON d.file_id = e.file_id
         AND d.chunk_id = ids.content_unit_id
        WHERE d.bank = :bank_symbol
          AND d.fiscal_year = :fiscal_year
          AND d.quarter = :quarter
          AND e.bank = :bank_symbol
          AND e.fiscal_year = :fiscal_year
          AND e.quarter = :quarter
          AND e.embedding_type = 'section_summary'
          AND e.embedding IS NOT NULL
        ORDER BY e.embedding <=> CAST(:embedding AS vector)
        LIMIT :top_k
        """
    )
    params = combo_params(combo)
    params.update({"embedding": format_vector(embedding_vector), "top_k": top_k})
    async with get_connection() as conn:
        result = await conn.execute(query, params)
        return [row_to_candidate(row, float(row.raw_score or 0.0)) for row in result]


async def bm25_search(
    combo: Dict[str, Any],
    query_text: str,
    top_k: int,
) -> List[Dict[str, Any]]:
    """Run PostgreSQL full-text search against chunk metadata and content."""
    clean_query = sanitize_search_query(query_text)
    if not clean_query:
        return []
    search_vector = """
        setweight(to_tsvector('english', coalesce(d.name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(d.summary, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(d.keywords::text, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(d.metrics::text, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(d.chunk_content, '')), 'C')
    """
    query = text(
        f"""
        WITH q AS (
            SELECT websearch_to_tsquery('english', :query_text) AS tsq
        )
        SELECT
            d.source_type,
            d.fiscal_year,
            d.quarter,
            d.bank,
            d.filename,
            d.file_id,
            d.file_type,
            d.file_path,
            d.page_number,
            d.name,
            d.summary,
            d.chunk_id,
            d.chunk_content,
            d.keywords,
            d.metrics,
            ts_rank_cd(({search_vector}), q.tsq) AS raw_score
        FROM {DATA_TABLE} d, q
        WHERE d.bank = :bank_symbol
          AND d.fiscal_year = :fiscal_year
          AND d.quarter = :quarter
          AND q.tsq @@ ({search_vector})
        ORDER BY raw_score DESC
        LIMIT :top_k
        """
    )
    params = combo_params(combo)
    params.update({"query_text": clean_query, "top_k": top_k})
    async with get_connection() as conn:
        result = await conn.execute(query, params)
        return [row_to_candidate(row, float(row.raw_score or 0.0)) for row in result]


async def jsonb_containment_search(
    combo: Dict[str, Any],
    column_name: str,
    terms: List[str],
    limit: int,
) -> List[Dict[str, Any]]:
    """Search keyword or metric JSONB arrays by case-insensitive containment."""
    safe_terms = limit_unique_texts(terms, BM25_TERM_CAP)
    if not safe_terms:
        return []
    clauses = []
    params = combo_params(combo)
    for index, term_value in enumerate(safe_terms):
        key = f"term_{index}"
        clauses.append(f"COALESCE(d.{column_name}::text, '') ILIKE :{key}")
        params[key] = f"%{term_value}%"
    params["limit"] = limit
    where_terms = " OR ".join(clauses)
    query = text(
        f"""
        SELECT
            d.source_type,
            d.fiscal_year,
            d.quarter,
            d.bank,
            d.filename,
            d.file_id,
            d.file_type,
            d.file_path,
            d.page_number,
            d.name,
            d.summary,
            d.chunk_id,
            d.chunk_content,
            d.keywords,
            d.metrics,
            1.0 AS raw_score
        FROM {DATA_TABLE} d
        WHERE d.bank = :bank_symbol
          AND d.fiscal_year = :fiscal_year
          AND d.quarter = :quarter
          AND ({where_terms})
        LIMIT :limit
        """
    )
    async with get_connection() as conn:
        result = await conn.execute(query, params)
        candidates = []
        for row in result:
            candidate = row_to_candidate(row, 0.0)
            haystack = json.dumps(candidate.get(column_name, []), default=str).casefold()
            candidate["raw_score"] = float(
                sum(1 for term_value in safe_terms if term_value.casefold() in haystack)
            )
            candidates.append(candidate)
        candidates.sort(key=lambda item: item["raw_score"], reverse=True)
        return candidates


def fuse_strategy_batches(
    batches: List[Tuple[str, List[Dict[str, Any]], bool, float]],
) -> List[Dict[str, Any]]:
    """Normalize and fuse candidate scores across search strategies."""
    raw_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    combined: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for strategy_name, hits, invert, scale in batches:
        normalized = normalize_strategy_scores(hits, invert=invert)
        for hit in hits:
            key = candidate_key(hit)
            raw_by_key.setdefault(key, hit)
            if key not in combined:
                combined[key] = {
                    "score": 0.0,
                    "strategy_scores": {},
                    "match_sources": [],
                }
            strategy_score = normalized.get(key, 0.0)
            weighted_score = FUSION_WEIGHTS.get(strategy_name, 0.0) * scale * strategy_score
            combined[key]["score"] += weighted_score
            current_strategy_score = combined[key]["strategy_scores"].get(strategy_name, 0.0)
            combined[key]["strategy_scores"][strategy_name] = max(
                current_strategy_score, strategy_score
            )
            if strategy_name not in combined[key]["match_sources"]:
                combined[key]["match_sources"].append(strategy_name)

    fused = []
    for key, scoring in combined.items():
        candidate = dict(raw_by_key[key])
        candidate["score"] = scoring["score"]
        candidate["strategy_scores"] = scoring["strategy_scores"]
        candidate["match_sources"] = scoring["match_sources"]
        candidate["is_gap_fill"] = False
        fused.append(candidate)
    fused.sort(key=lambda item: item["score"], reverse=True)
    return fused


def normalize_strategy_scores(
    hits: List[Dict[str, Any]],
    invert: bool,
) -> Dict[Tuple[str, str], float]:
    """Normalize one strategy's raw scores to 0.0-1.0 by chunk key."""
    if not hits:
        return {}
    best_raw: Dict[Tuple[str, str], float] = {}
    for hit in hits:
        key = candidate_key(hit)
        raw_score = float(hit.get("raw_score", 0.0))
        if key not in best_raw:
            best_raw[key] = raw_score
            continue
        if invert:
            best_raw[key] = min(best_raw[key], raw_score)
        else:
            best_raw[key] = max(best_raw[key], raw_score)

    max_value = max(best_raw.values()) if best_raw else 0.0
    if max_value == 0:
        return {key: 1.0 for key in best_raw}
    if invert:
        return {key: max(0.0, 1.0 - value / max_value) for key, value in best_raw.items()}
    return {key: max(0.0, value / max_value) for key, value in best_raw.items()}


async def rerank_candidates(
    query: str,
    combo: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    context: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Filter clearly irrelevant chunks using metadata-only LLM reranking."""
    logger = get_logger()
    if not candidates:
        return []
    try:
        parsed, _usage = await call_tool_prompt(
            prompt_name="rerank",
            replacements={
                "user_input": query,
                "research_scope": format_scope([combo]),
                "candidates": format_rerank_candidates(candidates),
            },
            context=context,
            max_tokens=800,
        )
        valid_remove = normalize_remove_indices(parsed.get("remove_indices", []), len(candidates))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "subagent.supplementary_financials.rerank_keep_all",
            execution_id=context.get("execution_id"),
            candidate_count=len(candidates),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return candidates

    valid_remove = apply_min_keep_floor(candidates, valid_remove)
    logger.info(
        "subagent.supplementary_financials.rerank_complete",
        execution_id=context.get("execution_id"),
        candidate_count=len(candidates),
        removed_count=len(valid_remove),
        kept_count=len(candidates) - len(valid_remove),
    )
    return [candidate for index, candidate in enumerate(candidates) if index not in valid_remove]


async def gap_fill_one_sheet_gaps(
    chunks: List[Dict[str, Any]],
    search_semaphore: Optional[asyncio.Semaphore] = None,
) -> List[Dict[str, Any]]:
    """Fill up to two missing sheets between retrieved chunks from the same file."""
    if not chunks:
        return []
    search_semaphore = search_semaphore or asyncio.Semaphore(MAX_PARALLEL_SEARCH_QUERIES)
    expanded: Dict[Tuple[str, str], Dict[str, Any]] = {
        candidate_key(chunk): chunk for chunk in chunks
    }
    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for chunk in chunks:
        by_file.setdefault(str(chunk.get("file_id", "")), []).append(chunk)

    gap_specs = []
    seen_gap_specs = set()
    for file_id, file_chunks in by_file.items():
        sheet_numbers = sorted(
            {
                sheet_number
                for sheet_number in (
                    parse_sheet_number(chunk.get("chunk_id", "")) for chunk in file_chunks
                )
                if sheet_number is not None
            }
        )
        for left, right in zip(sheet_numbers, sheet_numbers[1:]):
            missing_count = right - left - 1
            if missing_count <= 0 or missing_count > GAP_FILL_MAX_UNITS:
                continue
            for gap_sheet in range(left + 1, right):
                gap_spec = (file_id, gap_sheet)
                if gap_spec in seen_gap_specs:
                    continue
                seen_gap_specs.add(gap_spec)
                gap_specs.append(gap_spec)

    gap_results = await asyncio.gather(
        *[
            load_sheet_chunks_limited(file_id, gap_sheet, search_semaphore)
            for file_id, gap_sheet in gap_specs
        ]
    )
    for gap_chunks in gap_results:
        for gap_chunk in gap_chunks:
            key = candidate_key(gap_chunk)
            if key in expanded:
                continue
            gap_chunk["score"] = 0.0
            gap_chunk["strategy_scores"] = {}
            gap_chunk["match_sources"] = ["gap_fill"]
            gap_chunk["is_gap_fill"] = True
            expanded[key] = gap_chunk

    result = list(expanded.values())
    result.sort(key=chunk_sort_key)
    return result


async def load_sheet_chunks_limited(
    file_id: str,
    sheet_number: int,
    search_semaphore: asyncio.Semaphore,
) -> List[Dict[str, Any]]:
    """Load sheet chunks behind the shared retrieval concurrency limit."""
    async with search_semaphore:
        return await load_sheet_chunks(file_id, sheet_number)


async def load_sheet_chunks(file_id: str, sheet_number: int) -> List[Dict[str, Any]]:
    """Load every chunk for one spreadsheet sheet number."""
    query = text(
        f"""
        SELECT
            d.source_type,
            d.fiscal_year,
            d.quarter,
            d.bank,
            d.filename,
            d.file_id,
            d.file_type,
            d.file_path,
            d.page_number,
            d.name,
            d.summary,
            d.chunk_id,
            d.chunk_content,
            d.keywords,
            d.metrics,
            0.0 AS raw_score
        FROM {DATA_TABLE} d
        WHERE d.file_id = :file_id
          AND d.chunk_id LIKE :chunk_prefix
        ORDER BY d.page_number, d.chunk_id
        """
    )
    async with get_connection() as conn:
        result = await conn.execute(
            query,
            {
                "file_id": file_id,
                "chunk_prefix": f"sheet_{sheet_number}.%",
            },
        )
        return [row_to_candidate(row, 0.0) for row in result]


async def run_research_loop(
    prepared: Dict[str, Any],
    combo: Dict[str, Any],
    initial_chunks: List[Dict[str, Any]],
    context: Dict[str, Any],
    search_semaphore: Optional[asyncio.Semaphore] = None,
) -> Dict[str, Any]:
    """Run iterative research extraction with follow-up vector searches."""
    logger = get_logger()
    search_semaphore = search_semaphore or asyncio.Semaphore(MAX_PARALLEL_SEARCH_QUERIES)
    chunks = list(initial_chunks)
    seen_ids = {chunk["chunk_id"] for chunk in chunks}
    iterations = []
    previous_queries: List[str] = []
    stopping_reason = "max_iterations"

    for iteration_number in range(1, RESEARCH_MAX_ITERATIONS + 1):
        try:
            iteration = await call_research_iteration(
                prepared=prepared,
                combo=combo,
                chunks=chunks,
                previous_iterations=iterations,
                context=context,
                iteration_number=iteration_number,
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "subagent.supplementary_financials.research_iteration_failed",
                execution_id=context.get("execution_id"),
                iteration=iteration_number,
                error=str(exc),
            )
            stopping_reason = "research_llm_error"
            break

        iterations.append(iteration)
        additional_queries = limit_unique_texts(
            iteration.get("additional_queries", []),
            MAX_ADDITIONAL_QUERIES,
        )
        if float(iteration.get("confidence", 0.0) or 0.0) >= RESEARCH_CONFIDENCE_STOP_THRESHOLD:
            stopping_reason = "high_confidence"
            break
        if not additional_queries:
            stopping_reason = "no_additional_queries"
            break
        if queries_are_repeats(previous_queries, additional_queries):
            stopping_reason = "repeated_additional_queries"
            break
        previous_queries.extend(additional_queries)

        new_chunks = await search_additional_queries(
            combo=combo,
            queries=additional_queries,
            context=context,
            seen_ids=seen_ids,
            search_semaphore=search_semaphore,
        )
        if not new_chunks:
            stopping_reason = "no_new_chunks"
            break
        chunks.extend(new_chunks)
        chunks = await gap_fill_one_sheet_gaps(chunks, search_semaphore=search_semaphore)
        seen_ids = {chunk["chunk_id"] for chunk in chunks}

    return {
        "iterations": iterations,
        "findings": combine_findings(iterations),
        "chunks": chunks,
        "stopping_reason": stopping_reason,
    }


async def call_research_iteration(
    prepared: Dict[str, Any],
    combo: Dict[str, Any],
    chunks: List[Dict[str, Any]],
    previous_iterations: List[Dict[str, Any]],
    context: Dict[str, Any],
    iteration_number: int,
) -> Dict[str, Any]:
    """Call the research prompt and parse one structured iteration."""
    source_catalog = build_source_catalog(chunks)
    replacements = {
        "query": prepared["original_query"],
        "source_label": "Supplementary financials package",
        "bank": combo_bank_label(combo),
        "period": combo_period_label(combo),
        "previous_research": format_previous_research(previous_iterations),
        "chunks": format_evidence_chunks(chunks, source_catalog),
    }
    parsed, usage = await call_tool_prompt(
        prompt_name="research",
        replacements=replacements,
        context=context,
        max_tokens=5000,
    )
    try:
        findings = parse_research_findings(parsed.get("findings", []), source_catalog)
        additional_queries = parsed.get("additional_queries", [])
        confidence = parsed.get("confidence", 0.0)
        if not isinstance(additional_queries, list):
            raise ValueError("additional_queries must be a list")
        if not isinstance(confidence, (int, float)):
            raise ValueError("confidence must be a number")
    except ValueError:
        retry_replacements = dict(replacements)
        retry_replacements["chunks"] = (
            replacements["chunks"] + "\n\nCorrection: Return structured findings using the tool. "
            "Each finding needs finding, page, location_detail, and source_ref_ids. "
            "Do not return an empty findings array if the chunks contain relevant information."
        )
        parsed, usage = await call_tool_prompt(
            prompt_name="research",
            replacements=retry_replacements,
            context=context,
            max_tokens=5000,
        )
        findings = parse_research_findings(parsed.get("findings", []), source_catalog)
        additional_queries = parsed.get("additional_queries", [])
        confidence = parsed.get("confidence", 0.0)

    return {
        "iteration": iteration_number,
        "findings": findings,
        "additional_queries": limit_unique_texts(additional_queries, MAX_ADDITIONAL_QUERIES),
        "confidence": float(confidence),
        "usage": usage,
    }


async def search_additional_queries(
    combo: Dict[str, Any],
    queries: List[str],
    context: Dict[str, Any],
    seen_ids: set[str],
    search_semaphore: Optional[asyncio.Semaphore] = None,
) -> List[Dict[str, Any]]:
    """Embed additional research queries and run focused content-vector search."""
    if not queries:
        return []
    search_semaphore = search_semaphore or asyncio.Semaphore(MAX_PARALLEL_SEARCH_QUERIES)
    logger = get_logger()
    try:
        response = await embed_batch(input_texts=queries, context=context)
        vectors = [item.get("embedding", []) for item in response.get("data", [])]
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "subagent.supplementary_financials.additional_embedding_skipped",
            execution_id=context.get("execution_id"),
            error=str(exc),
        )
        return []

    new_chunks: Dict[Tuple[str, str], Dict[str, Any]] = {}
    search_results = await asyncio.gather(
        *[search_additional_vector(combo, vector, search_semaphore) for vector in vectors if vector]
    )
    for hits in search_results:
        for hit in hits:
            if hit["chunk_id"] in seen_ids:
                continue
            key = candidate_key(hit)
            if key in new_chunks:
                continue
            hit["score"] = max(0.0, 1.0 - float(hit.get("raw_score", 1.0)))
            hit["strategy_scores"] = {"additional_content_vector": hit["score"]}
            hit["match_sources"] = ["additional_content_vector"]
            hit["is_gap_fill"] = False
            new_chunks[key] = hit
    result = list(new_chunks.values())
    result.sort(key=chunk_sort_key)
    return result


async def search_additional_vector(
    combo: Dict[str, Any],
    vector: List[float],
    search_semaphore: asyncio.Semaphore,
) -> List[Dict[str, Any]]:
    """Run one additional-query vector search behind the shared concurrency limit."""
    async with search_semaphore:
        return await search_embedding_type(
            combo=combo,
            embedding_vector=vector,
            embedding_type="content",
            top_k=RESEARCH_ADDITIONAL_SEARCH_TOP_K,
        )


async def call_tool_prompt(
    prompt_name: str,
    replacements: Dict[str, str],
    context: Dict[str, Any],
    max_tokens: int,
) -> Tuple[Dict[str, Any], Dict[str, int]]:
    """Load a stage prompt, call the LLM, and return tool arguments."""
    prompt = load_stage_prompt(prompt_name, execution_id=context.get("execution_id"))
    user_text = prompt["user_prompt"]
    for key, value in replacements.items():
        user_text = user_text.replace("{" + key + "}", value)
    messages = []
    if prompt.get("system_prompt"):
        messages.append({"role": "system", "content": prompt["system_prompt"]})
    messages.append({"role": "user", "content": user_text})
    response = await complete_with_tools(
        messages=messages,
        tools=prompt["tools"],
        context=context,
        llm_params={
            "model": getattr(config.llm, SUPPLEMENTARY_FINANCIALS_MODEL_TIER).model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "tool_choice": resolve_tool_choice(prompt),
        },
    )
    return extract_tool_arguments(response), response_usage(response)


def resolve_tool_choice(prompt: Dict[str, Any]) -> Any:
    """Resolve prompt tool choice, forcing the named function when unambiguous."""
    tool_choice = prompt.get("tool_choice", "required")
    tools = prompt.get("tools") or []
    if tool_choice == "required" and len(tools) == 1:
        function_name = tools[0].get("function", {}).get("name")
        if function_name:
            return {"type": "function", "function": {"name": function_name}}
    return tool_choice


def load_stage_prompt(prompt_name: str, execution_id: Optional[str] = None) -> Dict[str, Any]:
    """Load a stage prompt from PostgreSQL."""
    prompt_data = load_prompt_from_db(
        layer="supplementary_financials",
        name=prompt_name,
        compose_with_globals=False,
        execution_id=execution_id,
    )
    return normalize_db_stage_prompt(prompt_data, prompt_name)


def normalize_db_stage_prompt(prompt_data: Dict[str, Any], prompt_name: str) -> Dict[str, Any]:
    """Normalize a prompts-table row into the local stage prompt schema."""
    if not isinstance(prompt_data, dict):
        raise ValueError(f"Prompt {prompt_name} was not found in the prompts table")
    system_prompt = prompt_data.get("system_prompt")
    user_prompt = prompt_data.get("user_prompt")
    tool_definition = prompt_data.get("tool_definition") or prompt_data.get("tool_definitions")
    if isinstance(tool_definition, dict):
        tools = [tool_definition]
    else:
        tools = tool_definition
    if not system_prompt or not user_prompt or not tools:
        raise ValueError(f"Prompt {prompt_name} is missing required prompt fields")
    return {
        "stage": prompt_name,
        "version": str(prompt_data.get("version") or "1.0"),
        "description": prompt_data.get("description"),
        "system_prompt": str(system_prompt),
        "user_prompt": str(user_prompt),
        "tool_choice": "required" if tools else "none",
        "tools": tools,
    }


def extract_tool_arguments(response: Dict[str, Any]) -> Dict[str, Any]:
    """Extract JSON function-call arguments from an OpenAI chat response."""
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("LLM response did not include choices")
    message = choices[0].get("message", {})
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        raw_arguments = tool_calls[0].get("function", {}).get("arguments", "{}")
        return parse_tool_arguments(raw_arguments)
    content = message.get("content")
    if content:
        return parse_tool_arguments(content)
    raise ValueError("LLM response did not include a tool call or JSON content")


def parse_tool_arguments(raw_arguments: Any) -> Dict[str, Any]:
    """Parse tool arguments from a function call or JSON text response."""
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, list):
        parts = []
        for part in raw_arguments:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("content") or ""))
            elif part:
                parts.append(str(part))
        raw_arguments = "\n".join(parts)
    if not isinstance(raw_arguments, str):
        raise ValueError("LLM tool arguments must be JSON object text")
    parsed = json.loads(extract_json_object_text(raw_arguments))
    if not isinstance(parsed, dict):
        raise ValueError("LLM tool arguments must decode to an object")
    return parsed


def extract_json_object_text(text_value: str) -> str:
    """Return the JSON object substring from plain text or fenced JSON."""
    stripped = text_value.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def response_usage(response: Dict[str, Any]) -> Dict[str, int]:
    """Return compact token usage metrics from an LLM response."""
    usage = response.get("usage") or {}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
    }


def row_to_candidate(row: Any, raw_score: float) -> Dict[str, Any]:
    """Convert a SQLAlchemy row into a retrieval candidate."""
    return {
        "source_type": row.source_type,
        "fiscal_year": row.fiscal_year,
        "quarter": row.quarter,
        "bank": row.bank,
        "filename": row.filename,
        "file_id": row.file_id,
        "file_type": row.file_type,
        "file_path": row.file_path,
        "page_number": int(row.page_number or 0),
        "name": row.name,
        "summary": row.summary,
        "chunk_id": row.chunk_id,
        "chunk_content": row.chunk_content or "",
        "keywords": coerce_list(row.keywords),
        "metrics": coerce_list(row.metrics),
        "raw_score": raw_score,
    }


def coerce_list(value: Any) -> List[str]:
    """Coerce JSON/list/scalar values into a string list."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if item is not None]
        except json.JSONDecodeError:
            pass
        return [value]
    return [str(value)]


def format_vector(embedding: Sequence[float]) -> str:
    """Serialize an embedding vector as a pgvector literal."""
    return "[" + ",".join(str(value) for value in embedding) + "]"


def combo_params(combo: Dict[str, Any]) -> Dict[str, Any]:
    """Return normalized SQL parameters for a bank-period combination."""
    fiscal_year = str(combo.get("fiscal_year", "")).strip()
    fiscal_year = re.sub(r"^FY", "", fiscal_year, flags=re.IGNORECASE)
    bank_symbol = combo.get("bank_symbol") or combo.get("bank") or combo.get("ticker") or ""
    return {
        "bank_symbol": normalize_supplementary_bank_symbol(bank_symbol),
        "fiscal_year": fiscal_year,
        "quarter": str(combo.get("quarter", "")).strip().upper(),
    }


def normalize_supplementary_bank_symbol(bank_symbol: Any) -> str:
    """Return the bank symbol exactly as stored in supplementary metadata."""
    return str(bank_symbol or "").strip().upper()


def combo_bank_label(combo: Dict[str, Any]) -> str:
    """Return a readable bank label for prompts."""
    bank_name = str(combo.get("bank_name") or "").strip()
    bank_symbol = str(combo.get("bank_symbol") or "").strip()
    if bank_name and bank_symbol:
        return f"{bank_name} ({bank_symbol})"
    return bank_name or bank_symbol or "selected bank"


def combo_period_label(combo: Dict[str, Any]) -> str:
    """Return a readable period label for prompts."""
    quarter = str(combo.get("quarter") or "").strip().upper()
    fiscal_year = str(combo.get("fiscal_year") or "").strip()
    return f"{quarter} {fiscal_year}".strip()


def group_combinations_by_period(
    bank_period_combinations: List[Dict[str, Any]],
) -> List[List[Dict[str, Any]]]:
    """Group combos by period while preserving first-seen period and combo order."""
    period_order = []
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for combo in bank_period_combinations:
        params = combo_params(combo)
        key = (params["fiscal_year"], params["quarter"])
        if key not in grouped:
            grouped[key] = []
            period_order.append(key)
        grouped[key].append(combo)
    return [grouped[key] for key in period_order]


def chunk_sequence(values: Sequence[Any], chunk_size: int) -> List[List[Any]]:
    """Split a sequence into ordered chunks of at most chunk_size."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    chunks = []
    for index in range(0, len(values), chunk_size):
        stop_index = min(index + chunk_size, len(values))
        chunks.append([values[item_index] for item_index in range(index, stop_index)])
    return chunks


def format_scope(bank_period_combinations: List[Dict[str, Any]]) -> str:
    """Format bank-period combinations for query-prep prompts."""
    lines = []
    for combo in bank_period_combinations:
        lines.append(f"- {combo_bank_label(combo)} / {combo_period_label(combo)}")
    return "\n".join(lines) if lines else "- No explicit bank-period scope provided"


def candidate_key(candidate: Dict[str, Any]) -> Tuple[str, str]:
    """Return the unique key for a source chunk."""
    return (str(candidate.get("file_id", "")), str(candidate.get("chunk_id", "")))


def parse_sheet_number(chunk_id: str) -> Optional[int]:
    """Parse the sheet number from a sheet chunk identifier."""
    match = SHEET_ID_RE.search(str(chunk_id))
    if not match:
        return None
    return int(match.group(1))


def parse_sheet_minor(chunk_id: str) -> int:
    """Parse the within-sheet chunk number from a sheet chunk identifier."""
    match = SHEET_ID_RE.search(str(chunk_id))
    if not match or not match.group(2):
        return 0
    return int(match.group(2))


def chunk_sort_key(chunk: Dict[str, Any]) -> Tuple[str, str, int, int, str]:
    """Sort chunks by file, sheet, sub-chunk, and chunk ID."""
    sheet_number = parse_sheet_number(chunk.get("chunk_id", "")) or 0
    return (
        str(chunk.get("file_id", "")),
        str(chunk.get("bank", "")),
        sheet_number,
        parse_sheet_minor(chunk.get("chunk_id", "")),
        str(chunk.get("chunk_id", "")),
    )


def cap_gap_filled_chunks(
    chunks: List[Dict[str, Any]],
    anchor_chunks: List[Dict[str, Any]],
    limit: int,
) -> List[Dict[str, Any]]:
    """Keep retrieved anchors first, then gap-fill chunks only if space remains."""
    if limit <= 0:
        return []
    if len(chunks) <= limit:
        return chunks

    anchor_keys = {candidate_key(chunk) for chunk in anchor_chunks}
    selected = []
    selected_keys = set()
    for chunk in sorted(chunks, key=chunk_sort_key):
        key = candidate_key(chunk)
        if key not in anchor_keys or key in selected_keys:
            continue
        selected.append(chunk)
        selected_keys.add(key)
        if len(selected) >= limit:
            return selected

    for chunk in sorted(chunks, key=chunk_sort_key):
        key = candidate_key(chunk)
        if key in selected_keys:
            continue
        selected.append(chunk)
        selected_keys.add(key)
        if len(selected) >= limit:
            break
    return selected


def sanitize_search_query(query_text: str) -> str:
    """Prepare query text for PostgreSQL websearch_to_tsquery."""
    cleaned = re.sub(r"[^\w\s&|\"'-]", " ", query_text)
    return " ".join(cleaned.split())


def sanitize_bm25_term(term: str) -> str:
    """Normalize one BM25 term."""
    cleaned = NON_ALNUM_RE.sub(" ", term.replace("%", " percentage "))
    return " ".join(cleaned.split())


def build_bm25_query(prepared: Dict[str, Any]) -> str:
    """Build focused BM25 text from metrics and keywords."""
    terms = []
    seen = set()
    for raw_term in [*prepared.get("metrics", []), *prepared.get("keywords", [])]:
        cleaned = sanitize_bm25_term(raw_term)
        normalized = cleaned.casefold()
        if not cleaned or normalized in seen:
            continue
        seen.add(normalized)
        terms.append(f'"{cleaned}"' if " " in cleaned else cleaned)
        if len(terms) >= BM25_TERM_CAP:
            break
    if terms:
        return " OR ".join(terms)
    fallback = sanitize_bm25_term(prepared.get("rewritten_query", ""))
    return f'"{fallback}"' if " " in fallback else fallback


def extract_search_terms(text_value: str) -> Dict[str, List[str]]:
    """Extract deterministic keyword and metric candidates from query text."""
    text_lower = text_value.lower()
    metrics = [term for term in METRIC_TERMS if term in text_lower]
    tokens = re.findall(r"[a-z][a-z0-9&/-]{2,}", text_lower)
    keywords = []
    for token in tokens:
        if token in STOPWORDS or token in keywords:
            continue
        if any(token in metric.split() for metric in metrics):
            continue
        keywords.append(token)
    return {"metrics": metrics[:8], "keywords": keywords[:12]}


def limit_unique_texts(values: Sequence[Any], max_items: int) -> List[str]:
    """Deduplicate and cap text values."""
    limited = []
    seen = set()
    for value in values:
        normalized = " ".join(str(value).split())
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        limited.append(normalized)
        if len(limited) >= max_items:
            break
    return limited


def merge_unique_terms(
    primary: Sequence[Any], fallback: Sequence[Any], max_items: int
) -> List[str]:
    """Merge two term lists while preserving order and uniqueness."""
    return limit_unique_texts([*primary, *fallback], max_items)


def soften_hyde_answer(text_value: str) -> str:
    """Remove speculative numeric anchors from HYDE text."""
    softened = PERCENT_RANGE_RE.sub("a reported percentage range", text_value)
    softened = PERCENT_RE.sub("a reported percentage", softened)
    softened = BASIS_POINTS_RE.sub("a reported basis-point change", softened)
    softened = CURRENCY_AMOUNT_RE.sub("a reported amount", softened)
    softened = SCALED_AMOUNT_RE.sub("a reported amount", softened)
    return MULTISPACE_RE.sub(" ", softened).strip()


def apply_min_keep_floor(candidates: List[Dict[str, Any]], remove_set: set[int]) -> set[int]:
    """Restore best-scoring removals if rerank would keep too few chunks."""
    min_keep = min(RERANK_MIN_KEEP, len(candidates))
    would_keep = len(candidates) - len(remove_set)
    if would_keep >= min_keep:
        return remove_set
    scored_removals = sorted(remove_set, key=lambda index: candidates[index].get("score", 0.0))
    restore_count = min_keep - would_keep
    restored = set(scored_removals[-restore_count:])
    return remove_set - restored


def normalize_remove_indices(raw_indices: Any, candidate_count: int) -> set[int]:
    """Validate reranker removals while accepting stringified integer indices."""
    if not isinstance(raw_indices, list):
        raise ValueError("remove_indices must be a list")
    valid_indices: set[int] = set()
    for raw_index in raw_indices:
        if isinstance(raw_index, bool):
            continue
        if isinstance(raw_index, int):
            index = raw_index
        elif isinstance(raw_index, float) and raw_index.is_integer():
            index = int(raw_index)
        elif isinstance(raw_index, str) and raw_index.strip().isdigit():
            index = int(raw_index.strip())
        else:
            continue
        if 0 <= index < candidate_count:
            valid_indices.add(index)
    return valid_indices


def queries_are_repeats(previous_queries: List[str], current_queries: List[str]) -> bool:
    """Check whether follow-up queries repeat prior search requests."""
    if not previous_queries or not current_queries:
        return False
    previous = {query.strip().casefold() for query in previous_queries}
    return all(query.strip().casefold() in previous for query in current_queries)


def normalize_finding_type_value(value: Any) -> str:
    """Return a supported finding type for downstream formatting."""
    normalized = str(value or "").strip().lower()
    if normalized in {"quantitative", "qualitative", "table", "summary", "detailed"}:
        return normalized
    return ""


def normalize_research_table(raw_table: Any) -> Optional[Dict[str, Any]]:
    """Validate and normalize optional table payloads from research prompts."""
    if not isinstance(raw_table, dict):
        return None
    columns = [str(column).strip() for column in raw_table.get("columns", []) if str(column).strip()]
    raw_rows = raw_table.get("rows") or []
    rows = []
    if isinstance(raw_rows, list):
        for raw_row in raw_rows:
            if isinstance(raw_row, dict):
                rows.append({str(key): value for key, value in raw_row.items()})
            elif isinstance(raw_row, list) and columns:
                rows.append(
                    {
                        columns[index]: value
                        for index, value in enumerate(raw_row[: len(columns)])
                    }
                )
    if not columns and rows:
        columns = list(rows[0].keys())
    if not columns and not rows:
        return None
    return {
        "title": str(raw_table.get("title") or "").strip(),
        "columns": columns,
        "rows": rows,
        "notes": str(raw_table.get("notes") or "").strip(),
    }


def parse_research_findings(
    raw_findings: Any,
    source_catalog: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Validate and normalize research findings from the LLM."""
    if not isinstance(raw_findings, list):
        raise ValueError("findings must be an array")
    source_catalog = source_catalog or {}
    findings = []
    for item in raw_findings:
        if not isinstance(item, dict):
            raise ValueError("each finding must be an object")
        if not isinstance(item.get("finding"), str) or not item["finding"].strip():
            raise ValueError("finding text is required")
        if not isinstance(item.get("page"), (int, float)):
            raise ValueError("finding page is required")
        if not isinstance(item.get("location_detail"), str) or not item["location_detail"].strip():
            raise ValueError("finding location_detail is required")
        finding = {
            "finding": item["finding"].strip(),
            "page": int(item["page"]),
            "location_detail": item["location_detail"].strip(),
            "source_ref_ids": resolve_source_ref_ids(item, source_catalog),
            "metric_name": str(item.get("metric_name") or ""),
            "metric_value": str(item.get("metric_value") or ""),
            "unit": str(item.get("unit") or ""),
            "period": str(item.get("period") or ""),
            "segment": str(item.get("segment") or ""),
        }
        finding_type = normalize_finding_type_value(item.get("finding_type"))
        if finding_type:
            finding["finding_type"] = finding_type
        details = str(item.get("details") or "").strip()
        if details:
            finding["details"] = details
        table = normalize_research_table(item.get("table"))
        if table:
            finding["table"] = table
        finding["references"] = [
            source_catalog[ref_id]
            for ref_id in finding["source_ref_ids"]
            if ref_id in source_catalog
        ]
        findings.append(finding)
    return findings


def combine_findings(iterations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge findings from all research iterations, deduping by page and text."""
    seen = set()
    combined = []
    for iteration in iterations:
        for finding in iteration.get("findings", []):
            key = (
                finding.get("finding", ""),
                finding.get("page"),
                finding.get("location_detail", ""),
            )
            if key in seen:
                continue
            seen.add(key)
            combined.append(finding)
    return combined


def format_rerank_candidates(candidates: List[Dict[str, Any]]) -> str:
    """Format candidate metadata and summaries for rerank without raw content."""
    lines = []
    for index, candidate in enumerate(candidates):
        keywords = ", ".join(candidate.get("keywords", [])[:8]) or "none"
        metrics = ", ".join(candidate.get("metrics", [])[:8]) or "none"
        sources = ", ".join(candidate.get("match_sources", [])) or "unknown"
        lines.extend(
            [
                f"[{index}] {candidate['bank']} {candidate['quarter']} "
                f"{candidate['fiscal_year']} | Page {candidate['page_number']} | "
                f"Sheet: {candidate.get('name') or 'unknown'} | "
                f"Chunk: {candidate['chunk_id']} | Score: {candidate.get('score', 0.0):.3f}",
                f"Filename: {candidate['filename']}",
                f"Match sources: {sources}",
                f"Summary: {truncate_text(candidate.get('summary') or '', 500)}",
                f"Keywords: {keywords}",
                f"Metrics: {metrics}",
                "",
            ]
        )
    return "\n".join(lines)


def build_source_catalog(chunks: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Build stable source refs for chunks shown to the research LLM."""
    catalog: Dict[str, Dict[str, Any]] = {}
    for index, chunk in enumerate(sorted(chunks, key=chunk_sort_key), start=1):
        ref_id = f"S{index}"
        catalog[ref_id] = build_source_reference(ref_id, chunk)
    return catalog


def build_source_reference(ref_id: str, chunk: Dict[str, Any]) -> Dict[str, Any]:
    """Build source metadata and an S3 link marker for a chunk."""
    filename = str(chunk.get("filename") or "")
    file_path = str(chunk.get("file_path") or filename)
    s3_key = filename or Path(file_path).name
    file_type = str(chunk.get("file_type") or Path(s3_key).suffix.lstrip(".") or "file")
    action = "open" if file_type.lower() == "pdf" else "download"
    page_number = int(chunk.get("page_number") or 0)
    sheet_name = str(chunk.get("name") or "Unknown sheet")
    bank = str(chunk.get("bank") or "")
    quarter = str(chunk.get("quarter") or "")
    fiscal_year = str(chunk.get("fiscal_year") or "")
    display_text = f"{bank} {quarter} {fiscal_year} source".strip()

    return {
        "ref_id": ref_id,
        "source_type": chunk.get("source_type", ""),
        "file_id": chunk.get("file_id", ""),
        "filename": filename,
        "file_path": file_path,
        "s3_key": s3_key,
        "file_type": file_type,
        "action": action,
        "page": page_number,
        "sheet": sheet_name,
        "chunk_id": chunk.get("chunk_id", ""),
        "bank": bank,
        "quarter": quarter,
        "fiscal_year": fiscal_year,
        "link_marker": format_s3_link_marker(
            action=action,
            file_type=file_type,
            s3_key=s3_key,
            display_text=display_text,
        ),
    }


def format_s3_link_marker(action: str, file_type: str, s3_key: str, display_text: str) -> str:
    """Return an Aegis S3 marker consumed by main.process_s3_links."""
    safe_key = str(s3_key).replace(":", "")
    safe_text = str(display_text).replace("}", "").strip() or "Source"
    safe_type = str(file_type).replace(":", "") or "file"
    safe_action = str(action).replace(":", "") or "download"
    return f"{{{{S3_LINK:{safe_action}:{safe_type}:{safe_key}:{safe_text}}}}}"


def resolve_source_ref_ids(
    finding: Dict[str, Any],
    source_catalog: Dict[str, Dict[str, Any]],
) -> List[str]:
    """Resolve explicit or fallback source refs for one finding."""
    raw_refs = finding.get("source_ref_ids", [])
    ref_ids = limit_unique_texts(raw_refs if isinstance(raw_refs, list) else [], 6)
    valid_refs = [ref_id for ref_id in ref_ids if ref_id in source_catalog]
    if valid_refs or not source_catalog:
        return valid_refs
    return infer_source_ref_ids(finding, source_catalog)


def infer_source_ref_ids(
    finding: Dict[str, Any],
    source_catalog: Dict[str, Dict[str, Any]],
) -> List[str]:
    """Infer source refs from page and location if the LLM omits source_ref_ids."""
    page = int(finding.get("page") or 0)
    location = str(finding.get("location_detail") or "").casefold()
    page_matches = [
        ref_id
        for ref_id, reference in source_catalog.items()
        if int(reference.get("page") or 0) == page
    ]
    if not page_matches:
        return []
    location_matches = [
        ref_id
        for ref_id in page_matches
        if location
        and (
            location in str(source_catalog[ref_id].get("sheet") or "").casefold()
            or location in str(source_catalog[ref_id].get("chunk_id") or "").casefold()
        )
    ]
    return location_matches or page_matches[:1]


def format_evidence_chunks(
    chunks: List[Dict[str, Any]],
    source_catalog: Dict[str, Dict[str, Any]],
) -> str:
    """Format expanded evidence chunks for the research LLM."""
    if not chunks:
        return "No evidence chunks were retrieved."
    ordered = sorted(chunks, key=chunk_sort_key)
    lines = []
    for chunk in ordered:
        reference = find_catalog_reference_for_chunk(source_catalog, chunk)
        ref_id = reference["ref_id"] if reference else "S?"
        gap_label = " | Gap fill" if chunk.get("is_gap_fill") else ""
        sources = ", ".join(chunk.get("match_sources", [])) or "context"
        lines.extend(
            [
                f"=== {chunk.get('name') or 'Unknown sheet'} "
                f"(Citation Page {chunk.get('page_number', 0)}) ===",
                (
                    f"[Source Ref: {ref_id} | Bank: {chunk.get('bank')} | "
                    f"Period: {chunk.get('quarter')} "
                    f"{chunk.get('fiscal_year')} | Chunk ID: {chunk.get('chunk_id')} | "
                    f"File: {chunk.get('filename')} | S3 key: {chunk.get('filename')} | "
                    f"Match: {sources}{gap_label}]"
                ),
            ]
        )
        if chunk.get("summary"):
            lines.append(f"Summary: {chunk['summary']}")
        lines.extend([chunk.get("chunk_content", "").strip(), ""])
    return "\n".join(lines)


def find_catalog_reference_for_chunk(
    source_catalog: Dict[str, Dict[str, Any]],
    chunk: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Find the source-catalog row for a chunk."""
    chunk_key = candidate_key(chunk)
    for reference in source_catalog.values():
        if (reference.get("file_path"), reference.get("chunk_id")) == (
            chunk.get("file_path"),
            chunk.get("chunk_id"),
        ):
            return reference
        if (reference.get("filename"), reference.get("chunk_id")) == (
            chunk.get("filename"),
            chunk_key[1],
        ):
            return reference
    return None


def format_previous_research(iterations: List[Dict[str, Any]]) -> str:
    """Format prior research iterations for the next research prompt."""
    if not iterations:
        return ""
    lines = ["<previous_research>"]
    for iteration in iterations:
        lines.append(
            f"[Iteration {iteration['iteration']}, confidence: {iteration.get('confidence', 0.0)}]"
        )
        for finding in iteration.get("findings", []):
            lines.append(
                f"- {finding['finding']} " f"(Page {finding['page']}, {finding['location_detail']})"
            )
        lines.append("")
    lines.append("</previous_research>")
    return "\n".join(lines)


def format_retrieval_response(results: Dict[str, Any]) -> str:
    """Format research findings for the dropdown and downstream summarizer."""
    lines = ["## Research Findings", ""]

    for combo_result in results["combo_results"]:
        combo = combo_result["combo"]
        lines.extend([f"### {combo_bank_label(combo)} / {combo_period_label(combo)}", ""])
        findings = combo_result.get("findings", [])
        if findings:
            for finding in findings:
                metric_text = format_metric_fields(finding)
                source_text = format_finding_references(finding)
                lines.append(f"- {finding['finding']} " f"{metric_text}\n  Source: {source_text}")
        elif combo_result.get("metrics", {}).get("skipped") == "no_search_candidates":
            lines.append("- No supplementary financials content was found for this bank/period.")
        else:
            lines.append("- No structured findings extracted.")
        lines.append("")

    if not results["combo_results"]:
        lines.append("No bank-period combinations were processed.")
    return "\n".join(lines).strip()


def format_finding_references(finding: Dict[str, Any]) -> str:
    """Format source references for one finding."""
    references = finding.get("references") or []
    if not references:
        return f"Page {finding['page']}, {finding['location_detail']}"
    rendered = []
    for reference in references:
        rendered.append(f"{reference['link_marker']} | Sheet: {reference['sheet']}")
    return "; ".join(rendered)


def format_metric_fields(finding: Dict[str, Any]) -> str:
    """Format optional metric fields for downstream summarization."""
    metric_name = finding.get("metric_name") or ""
    metric_value = finding.get("metric_value") or ""
    if not metric_name and not metric_value:
        return ""
    parts = []
    for key in ("metric_name", "metric_value", "unit", "period", "segment"):
        value = finding.get(key)
        if value:
            parts.append(f"{key}={value}")
    return " [" + "; ".join(parts) + "]"


def truncate_text(value: str, limit: int) -> str:
    """Truncate long text for prompt-safe output."""
    if len(value) <= limit:
        return value
    return value[: limit - 15].rstrip() + "\n...[truncated]"


def redact_embeddings(prepared: Dict[str, Any]) -> Dict[str, Any]:
    """Return prepared query metadata without large vector payloads."""
    redacted = dict(prepared)
    redacted["embeddings"] = {
        key: f"<{len(value)} dims>" if isinstance(value, list) else "<embedding>"
        for key, value in prepared.get("embeddings", {}).items()
    }
    return redacted
