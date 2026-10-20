"""Command line: serve, health, delete-job, batch."""

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
    sub.add_parser("serve", help="run the web app")
    sub.add_parser("health", help="check model server, models and OCR")
    d = sub.add_parser("delete-job", help="delete a job's files, chunks, embeddings and values")
    d.add_argument("job_id", type=int)
    b = sub.add_parser("batch", help="fill one form against many source-document sets")
    b.add_argument("--form", required=True, help="blank form (.pdf/.docx)")
    b.add_argument("--sets", required=True, help="directory with one sub-directory of source documents per set")
    b.add_argument("--out", required=True, help="output directory")
    b.add_argument("--format", default="filled", choices=["filled", "flattened", "json", "csv"])
    args = ap.parse_args(argv)

    if args.config:
        os.environ["FORMFILLER_CONFIG"] = str(Path(args.config).resolve())
    from app.config import get_config

    cfg = get_config()

    if args.cmd == "serve":
        import uvicorn

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
        return _batch(Path(args.form), Path(args.sets), Path(args.out), args.format)
    return 1


def _batch(form: Path, sets_dir: Path, out: Path, fmt: str) -> int:
    import shutil
    import uuid

    from app import db
    from app.forms.service import create_form
    from app.ingest.normalize import SUPPORTED_EXT, guess_mime
    from app.jobs.pipeline import run_job
    from app.jobs.service import job_dir
    from app.writeback.export import export_csv, export_filled, export_json

    out.mkdir(parents=True, exist_ok=True)
    form_id = create_form(form, form.name)
    batch_id = uuid.uuid4().hex[:12]
    sets = sorted(p for p in sets_dir.iterdir() if p.is_dir())
    t0 = time.monotonic()
    pages = 0
    for s in sets:
        job_id = db.insert("jobs", {"form_id": form_id, "status": "created", "batch_id": batch_id})
        for f in sorted(s.iterdir()):
            if f.suffix.lower() not in SUPPORTED_EXT:
                continue
            doc_id = db.insert("documents", {"job_id": job_id, "filename": f.name, "stored_path": "", "mime": guess_mime(f.name)})
            dest = job_dir(job_id) / "docs" / f"{doc_id}{f.suffix.lower()}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dest)
            db.ex("UPDATE documents SET stored_path = ? WHERE id = ?", (str(dest), doc_id))
        stats = run_job(job_id)
        pages += stats.get("pages", 0)
        if fmt == "json":
            (out / f"{s.name}.json").write_bytes(export_json(job_id))
        elif fmt == "csv":
            (out / f"{s.name}.csv").write_bytes(export_csv(job_id))
        else:
            path, warnings = export_filled(job_id, flatten=fmt == "flattened")
            shutil.copyfile(path, out / f"{s.name}{path.suffix}")
        print(f"{s.name}: job {job_id} done in {stats.get('total_s')}s")
    el = time.monotonic() - t0
    print(f"{len(sets)} sets, {pages} pages in {el:.1f}s -> {len(sets) / el * 60:.2f} forms/min, {pages / el * 60:.1f} pages/min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
