"""First-run setup over HTTP: status, model choice saved to the user config, background run."""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

from app import setup_ollama as so


def _client():
    from app.main import create_app

    return TestClient(create_app(start_worker=False))


def test_setup_status_and_start(app_env, tmp_path, monkeypatch):
    user_cfg = tmp_path / "user-config.yaml"
    monkeypatch.setenv("FORMFILLER_CONFIG", str(user_cfg))
    from app.config import reset_config

    reset_config()
    state = {"running": False, "models": set(), "calls": []}
    monkeypatch.setattr(so, "ollama_binary", lambda cfg=None: None)
    monkeypatch.setattr(so, "server_up", lambda url: state["running"])
    monkeypatch.setattr(so, "local_models", lambda url: set(state["models"]))
    monkeypatch.setattr(so, "total_ram_gb", lambda: 16.0)

    def install_portable(cfg, progress=None):
        state["calls"].append("install_portable")
        return cfg.data_path / "ollama" / "ollama"

    def start(url, log_dir, wait_s=45, binary=None, attached=False):
        state["calls"].append(f"start:{binary and 'portable'}:{attached}")
        state["running"] = True

    def pull(url, model, progress=None):
        state["calls"].append(f"pull:{model}")
        state["models"].add(model)

    monkeypatch.setattr(so, "install_portable", install_portable)
    monkeypatch.setattr(so, "start", start)
    monkeypatch.setattr(so, "pull", pull)
    monkeypatch.setattr(so, "runner", so.SetupRunner())

    with _client() as c:
        s = c.get("/api/setup").json()
        assert s["ready"] is False and s["ollama"]["installed"] is False
        assert [m["name"] for m in s["catalog"] if m["recommended"]] == ["qwen2.5:7b-instruct"]
        assert s["ram_gb"] == 16.0 and s["ocr"]["rapidocr"] is True

        s = c.post("/api/setup/start", json={"model": "gemma3:4b"}).json()
        assert s["models"]["current"] == "gemma3:4b"
        assert yaml.safe_load(user_cfg.read_text())["llm"]["model"] == "gemma3:4b"
        so.runner._thread.join(10)
        p = c.get("/api/setup/progress").json()
        assert p["done"] and p["error"] is None
        assert state["calls"][:2] == ["install_portable", "start:portable:True"]
        assert "pull:gemma3:4b" in state["calls"] and "pull:bge-m3" in state["calls"]
        assert c.get("/api/setup").json()["ready"] is True
        # a second start while one is running is refused
        so.runner.state["running"] = True
        assert c.post("/api/setup/start", json={}).status_code == 409
