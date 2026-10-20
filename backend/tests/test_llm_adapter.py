"""Adapter request shapes for Ollama and vLLM (P0, P9), using a mock HTTP transport."""

from __future__ import annotations

import json

import httpx
import numpy as np

from app.config import Config
from app.extraction.llm_adapter import OllamaAdapter, VLLMAdapter, parse_json_loose

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def _ollama(handler) -> OllamaAdapter:
    cfg = Config.model_validate({"llm": {"model": "m1"}, "embeddings": {"model": "emb"}})
    ad = OllamaAdapter(cfg)
    ad.client = httpx.Client(transport=httpx.MockTransport(handler))
    return ad


def test_ollama_structured_output_and_think_flag():
    seen = {}

    def handler(req: httpx.Request):
        body = json.loads(req.content or b"{}")
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "thinking"]})
        if req.url.path == "/api/chat":
            seen.update(body)
            return httpx.Response(200, json={"message": {"content": '```json\n{"a": "x"}\n```'}})
        if req.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[3.0, 4.0]] * len(body["input"])})
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "m1"}, {"name": "emb:latest"}]})
        return httpx.Response(404)

    ad = _ollama(handler)
    assert ad.chat_json([{"role": "user", "content": "hi"}], SCHEMA, temperature=0.6, seed=7) == {"a": "x"}
    assert seen["format"] == SCHEMA and seen["think"] is False and seen["stream"] is False
    assert seen["options"]["temperature"] == 0.6 and seen["options"]["seed"] == 7
    v = ad.embed(["a", "b"])
    assert v.shape == (2, 2) and np.allclose(np.linalg.norm(v, axis=1), 1)
    h = ad.health()
    assert h["reachable"] and h["models"]["llm"]["available"] and h["models"]["embeddings"]["available"]


def test_ollama_unreachable_reports_health():
    def handler(req):
        raise httpx.ConnectError("refused")

    h = _ollama(handler).health()
    assert h["reachable"] is False


def test_vllm_guided_json():
    seen = {}

    def handler(req: httpx.Request):
        body = json.loads(req.content or b"{}")
        if req.url.path == "/v1/chat/completions":
            seen.update(body)
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"a": "y"}'}}]})
        if req.url.path == "/v1/embeddings":
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(body["input"]))]})
        if req.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "Qwen/Qwen2.5-14B-Instruct"}]})
        return httpx.Response(404)

    cfg = Config.model_validate({"llm": {"backend": "vllm", "base_url": "http://gpu:8000", "model": "Qwen/Qwen2.5-14B-Instruct"}})
    ad = VLLMAdapter(cfg)
    ad.client = httpx.Client(transport=httpx.MockTransport(handler))
    assert ad.chat_json([{"role": "user", "content": "hi"}], SCHEMA) == {"a": "y"}
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["schema"] == SCHEMA
    assert ad.embed(["x"]).shape == (1, 2)
    assert ad.health()["models"]["llm"]["available"]


def test_parse_json_loose():
    assert parse_json_loose('noise {"a": 1} trailing') == {"a": 1}
