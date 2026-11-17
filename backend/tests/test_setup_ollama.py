"""`form-filler setup`: install/start Ollama and pull models only when needed and agreed to."""

from __future__ import annotations

import platform
from pathlib import Path
import pytest

from app import setup_ollama as so
from app.config import Config


@pytest.fixture
def fake(monkeypatch):
    state = {"installed": False, "running": False, "models": set(), "calls": []}

    monkeypatch.setattr(so, "ollama_binary", lambda cfg=None: "/usr/local/bin/ollama" if state["installed"] else None)
    monkeypatch.setattr(so, "server_up", lambda url: state["running"])
    monkeypatch.setattr(so, "local_models", lambda url: set(state["models"]))

    def install():
        state["calls"].append("install")
        state["installed"] = True

    def start(url, log_dir, wait_s=45, binary=None, attached=False):
        state["calls"].append("start" + (":attached" if attached else ""))
        state["running"] = True

    def pull(url, model, progress=None):
        state["calls"].append(f"pull:{model}")
        state["models"].add(model)
        if progress:
            progress(f"{model}: pulling", 0.5)

    def install_portable(cfg, progress=None):
        state["calls"].append("install_portable")
        state["installed"] = True
        return cfg.data_path / "ollama" / "ollama"

    monkeypatch.setattr(so, "install", install)
    monkeypatch.setattr(so, "start", start)
    monkeypatch.setattr(so, "pull", pull)
    monkeypatch.setattr(so, "install_portable", install_portable)
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


# ------------------------------------------------------------------ portable install
def test_portable_setup_unpacks_into_data_dir(fake, tmp_path):
    c = cfg()
    c.storage.data_dir = str(tmp_path)
    assert so.ensure(c, assume_yes=True, portable=True)
    assert fake["calls"] == ["install_portable", "start", "pull:m1", "pull:emb"]


def _tgz(path, members: dict[str, bytes]):
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as t:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            t.addfile(info, io.BytesIO(data))


def _zip(path, members: dict[str, bytes]):
    import zipfile

    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)


def test_install_portable_downloads_and_extracts(tmp_path, monkeypatch):
    c = cfg()
    c.storage.data_dir = str(tmp_path)
    # The archive layout follows the real release asset for the platform the test runs on.
    if platform.system() == "Windows":
        asset, rel = "ollama-windows-amd64.zip", Path("ollama.exe")
        archive = tmp_path / "fake.zip"
        _zip(archive, {"ollama.exe": b"MZ", "lib/ollama/ggml.dll": b"x"})
    else:
        asset, rel = "ollama-linux-amd64.tgz", Path("bin") / "ollama"
        archive = tmp_path / "fake.tgz"
        _tgz(archive, {"bin/ollama": b"#!/bin/sh\necho ollama\n", "lib/ollama/libggml.so": b"x"})
    seen = []

    def fake_download(url, dest, progress=None, label=""):
        seen.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(archive.read_bytes())
        if progress:
            progress("Downloading", 1.0)

    monkeypatch.setattr(so, "download", fake_download)
    monkeypatch.setattr(so, "portable_asset", lambda: asset)
    msgs = []
    binary = so.install_portable(c, lambda t, f: msgs.append((t, f)))
    assert binary == tmp_path / "ollama" / rel
    if platform.system() != "Windows":
        assert binary.stat().st_mode & 0o111
    assert seen == [so.RELEASE_BASE + asset]
    assert so.portable_binary(c) == binary and so.ollama_binary(c) == str(binary)
    assert not (tmp_path / "downloads" / asset).exists()  # archive removed after unpacking
    assert ("Unpacking Ollama", None) in msgs


def test_extract_refuses_paths_outside_dest(tmp_path):
    archive = tmp_path / "evil.tgz"
    _tgz(archive, {"../escape": b"x"})
    with pytest.raises(so.SetupError, match="unsafe path"):
        so.extract_archive(archive, tmp_path / "out")


def test_recommended_model_by_ram():
    assert so.recommended_model(8) == "gemma3:4b"
    assert so.recommended_model(16) == "qwen2.5:7b-instruct"
    assert so.recommended_model(24) == "qwen2.5:7b-instruct"
    assert so.recommended_model(32) == "qwen2.5:14b-instruct"
    assert so.recommended_model(4) == "gemma3:4b"
    assert so.recommended_model(None) == "qwen2.5:7b-instruct"


# ------------------------------------------------------------------ background runner (UI)
def test_runner_installs_portable_and_reports_progress(fake, tmp_path):
    c = cfg()
    c.storage.data_dir = str(tmp_path)
    r = so.SetupRunner()
    assert r.start(lambda: c)
    r._thread.join(10)
    assert r.state["done"] and r.state["error"] is None and r.state["step"] == "done"
    assert fake["calls"] == ["install_portable", "start:attached", "pull:m1", "pull:emb"]


def test_runner_refuses_second_run_and_reports_errors(fake, monkeypatch):
    def boom(url, log_dir, wait_s=45, binary=None, attached=False):
        raise so.SetupError("no start")

    fake.update(installed=True)
    monkeypatch.setattr(so, "start", boom)
    r = so.SetupRunner()
    r.state["running"] = True
    assert not r.start(cfg)
    r.state["running"] = False
    assert r.start(cfg)
    r._thread.join(10)
    assert r.state["done"] and r.state["error"] == "no start"
