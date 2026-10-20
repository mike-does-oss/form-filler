from __future__ import annotations

import hashlib
import re
import warnings
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pytest

warnings.filterwarnings("ignore")

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "eval" / "fixtures"


class FakeLLM:
    """Deterministic stand-in for the LLM adapter.

    `responder(messages, schema, **kw)` returns the JSON output; by default every
    schema gets an "empty" answer (nulls / empty lists / first enum value).
    """

    def __init__(self, responder: Callable[..., Any] | None = None):
        self.responder = responder
        self.calls: list[dict] = []

    def chat_json(self, messages, schema, *, model=None, temperature=None, seed=None):
        self.calls.append({"messages": messages, "schema": schema, "temperature": temperature, "seed": seed})
        if self.responder:
            out = self.responder(messages, schema, temperature=temperature, seed=seed)
            if out is not None:
                return out
        return empty_for(schema)

    def embed(self, texts):
        vecs = []
        for t in texts:
            v = np.zeros(64, dtype=np.float32)
            for tok in re.findall(r"[a-z0-9]+", t.lower()):
                v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % 64] += 1
            n = np.linalg.norm(v)
            vecs.append(v / n if n else v)
        return np.vstack(vecs) if vecs else np.zeros((0, 64), dtype=np.float32)

    def vision_transcribe(self, image_png, prompt, model=None):
        return ""

    def list_models(self):
        return ["fake"]

    def health(self):
        return {"reachable": True, "models": {"llm": {"name": "fake", "available": True}}}


def empty_for(schema: dict) -> Any:
    if "anyOf" in schema:
        for s in schema["anyOf"]:
            if s.get("type") == "null":
                return None
        return empty_for(schema["anyOf"][0])
    t = schema.get("type")
    if isinstance(t, list):
        return None if "null" in t else empty_for({**schema, "type": t[0]})
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        return {k: empty_for(v) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return []
    if t == "boolean":
        return False
    if t == "integer":
        return 0
    if t == "string":
        return ""
    return None


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Isolated data dir + fake LLM + thread worker mode."""
    from app import db
    from app.config import reset_config
    from app.extraction.llm_adapter import set_llm

    monkeypatch.setenv("FF__STORAGE__DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FF__SERVER__WORKER_MODE", "thread")
    monkeypatch.setenv("FF__OCR__ENGINE", "tesseract")
    monkeypatch.setenv("FF__JUDGMENT__SELF_CONSISTENCY_RUNS", "5")
    monkeypatch.delenv("FORMFILLER_CONFIG", raising=False)
    reset_config()
    fake = FakeLLM()
    set_llm(fake)
    yield fake
    set_llm(None)
    db.close_all()
    reset_config()


def chunk_ids_in(messages) -> dict[int, str]:
    """Map chunk_id -> chunk text from an extraction prompt."""
    text = messages[-1]["content"]
    out = {}
    for m in re.finditer(r"\[chunk_id=(\d+)\][^\n]*\n(.*?)(?=\n\n---\n\n\[chunk_id=|\n\nReturn a JSON|\Z)", text, re.S):
        out[int(m.group(1))] = m.group(2)
    return out


def find_quote(messages, pattern: str) -> tuple[int, str] | None:
    for cid, text in chunk_ids_in(messages).items():
        m = re.search(pattern, text)
        if m:
            return cid, m.group(0)
    return None
