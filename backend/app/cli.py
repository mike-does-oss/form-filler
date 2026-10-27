"""Command line: setup, serve, health, delete-job, batch."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="form-filler")
    ap.add_argument("--config", help="path to config.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("setup", help="install/start Ollama if missing and download the configured models")
    st.add_argument("-y", "--yes", action="store_true", help="don't ask before installing or downloading")
    sv = sub.add_parser("serve", help="run the web app (offers to set up Ollama first if it isn't running)")
    sv.add_argument("--skip-setup", action="store_true", help="don't check for Ollama before starting")
    sub.add_parser("health", help="check model server, models and OCR")
    d = sub.add_parser("delete-job", help="delete a job's files, chunks, embeddings and values")
    d.add_argument("job_id", type=int)
    b = sub.add_parser("batch", help="fill one form once per folder of source documents")
    b.add_argument("--form", required=True, help="blank form (.pdf/.docx)")
    b.add_argument("--sets", required=True, help="folder with one sub-folder of source documents per form to fill")
    b.add_argument("--out", required=True, help="output folder: one sub-folder per set plus summary.csv")
    b.add_argument("--format", default="filled", choices=["filled", "flattened"])
    b.add_argument("--name", help="batch name (default: the --sets folder name)")
    args = ap.parse_args(argv)

    if args.config:
        os.environ["FORMFILLER_CONFIG"] = str(Path(args.config).resolve())
    from app.config import get_config

    cfg = get_config()

    if args.cmd == "setup":
        from app.setup_ollama import SetupError, ensure

        try:
            return 0 if ensure(cfg, assume_yes=args.yes) else 1
        except SetupError as e:
            print(f"setup failed: {e}", file=sys.stderr)
            return 1
    if args.cmd == "serve":
        import uvicorn

        if not args.skip_setup:
            from app.setup_ollama import SetupError, ensure

            try:
                if not ensure(cfg):
                    print("Starting anyway; filling forms needs Ollama and the models.", file=sys.stderr)
            except SetupError as e:
                print(f"Ollama setup failed ({e}); starting anyway.", file=sys.stderr)

        if cfg.server.host not in ("127.0.0.1", "localhost", "::1"):
            print(f"WARNING: binding to {cfg.server.host}; the app has no authentication.", file=sys.stderr)
        uvicorn.run("app.main:app", host=cfg.server.host, port=cfg.server.port, log_level="info")
        return 0
    if args.cmd == "health":
        from app.api.routes import health

        out = health()
        print(json.dumps(out, indent=2))
        return 0 if out["status"] == "ok" else 1
    if args.cmd == "delete-job":
        from app.jobs.service import delete_job

        delete_job(args.job_id)
        print(f"deleted job {args.job_id}")
        return 0
    if args.cmd == "batch":
        return _batch(Path(args.form), Path(args.sets), Path(args.out), args.format, args.name)
    return 1


def _batch(form: Path, sets_dir: Path, out: Path, fmt: str, name: str | None) -> int:
    from app.forms.service import create_form
    from app.jobs import batch
    from app.jobs.pipeline import run_job

    if not sets_dir.is_dir():
        print(f"{sets_dir} is not a folder", file=sys.stderr)
        return 1
    form_id = create_form(form, form.name)
    try:
        b, skipped = batch.create_batch_from_folder(form_id, sets_dir, name)
    except batch.BatchError as e:
        print(str(e), file=sys.stderr)
        return 1
    for s in skipped:
        print(f"skipped {s['path']}: {s['reason']}", file=sys.stderr)
    print(f"batch {b['id']}: {len(b['jobs'])} sets from {sets_dir}")
    t0 = time.monotonic()
    pages = 0
    for job in b["jobs"]:
        try:
            stats = run_job(job["id"])
            pages += stats.get("pages", 0)
            print(f"  {job['label']}: done in {stats.get('total_s')}s")
        except Exception as e:  # noqa: BLE001 - one bad set shouldn't stop the batch
            print(f"  {job['label']}: FAILED ({e})", file=sys.stderr)
    el = time.monotonic() - t0
    batch.write_outputs(b["id"], out, flatten=fmt == "flattened")
    final = batch.batch_out(b["id"])
    print(f"{final['done']}/{final['total']} sets filled, {final['needs_review']} fields need review -> {out}/summary.csv")
    if el > 0:
        print(f"{pages} pages in {el:.1f}s ({final['done'] / el * 60:.2f} forms/min, {pages / el * 60:.1f} pages/min)")
    return 0 if final["failed"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
