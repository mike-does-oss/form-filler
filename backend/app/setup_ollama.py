"""One-time setup: install Ollama if missing, start it, and pull the configured models.

This is the only code that reaches the internet, and only when the user runs
`form-filler setup` or agrees to the prompt on `form-filler serve`. The app itself
makes no outbound calls at runtime.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from app.config import Config

MAC_APP_ZIP = "https://ollama.com/download/Ollama-darwin.zip"
LINUX_INSTALL = "curl -fsSL https://ollama.com/install.sh | sh"
MAC_APP_DIRS = [Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app"]


class SetupError(RuntimeError):
    pass


# ------------------------------------------------------------------ detection
def is_local(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def mac_app() -> Path | None:
    return next((p for p in MAC_APP_DIRS if p.exists()), None)


def ollama_binary() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    app = mac_app()
    if app and (app / "Contents/Resources/ollama").exists():
        return str(app / "Contents/Resources/ollama")
    return None


def server_up(base_url: str) -> bool:
    try:
        return httpx.get(f"{base_url.rstrip('/')}/api/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def local_models(base_url: str) -> set[str]:
    r = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=10)
    r.raise_for_status()
    names = set()
    for m in r.json().get("models", []):
        names.add(m["name"])
        if m["name"].endswith(":latest"):
            names.add(m["name"].removesuffix(":latest"))
    return names


def required_models(cfg: Config) -> list[str]:
    models = [cfg.llm.model, cfg.judgment_model, cfg.embeddings.model]
    if cfg.vision.enabled:
        models.append(cfg.vision.model)
    return list(dict.fromkeys(models))


def missing_models(cfg: Config) -> list[str]:
    have = local_models(cfg.llm.base_url)
    return [m for m in required_models(cfg) if m not in have and f"{m}:latest" not in have]


def status(cfg: Config) -> dict:
    up = server_up(cfg.llm.base_url)
    return {
        "installed": ollama_binary() is not None,
        "running": up,
        "missing_models": missing_models(cfg) if up else required_models(cfg),
    }


# ------------------------------------------------------------------ actions
def install() -> None:
    system = platform.system()
    if system == "Darwin":
        if shutil.which("brew"):
            _run(["brew", "install", "ollama"])
            return
        dest = Path("/Applications") if os.access("/Applications", os.W_OK) else Path.home() / "Applications"
        dest.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as td:
            zip_path = Path(td) / "Ollama-darwin.zip"
            _run(["curl", "-fL", "--progress-bar", "-o", str(zip_path), MAC_APP_ZIP])
            _run(["ditto", "-xk", str(zip_path), str(dest)])
        return
    if system == "Linux":
        _run(["sh", "-c", LINUX_INSTALL])
        return
    if system == "Windows":
        if not shutil.which("winget"):
            raise SetupError("Install Ollama from https://ollama.com/download, then re-run `form-filler setup`.")
        _run(["winget", "install", "--id", "Ollama.Ollama", "-e"])
        return
    raise SetupError(f"Don't know how to install Ollama on {system}; see https://ollama.com/download")


def start(base_url: str, log_dir: Path, wait_s: float = 45) -> None:
    app = mac_app()
    if platform.system() == "Darwin" and app:
        _run(["open", "-g", str(app)])  # the menu-bar app runs the server
    else:
        binary = ollama_binary()
        if not binary:
            raise SetupError("Ollama is not installed.")
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "ollama.log", "ab")  # noqa: SIM115 - handed to the child process
        env = {**os.environ, "OLLAMA_HOST": urlparse(base_url).netloc or "127.0.0.1:11434"}
        subprocess.Popen([binary, "serve"], stdout=log, stderr=log, env=env, start_new_session=True)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if server_up(base_url):
            return
        time.sleep(1)
    raise SetupError(f"Ollama did not start at {base_url} within {wait_s:.0f}s.")


def pull(base_url: str, model: str) -> None:
    last = ""
    with httpx.stream("POST", f"{base_url.rstrip('/')}/api/pull", json={"model": model}, timeout=None) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("error"):
                raise SetupError(f"pull {model}: {msg['error']}")
            text = msg.get("status", "")
            if msg.get("total") and msg.get("completed") is not None:
                text = f"{text} {100 * msg['completed'] / msg['total']:.0f}%"
            if text != last:
                print(f"\r  {model}: {text:<60}", end="", flush=True)
                last = text
    print()


def _run(cmd: list[str]) -> None:
    print("  $ " + " ".join(cmd), flush=True)
    if subprocess.run(cmd).returncode != 0:
        raise SetupError(f"command failed: {' '.join(cmd)}")


def _confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [Y/n] ").strip().lower() in ("", "y", "yes")


# ------------------------------------------------------------------ entry point
def ensure(cfg: Config, *, assume_yes: bool = False) -> bool:
    """Install/start Ollama and pull models as needed. Returns True when ready."""
    if cfg.llm.backend != "ollama":
        print(f"llm.backend is {cfg.llm.backend!r}; nothing to set up for Ollama.")
        return True
    url = cfg.llm.base_url
    if not is_local(url):
        print(f"Ollama at {url} is remote; install and start it on that machine.")
        return server_up(url)

    if not server_up(url):
        if ollama_binary() is None:
            if not _confirm("Ollama is not installed. Install it now?", assume_yes):
                print("Skipped. Install it from https://ollama.com/download or run `form-filler setup`.")
                return False
            install()
        print("Starting Ollama ...")
        start(url, cfg.data_path / "logs")

    missing = missing_models(cfg)
    if missing:
        if not _confirm(f"Download missing models ({', '.join(missing)})? This can take several GB.", assume_yes):
            print("Skipped. Pull them with: " + " && ".join(f"ollama pull {m}" for m in missing))
            return False
        for m in missing:
            pull(url, m)
    print(f"Ollama ready at {url} with {', '.join(required_models(cfg))}.")
    return True
