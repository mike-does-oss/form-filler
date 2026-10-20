"""One adapter interface over Ollama and vLLM (OpenAI-compatible) servers.

All calls stay on the configured local base_url. Structured output is enforced by
the server (Ollama `format`, vLLM `response_format: json_schema`).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
import numpy as np

from app.config import Config, get_config

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def parse_json_loose(text: str) -> Any:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise
        end = max(text.rfind("}"), text.rfind("]"))
        return json.loads(text[start : end + 1])


class LLMAdapter(ABC):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._trace_lock = threading.Lock()

    def trace(self, kind: str, model: str, messages: Any, output: Any, seconds: float) -> None:
        if not self.cfg.llm.trace:
            return
        rec = {"ts": time.time(), "kind": kind, "model": model, "seconds": round(seconds, 2), "messages": messages, "output": output}
        path = self.cfg.data_path / "llm_trace.jsonl"
        with self._trace_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    @abstractmethod
    def chat_json(
        self,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        model: str | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> Any: ...

    @abstractmethod
    def embed(self, texts: list[str]) -> np.ndarray: ...

    @abstractmethod
    def vision_transcribe(self, image_png: bytes, prompt: str, model: str | None = None) -> str: ...

    @abstractmethod
    def list_models(self) -> list[str]: ...

    def health(self) -> dict[str, Any]:
        cfg = self.cfg
        out: dict[str, Any] = {"backend": cfg.llm.backend, "base_url": cfg.llm.base_url}
        try:
            models = self.list_models()
            out["reachable"] = True
        except Exception as e:  # noqa: BLE001
            out.update(reachable=False, error=str(e), models={})
            return out
        wanted = {
            "llm": cfg.llm.model,
            "judgment": cfg.judgment_model,
            "embeddings": cfg.embeddings.model,
        }
        if cfg.vision.enabled:
            wanted["vision"] = cfg.vision.model
        out["models"] = {
            role: {"name": name, "available": _model_available(name, models)} for role, name in wanted.items()
        }
        out["available_models"] = models
        return out


def _model_available(name: str, models: list[str]) -> bool:
    if name in models:
        return True
    if ":" not in name and f"{name}:latest" in models:
        return True
    return False


class OllamaAdapter(LLMAdapter):
    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.base = cfg.llm.base_url.rstrip("/")
        self.embed_base = cfg.embeddings_base_url.rstrip("/")
        self.client = httpx.Client(timeout=cfg.llm.request_timeout_s)
        self._caps: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def _capabilities(self, model: str) -> list[str]:
        with self._lock:
            if model not in self._caps:
                try:
                    r = self.client.post(f"{self.base}/api/show", json={"model": model})
                    r.raise_for_status()
                    self._caps[model] = r.json().get("capabilities", []) or []
                except Exception:  # noqa: BLE001
                    self._caps[model] = []
            return self._caps[model]

    def chat_json(self, messages, schema, *, model=None, temperature=None, seed=None):
        model = model or self.cfg.llm.model
        options: dict[str, Any] = {
            "temperature": self.cfg.llm.temperature if temperature is None else temperature,
            "num_ctx": self.cfg.llm.num_ctx,
        }
        if seed is not None:
            options["seed"] = seed
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "format": schema,
            "stream": False,
            "options": options,
        }
        if "thinking" in self._capabilities(model):
            body["think"] = False
        last_err: Exception | None = None
        for _ in range(2):
            try:
                t0 = time.monotonic()
                r = self.client.post(f"{self.base}/api/chat", json=body)
                if r.status_code >= 400:
                    raise LLMError(f"ollama {r.status_code}: {r.text[:500]}")
                content = r.json()["message"]["content"]
                self.trace("chat_json", model, messages, content, time.monotonic() - t0)
                return parse_json_loose(content)
            except (json.JSONDecodeError, KeyError) as e:
                last_err = e
                log.warning("LLM returned unparseable JSON, retrying: %s", e)
            except httpx.HTTPError as e:
                raise LLMError(f"ollama request failed: {e}") from e
        raise LLMError(f"LLM output was not valid JSON: {last_err}")

    def embed(self, texts):
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        out: list[list[float]] = []
        bs = self.cfg.embeddings.batch_size
        for i in range(0, len(texts), bs):
            batch = [t[:8000] for t in texts[i : i + bs]]
            try:
                r = self.client.post(
                    f"{self.embed_base}/api/embed",
                    json={"model": self.cfg.embeddings.model, "input": batch, "truncate": True},
                )
            except httpx.HTTPError as e:
                raise LLMError(f"embedding request failed: {e}") from e
            if r.status_code >= 400:
                raise LLMError(f"ollama embed {r.status_code}: {r.text[:300]}")
            out.extend(r.json()["embeddings"])
        arr = np.asarray(out, dtype=np.float32)
        return _l2norm(arr)

    def vision_transcribe(self, image_png, prompt, model=None):
        model = model or self.cfg.vision.model
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(image_png).decode()]}],
            "stream": False,
            "options": {"temperature": 0},
        }
        if "thinking" in self._capabilities(model):
            body["think"] = False
        r = self.client.post(f"{self.base}/api/chat", json=body)
        if r.status_code >= 400:
            raise LLMError(f"ollama vision {r.status_code}: {r.text[:300]}")
        return r.json()["message"]["content"]

    def list_models(self):
        r = self.client.get(f"{self.base}/api/tags", timeout=5)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]


class VLLMAdapter(LLMAdapter):
    """vLLM's OpenAI-compatible server with guided JSON decoding."""

    def __init__(self, cfg: Config):
        super().__init__(cfg)
        self.base = cfg.llm.base_url.rstrip("/")
        if not self.base.endswith("/v1"):
            self.base += "/v1"
        self.embed_base = (cfg.embeddings.base_url or self.base).rstrip("/")
        if not self.embed_base.endswith("/v1"):
            self.embed_base += "/v1"
        headers = {"Authorization": f"Bearer {cfg.llm.api_key}"} if cfg.llm.api_key else {}
        self.client = httpx.Client(timeout=cfg.llm.request_timeout_s, headers=headers)

    def chat_json(self, messages, schema, *, model=None, temperature=None, seed=None):
        body: dict[str, Any] = {
            "model": model or self.cfg.llm.model,
            "messages": messages,
            "temperature": self.cfg.llm.temperature if temperature is None else temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": schema, "strict": True},
            },
        }
        if seed is not None:
            body["seed"] = seed
        t0 = time.monotonic()
        try:
            r = self.client.post(f"{self.base}/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"vllm request failed: {e}") from e
        if r.status_code >= 400:
            raise LLMError(f"vllm {r.status_code}: {r.text[:500]}")
        content = r.json()["choices"][0]["message"]["content"]
        self.trace("chat_json", body["model"], messages, content, time.monotonic() - t0)
        return parse_json_loose(content)

    def embed(self, texts):
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        out: list[list[float]] = []
        bs = self.cfg.embeddings.batch_size
        for i in range(0, len(texts), bs):
            r = self.client.post(
                f"{self.embed_base}/embeddings",
                json={"model": self.cfg.embeddings.model, "input": texts[i : i + bs]},
            )
            if r.status_code >= 400:
                raise LLMError(f"vllm embed {r.status_code}: {r.text[:300]}")
            out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"]))
        return _l2norm(np.asarray(out, dtype=np.float32))

    def vision_transcribe(self, image_png, prompt, model=None):
        url = "data:image/png;base64," + base64.b64encode(image_png).decode()
        body = {
            "model": model or self.cfg.vision.model,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}],
                }
            ],
            "temperature": 0,
        }
        r = self.client.post(f"{self.base}/chat/completions", json=body)
        if r.status_code >= 400:
            raise LLMError(f"vllm vision {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]

    def list_models(self):
        r = self.client.get(f"{self.base}/models", timeout=5)
        r.raise_for_status()
        return [m["id"] for m in r.json().get("data", [])]


def _l2norm(arr: np.ndarray) -> np.ndarray:
    if arr.size == 0:
        return arr
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


_adapter: LLMAdapter | None = None
_adapter_lock = threading.Lock()


def make_adapter(cfg: Config) -> LLMAdapter:
    if cfg.llm.backend == "vllm":
        return VLLMAdapter(cfg)
    return OllamaAdapter(cfg)


def get_llm() -> LLMAdapter:
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            _adapter = make_adapter(get_config())
        return _adapter


def set_llm(adapter: LLMAdapter | None) -> None:
    """Swap the adapter (tests, eval harness)."""
    global _adapter
    with _adapter_lock:
        _adapter = adapter
