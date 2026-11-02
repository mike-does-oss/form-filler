"""One-time setup: install Ollama if missing, start it, and pull the configured models.

This is the only code that reaches the internet, and only when the user runs
`form-filler setup`, agrees to the prompt on `form-filler serve`, or presses "Set up" on the
app's first-run screen. The app itself makes no outbound calls at runtime.

Two ways to install Ollama:
  * system install (CLI `setup`): Homebrew / official app on macOS, install.sh on Linux, winget
    on Windows.
  * portable (desktop app and `setup --portable`): the official release archive is unpacked
    into <data_dir>/ollama. No admin rights, nothing outside the app's own folder, removed with
    the app's data. Models still live in the standard ~/.ollama so an existing install shares them.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx

from app.config import Config

MAC_APP_ZIP = "https://ollama.com/download/Ollama-darwin.zip"
LINUX_INSTALL = "curl -fsSL https://ollama.com/install.sh | sh"
MAC_APP_DIRS = [Path("/Applications/Ollama.app"), Path.home() / "Applications" / "Ollama.app"]
RELEASE_BASE = "https://github.com/ollama/ollama/releases/latest/download/"
PORTABLE_ASSETS = {
    ("Darwin", "arm64"): "ollama-darwin.tgz",
    ("Darwin", "x86_64"): "ollama-darwin.tgz",
    ("Linux", "x86_64"): "ollama-linux-amd64.tgz",
    ("Linux", "aarch64"): "ollama-linux-arm64.tgz",
    ("Windows", "AMD64"): "ollama-windows-amd64.zip",
    ("Windows", "ARM64"): "ollama-windows-arm64.zip",
}

# Progress callback: (message, fraction 0..1 or None)
Progress = Callable[[str, float | None], None]


def _print_progress(text: str, frac: float | None) -> None:
    pct = f" {frac * 100:.0f}%" if frac is not None else ""
    print(f"\r  {text}{pct:<8}", end="", flush=True)


class SetupError(RuntimeError):
    pass


# ------------------------------------------------------------------ detection
def is_local(base_url: str) -> bool:
    return (urlparse(base_url).hostname or "") in ("localhost", "127.0.0.1", "::1")


def mac_app() -> Path | None:
    return next((p for p in MAC_APP_DIRS if p.exists()), None)


def portable_dir(cfg: Config) -> Path:
    return cfg.data_path / "ollama"


def portable_binary(cfg: Config) -> Path | None:
    root = portable_dir(cfg)
    if not root.is_dir():
        return None
    name = "ollama.exe" if platform.system() == "Windows" else "ollama"
    for cand in (root / name, root / "bin" / name):
        if cand.is_file():
            return cand
    return next((p for p in root.rglob(name) if p.is_file()), None)


def ollama_binary(cfg: Config | None = None) -> str | None:
    if cfg is not None:
        portable = portable_binary(cfg)
        if portable:
            return str(portable)
    found = shutil.which("ollama")
    if found:
        return found
    app = mac_app()
    if app and (app / "Contents/Resources/ollama").exists():
        return str(app / "Contents/Resources/ollama")
    # Windows: winget installs here, and the current shell's PATH isn't refreshed until restart
    local = os.environ.get("LOCALAPPDATA")
    if local and (Path(local) / "Programs/Ollama/ollama.exe").exists():
        return str(Path(local) / "Programs/Ollama/ollama.exe")
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
        "installed": ollama_binary(cfg) is not None,
        "portable": portable_binary(cfg) is not None,
        "running": up,
        "remote": not is_local(cfg.llm.base_url),
        "base_url": cfg.llm.base_url,
        "missing_models": missing_models(cfg) if up else required_models(cfg),
    }


def total_ram_gb() -> float | None:
    try:
        if platform.system() == "Windows":

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MemoryStatus()
            stat.dwLength = ctypes.sizeof(stat)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
            return round(stat.ullTotalPhys / 2**30, 1)
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (AttributeError, ValueError, OSError):
        return None


# Models offered on the first-run screen. download_gb is the pull size; min_ram_gb is a
# comfortable total for model + KV cache + the rest of the machine.
MODEL_CATALOG = [
    {"name": "gemma3:4b", "label": "Small", "download_gb": 3.3, "min_ram_gb": 8,
     "blurb": "Fastest. Good on simple forms; check more fields by hand."},
    {"name": "qwen2.5:7b-instruct", "label": "Medium", "download_gb": 4.7, "min_ram_gb": 16,
     "blurb": "Good balance of accuracy and speed on a laptop."},
    {"name": "qwen2.5:14b-instruct", "label": "Large", "download_gb": 9.0, "min_ram_gb": 32,
     "blurb": "Most accurate. Needs a well-equipped machine or a GPU."},
]


def recommended_model(ram_gb: float | None) -> str:
    if ram_gb is None:
        return MODEL_CATALOG[1]["name"]
    fits = [m for m in MODEL_CATALOG if ram_gb >= m["min_ram_gb"]]
    return (fits[-1] if fits else MODEL_CATALOG[0])["name"]


# ------------------------------------------------------------------ actions
def install() -> None:
    """System-wide install via the platform's package manager / official installer."""
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


