"""Configuration loading.

Order of precedence (later wins):
  1. Built-in defaults (the models below)
  2. config.yaml (path from FORMFILLER_CONFIG, else ./config.yaml, else repo root)
  3. Environment variables of the form FF__SECTION__KEY=value, e.g. FF__LLM__MODEL=gemma4:12b
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]


class LLMConfig(BaseModel):
    backend: Literal["ollama", "vllm"] = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "qwen2.5:14b-instruct"
    temperature: float = 0.0
    context_budget_tokens: int = 12000
    num_ctx: int = 16384  # KV cache size per request slot; the biggest memory lever after model size
    keep_alive: str = "5m"  # how long Ollama keeps models loaded after the last request
    request_timeout_s: float = 600.0
    trace: bool = False  # append every LLM call to <data_dir>/llm_trace.jsonl (local debugging)
    api_key: str | None = None  # vLLM only, optional


class VisionConfig(BaseModel):
    enabled: bool = False
    model: str = "qwen2.5vl:7b"
    min_ocr_confidence: float = 60.0


class EmbeddingsConfig(BaseModel):
    model: str = "bge-m3"
    base_url: str | None = None  # defaults to llm.base_url for ollama
    batch_size: int = 32


class OCRConfig(BaseModel):
    engine: Literal["docling", "paddleocr", "tesseract"] = "docling"
    language: str = "en"
    text_layer_min_chars: int = 30
    dpi: int = 300


class RetrievalConfig(BaseModel):
    k: int = 8
    use_full_context_if_fits: bool = True
    chunk_tokens: int = 500


class VerificationConfig(BaseModel):
    fuzzy_threshold: int = 90


class JudgmentConfig(BaseModel):
    model: str | None = None
    self_consistency_runs: int = 5
    temperature: float = 0.6
    parallel_runs: int = 1  # >1 only helps if the server has spare memory (OLLAMA_NUM_PARALLEL)


class ExtractionConfig(BaseModel):
    batch_min: int = 5
    batch_max: int = 15
    max_fewshot: int = 5
    parallel_batches: int = 1


class StorageConfig(BaseModel):
    data_dir: str = "./data"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080
    workers: int = 2
    worker_mode: Literal["process", "thread"] = "process"


class Config(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    judgment: JudgmentConfig = Field(default_factory=JudgmentConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)

    @property
    def data_path(self) -> Path:
        p = Path(self.storage.data_dir)
        if not p.is_absolute():
            p = (self._base_dir / p).resolve()
        return p

    _base_dir: Path = REPO_ROOT

    @property
    def judgment_model(self) -> str:
        return self.judgment.model or self.llm.model

    @property
    def embeddings_base_url(self) -> str:
        return self.embeddings.base_url or self.llm.base_url


def _find_config_file() -> Path | None:
    env = os.environ.get("FORMFILLER_CONFIG")
    if env:
        return Path(env)
    for cand in (Path.cwd() / "config.yaml", REPO_ROOT / "config.yaml"):
        if cand.exists():
            return cand
    return None


def _coerce(value: str):
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError:
        return value


def _apply_env(data: dict) -> dict:
    for key, value in os.environ.items():
        if not key.startswith("FF__"):
            continue
        parts = [p.lower() for p in key[4:].split("__")]
        cur = data
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = _coerce(value)
    return data


def load_config(path: Path | None = None) -> Config:
    path = path or _find_config_file()
    data: dict = {}
    if path and path.exists():
        data = yaml.safe_load(path.read_text()) or {}
    data = _apply_env(data)
    cfg = Config.model_validate(data)
    cfg._base_dir = path.parent.resolve() if path else REPO_ROOT
    return cfg


@lru_cache(maxsize=1)
def get_config() -> Config:
    return load_config()


def reset_config() -> None:
    get_config.cache_clear()
