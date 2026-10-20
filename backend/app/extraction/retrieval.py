"""Hybrid retrieval: BM25 top-k + embedding top-k, merged with reciprocal rank fusion."""

from __future__ import annotations

import logging

from app.config import get_config
from app.db.models import FieldDef
from app.extraction.llm_adapter import get_llm
from app.ingest.chunking import approx_tokens
from app.ingest.embeddings import JobIndex

log = logging.getLogger(__name__)


def build_query(section: str | None, fields: list[FieldDef]) -> str:
    parts = [section or ""]
    for f in fields:
        parts.append(f.label)
        if f.format_hint:
            parts.append(f.format_hint)
        if f.options and len(f.options) <= 6:
            parts.extend(f.options)
    return " ".join(p for p in parts if p)


def rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking):
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return [cid for cid, _ in sorted(scores.items(), key=lambda kv: -kv[1])]


def total_tokens(index: JobIndex) -> int:
    return sum(approx_tokens(c["text"]) for c in index.chunks)


def fits_full_context(index: JobIndex, reserve: int = 4000) -> bool:
    cfg = get_config()
    return cfg.retrieval.use_full_context_if_fits and total_tokens(index) + reserve <= cfg.llm.context_budget_tokens


def retrieve(index: JobIndex, section: str | None, fields: list[FieldDef], k: int | None = None) -> list[dict]:
    """Chunks for one field batch, in document order."""
    if not index.chunks:
        return []
    if fits_full_context(index):
        return list(index.chunks)
    k = k or get_config().retrieval.k
    rankings: list[list[int]] = []
    queries = [build_query(section, fields)] + [f"{f.label} {f.instructions or ''}" for f in fields]
    # one BM25 ranking for the batch, plus per-field BM25 so a rare field isn't drowned out
    rankings.append(index.bm25_top(queries[0], k * 2))
    for q in queries[1:]:
        rankings.append(index.bm25_top(q, 3))
    try:
        qvecs = get_llm().embed(queries)
        rankings.append(index.vector_top(qvecs[0], k * 2))
        for qv in qvecs[1:]:
            rankings.append(index.vector_top(qv, 3))
    except Exception as e:  # noqa: BLE001
        log.warning("embedding retrieval unavailable, BM25 only: %s", e)
    fused = rrf(rankings)
    # respect the budget: keep adding best chunks while they fit (but at least k)
    budget = get_config().llm.context_budget_tokens - 4000
    chosen: list[int] = []
    used = 0
    for cid in fused:
        t = approx_tokens(index.by_id[cid]["text"])
        if len(chosen) >= k and used + t > budget:
            break
        if len(chosen) >= max(k, len(fields) + k // 2):
            break
        chosen.append(cid)
        used += t
    order = {c["id"]: i for i, c in enumerate(index.chunks)}
    return [index.by_id[c] for c in sorted(chosen, key=lambda c: order[c])]
