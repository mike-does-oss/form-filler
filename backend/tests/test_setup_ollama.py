"""`form-filler setup`: install/start Ollama and pull models only when needed and agreed to."""

from __future__ import annotations

import pytest

from app import setup_ollama as so
from app.config import Config


@pytest.fixture
def fake(monkeypatch):
    state = {"installed": False, "running": False, "models": set(), "calls": []}

    monkeypatch.setattr(so, "ollama_binary", lambda: "/usr/local/bin/ollama" if state["installed"] else None)
    monkeypatch.setattr(so, "server_up", lambda url: state["running"])
    monkeypatch.setattr(so, "local_models", lambda url: set(state["models"]))

    def install():
        state["calls"].append("install")
        state["installed"] = True

    def start(url, log_dir, wait_s=45):
        state["calls"].append("start")
        state["running"] = True

    def pull(url, model):
        state["calls"].append(f"pull:{model}")
        state["models"].add(model)

    monkeypatch.setattr(so, "install", install)
    monkeypatch.setattr(so, "start", start)
    monkeypatch.setattr(so, "pull", pull)
    return state


def cfg(**llm) -> Config:
    return Config.model_validate({"llm": {"model": "m1", **llm}, "embeddings": {"model": "emb"}})


def test_fresh_machine_installs_starts_and_pulls(fake):
    assert so.ensure(cfg(), assume_yes=True)
    assert fake["calls"] == ["install", "start", "pull:m1", "pull:emb"]


def test_ready_machine_does_nothing(fake):
    fake.update(installed=True, running=True, models={"m1", "emb:latest"})
    assert so.ensure(cfg(), assume_yes=True)
    assert fake["calls"] == []


def test_installed_but_stopped_is_started_not_reinstalled(fake):
    fake.update(installed=True, models={"m1", "emb"})
    assert so.ensure(cfg(), assume_yes=True)
    assert fake["calls"] == ["start"]


def test_no_tty_and_no_yes_never_installs(fake, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert not so.ensure(cfg())
    assert fake["calls"] == []


def test_user_declines_download(fake, monkeypatch):
    fake.update(installed=True, running=True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert not so.ensure(cfg())
    assert fake["calls"] == []


def test_remote_or_vllm_is_left_alone(fake):
    assert not so.ensure(cfg(base_url="http://ollama:11434"), assume_yes=True)
    assert so.ensure(cfg(backend="vllm"), assume_yes=True)
    assert fake["calls"] == []
