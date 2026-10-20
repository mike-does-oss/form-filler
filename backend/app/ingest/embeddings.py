"""Chunk embeddings (stored as float32 blobs in SQLite) and a BM25 index per job."""

from __future__ import annotations

import re
import threading

import numpy as np
from rank_bm25 import BM25Okapi

from app import db
from app.extraction.llm_adapter import get_llm

_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[.'@-][A-Za-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def embed_chunks(chunk_ids: list[int], texts: list[str]) -> None:
    if not chunk_ids:
        return
    vecs = get_llm().embed(texts)
    with db.transaction() as conn:
        for cid, v in zip(chunk_ids, vecs):
            conn.execute("UPDATE chunks SET embedding = ? WHERE id = ?", (to_blob(v), cid))


class JobIndex:
    """In-memory hybrid index over all chunks of a job."""

    def __init__(self, job_id: int):
        rows = db.q(
            """SELECT c.id, c.document_id, c.page, c.bbox_json, c.text, c.embedding, c.chunk_index, c.meta_json
               FROM chunks c JOIN documents d ON d.id = c.document_id
               WHERE d.job_id = ? ORDER BY c.document_id, c.chunk_index""",
            (job_id,),
        )
        self.job_id = job_id
        self.chunks = rows
        self.by_id = {r["id"]: r for r in rows}
        self.bm25 = BM25Okapi([tokenize(r["text"]) or ["_"] for r in rows]) if rows else None
        vecs = [from_blob(r["embedding"]) for r in rows]
        if rows and all(v is not None for v in vecs):
            self.matrix: np.ndarray | None = np.vstack(vecs)
        else:
            self.matrix = None

    def bm25_top(self, query: str, k: int) -> list[int]:
        if not self.bm25:
            return []
        scores = self.bm25.get_scores(tokenize(query))
        order = np.argsort(-scores)[:k]
        return [self.chunks[i]["id"] for i in order if scores[i] > 0]

    def vector_top(self, qvec: np.ndarray, k: int) -> list[int]:
        if self.matrix is None:
            return []
        sims = self.matrix @ qvec
        order = np.argsort(-sims)[:k]
        return [self.chunks[i]["id"] for i in order]


_cache: dict[int, JobIndex] = {}
_lock = threading.Lock()


def get_index(job_id: int, refresh: bool = False) -> JobIndex:
    with _lock:
        if refresh or job_id not in _cache:
            _cache[job_id] = JobIndex(job_id)
        return _cache[job_id]


def drop_index(job_id: int) -> None:
    with _lock:
        _cache.pop(job_id, None)
