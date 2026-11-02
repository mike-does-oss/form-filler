# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the backend used by the desktop app.
#   uv run pyinstaller desktop/backend.spec --noconfirm --distpath dist/backend
# produces dist/backend/form-filler/ (one folder, no Python install needed).

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
BACKEND = ROOT / "backend"

datas = [
    (str(BACKEND / "app" / "db" / "migrations"), "app/db/migrations"),
    (str(BACKEND / "app" / "ingest" / "models"), "app/ingest/models"),
]
datas += collect_data_files("rapidocr_onnxruntime")  # ONNX models + config.yaml
datas += collect_data_files("pymupdf")  # pymupdf.layout resources, loaded when pymupdf is imported

hiddenimports = collect_submodules("app") + [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "multipart",
    "python_multipart",
]

a = Analysis(
    [str(BACKEND / "desktop_entry.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "docling", "paddleocr", "torch"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="form-filler",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # keep stdout/stderr; the desktop shell hides the window and logs them
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="form-filler")