def portable_asset() -> str:
    key = (platform.system(), platform.machine())
    try:
        return PORTABLE_ASSETS[key]
    except KeyError:
        raise SetupError(f"No portable Ollama build for {key[0]} {key[1]}; install it from https://ollama.com/download") from None


def download(url: str, dest: Path, progress: Progress | None = None, label: str = "Downloading") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60, read=600)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0
        last = -1.0
        with open(tmp, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    frac = done / total
                    if frac - last >= 0.005:
                        progress(f"{label} ({done / 2**30:.2f} of {total / 2**30:.2f} GB)", frac)
                        last = frac
    tmp.replace(dest)


def extract_archive(archive: Path, dest: Path) -> None:
    """Unpack a release archive, refusing entries that would land outside dest."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def check(name: str) -> None:
        if not (root / name).resolve().is_relative_to(root):
            raise SetupError(f"unsafe path in archive: {name}")

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                check(info.filename)
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            for m in t.getmembers():
                check(m.name)
            t.extractall(dest, filter="data")


def install_portable(cfg: Config, progress: Progress | None = None) -> Path:
    """Download the official release archive into <data_dir>/ollama and return the binary."""
    asset = portable_asset()
    dest = portable_dir(cfg)
    if dest.exists():
        shutil.rmtree(dest)
    archive = cfg.data_path / "downloads" / asset
    download(RELEASE_BASE + asset, archive, progress, label=f"Downloading Ollama ({asset})")
    if progress:
        progress("Unpacking Ollama", None)
    extract_archive(archive, dest)
    archive.unlink(missing_ok=True)
    binary = portable_binary(cfg)
    if not binary:
        raise SetupError(f"{asset} did not contain an ollama binary")
    binary.chmod(binary.stat().st_mode | 0o111)
    return binary


_started: subprocess.Popen | None = None
_started_lock = threading.Lock()


def start(base_url: str, log_dir: Path, wait_s: float = 45, binary: str | None = None, attached: bool = False) -> None:
    """Start the Ollama server. `attached` keeps it as our child so stop_started() can end it."""
    global _started
    app = mac_app()
    if platform.system() == "Darwin" and app and not binary:
        _run(["open", "-g", str(app)])  # the menu-bar app runs the server
    else:
        binary = binary or ollama_binary()
        if not binary:
            raise SetupError("Ollama is not installed.")
        log_dir.mkdir(parents=True, exist_ok=True)
        log = open(log_dir / "ollama.log", "ab")  # noqa: SIM115 - handed to the child process
        env = {**os.environ, "OLLAMA_HOST": urlparse(base_url).netloc or "127.0.0.1:11434"}
        proc = subprocess.Popen([binary, "serve"], stdout=log, stderr=log, env=env, start_new_session=not attached)
        if attached:
            with _started_lock:
                _started = proc
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if server_up(base_url):
            return
        time.sleep(1)
    raise SetupError(f"Ollama did not start at {base_url} within {wait_s:.0f}s.")


def stop_started() -> None:
    """Stop an Ollama server this process started (frees the model's memory on exit)."""
    global _started
    with _started_lock:
        proc, _started = _started, None
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def pull(base_url: str, model: str, progress: Progress | None = None) -> None:
    progress = progress or _print_progress
    last = ""
    with httpx.stream("POST", f"{base_url.rstrip('/')}/api/pull", json={"model": model}, timeout=None) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("error"):
                raise SetupError(f"pull {model}: {msg['error']}")
            text = f"{model}: {msg.get('status', '')}"
            frac = None
            if msg.get("total") and msg.get("completed") is not None:
                frac = msg["completed"] / msg["total"]
                text = f"{model}: {msg.get('status', '')} ({msg['completed'] / 2**30:.2f} of {msg['total'] / 2**30:.2f} GB)"
            if text != last or frac is not None:
                progress(text, frac)
                last = text
    if progress is _print_progress:
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


# ------------------------------------------------------------------ entry points
def ensure(cfg: Config, *, assume_yes: bool = False, portable: bool = False) -> bool:
    """CLI flow: install/start Ollama and pull models as needed. Returns True when ready."""
    if cfg.llm.backend != "ollama":
        print(f"llm.backend is {cfg.llm.backend!r}; nothing to set up for Ollama.")
        return True
    url = cfg.llm.base_url
    if not is_local(url):
        print(f"Ollama at {url} is remote; install and start it on that machine.")
        return server_up(url)

    if not server_up(url):
        binary = ollama_binary(cfg)
        if binary is None:
            if not _confirm("Ollama is not installed. Install it now?", assume_yes):
                print("Skipped. Install it from https://ollama.com/download or run `form-filler setup`.")
                return False
            if portable:
                binary = str(install_portable(cfg, _print_progress))
                print()
            else:
                install()
        print("Starting Ollama ...")
        start(url, cfg.data_path / "logs", binary=binary if portable_binary(cfg) else None)

    missing = missing_models(cfg)
    if missing:
        if not _confirm(f"Download missing models ({', '.join(missing)})? This can take several GB.", assume_yes):
            print("Skipped. Pull them with: " + " && ".join(f"ollama pull {m}" for m in missing))
            return False
        for m in missing:
            pull(url, m)
    print(f"Ollama ready at {url} with {', '.join(required_models(cfg))}.")
    return True


class SetupRunner:
    """Background setup for the UI: one run at a time, progress polled over HTTP.

    Always installs the portable build when Ollama is missing (no admin prompt possible from a
    web page) and keeps a server it started attached so it stops with the app.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state: dict = {"running": False, "done": False, "error": None, "step": None, "detail": "", "fraction": None}

    def _progress(self, text: str, frac: float | None) -> None:
        self.state.update(detail=text, fraction=frac)

    def start(self, get_cfg: Callable[[], Config]) -> bool:
        with self._lock:
            if self.state["running"]:
                return False
            self.state = {"running": True, "done": False, "error": None, "step": "starting", "detail": "", "fraction": None}
            self._thread = threading.Thread(target=self._run, args=(get_cfg,), name="setup", daemon=True)
            self._thread.start()
            return True

    def _run(self, get_cfg: Callable[[], Config]) -> None:
        try:
            cfg = get_cfg()
            url = cfg.llm.base_url
            if cfg.llm.backend == "ollama" and is_local(url):
                if not server_up(url):
                    binary = ollama_binary(cfg)
                    if binary is None:
                        self.state.update(step="install", detail="Downloading Ollama", fraction=None)
                        binary = str(install_portable(cfg, self._progress))
                    self.state.update(step="start", detail="Starting Ollama", fraction=None)
                    start(url, cfg.data_path / "logs", binary=binary, attached=True)
                for m in missing_models(cfg):
                    self.state.update(step="pull", detail=f"Downloading {m}", fraction=None)
                    pull(url, m, self._progress)
            elif not server_up(url):
                raise SetupError(f"Model server at {url} is not reachable.")
            self.state.update(step="done", detail="Ready", fraction=1.0)
        except Exception as e:  # noqa: BLE001
            self.state.update(error=str(e), detail=str(e))
        finally:
            self.state.update(running=False, done=True)


runner = SetupRunner()
