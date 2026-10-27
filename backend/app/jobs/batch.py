"""Batch fill: one form, one job per folder of source documents.

Input layout (what the user picks or passes to the CLI):

    clients/                 <- the batch folder
      alice/                 <- one set = one filled form
        payslip.pdf
        2024/licence.png     <- nested folders belong to their top-level set
      bob/
        ...

Output layout (zip download or CLI --out):

    <batch name>/
      summary.csv            <- one row per set: status, fields needing review, every value
      alice/
        <form>_filled.pdf    <- or .docx
        values.json          <- values with source quotes
        values.csv
      bob/
        ...
"""

from __future__ import annotations

import csv
import io
import re
import shutil
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from app import db
from app.ingest.normalize import SUPPORTED_EXT, guess_mime
from app.jobs.service import delete_job, job_dir, list_values, resolved_values

JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini"}
NEEDS_REVIEW_STATUSES = ("conflict", "unverified", "cannot_determine", "judged")


class BatchError(ValueError):
    pass


# ------------------------------------------------------------------ grouping
def group_files(relpaths: list[str]) -> tuple[dict[str, list[int]], list[dict]]:
    """Group paths like "alice/payslip.pdf" by their first folder.

    Returns ({set name: [indexes into relpaths]}, [{path, reason} for skipped files]).
    Paths must be relative to the batch folder, i.e. the first part is the set.
    """
    sets: dict[str, list[int]] = {}
    skipped: list[dict] = []
    for i, raw in enumerate(relpaths):
        parts = [p for p in PurePosixPath(raw.replace("\\", "/")).parts if p not in ("", ".", "/")]
        if not parts or ".." in parts:
            skipped.append({"path": raw, "reason": "invalid path"})
            continue
        name = parts[-1]
        if name.lower() in JUNK_FILES or any(p.startswith(".") for p in parts):
            continue  # hidden/system files: skip silently
        if len(parts) < 2:
            skipped.append({"path": raw, "reason": "not inside a sub-folder"})
            continue
        if Path(name).suffix.lower() not in SUPPORTED_EXT:
            skipped.append({"path": raw, "reason": "unsupported file type"})
            continue
        sets.setdefault(parts[0], []).append(i)
    return dict(sorted(sets.items(), key=lambda kv: _natural_key(kv[0]))), skipped


def scan_folder(root: Path) -> tuple[dict[str, list[Path]], list[dict]]:
    files = sorted(p for p in root.rglob("*") if p.is_file())
    rel = [p.relative_to(root).as_posix() for p in files]
    groups, skipped = group_files(rel)
    return {k: [files[i] for i in idx] for k, idx in groups.items()}, skipped


def _natural_key(s: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def inner_name(relpath: str) -> str:
    """Display name of a document inside its set: "alice/2024/x.pdf" -> "2024/x.pdf"."""
    parts = PurePosixPath(relpath.replace("\\", "/")).parts
    return "/".join(parts[1:]) if len(parts) > 1 else parts[-1]


# ------------------------------------------------------------------ create
def create_batch(form_id: int, labels: list[str], name: str | None = None) -> dict:
    if not db.q1("SELECT id FROM forms WHERE id = ?", (form_id,)):
        raise BatchError("form not found")
    labels = [lb.strip() for lb in labels]
    if not labels or any(not lb for lb in labels):
        raise BatchError("every set needs a name")
    if len(set(labels)) != len(labels):
        raise BatchError("set names must be unique")
    batch_id = uuid.uuid4().hex[:12]
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO batches (id, form_id, name) VALUES (?, ?, ?)",
            (batch_id, form_id, (name or "").strip() or f"batch-{batch_id[:6]}"),
        )
        for lb in labels:
            conn.execute(
                "INSERT INTO jobs (form_id, status, batch_id, label) VALUES (?, 'created', ?, ?)", (form_id, batch_id, lb)
            )
    return batch_out(batch_id)


def add_document(job_id: int, display_name: str, src: Path | None = None) -> tuple[int, Path]:
    """Register a document; copies `src` if given, else returns the path to write to."""
    ext = Path(display_name).suffix.lower()
    doc_id = db.insert("documents", {"job_id": job_id, "filename": display_name, "stored_path": "", "mime": guess_mime(display_name)})
    dest = job_dir(job_id) / "docs" / f"{doc_id}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src is not None:
        shutil.copyfile(src, dest)
    db.ex("UPDATE documents SET stored_path = ? WHERE id = ?", (str(dest), doc_id))
    return doc_id, dest


def create_batch_from_folder(form_id: int, root: Path, name: str | None = None) -> tuple[dict, list[dict]]:
    groups, skipped = scan_folder(root)
    if not groups:
        raise BatchError(f"no sub-folders with supported documents in {root}")
    batch = create_batch(form_id, list(groups), name or root.name)
    for job in batch["jobs"]:
        for f in groups[job["label"]]:
            add_document(job["id"], inner_name(f.relative_to(root).as_posix()), f)
    return batch_out(batch["id"]), skipped


# ------------------------------------------------------------------ status
def _get(batch_id: str) -> dict:
    b = db.q1("SELECT * FROM batches WHERE id = ?", (batch_id,))
    if b:
        return b
    j = db.q1("SELECT form_id, MIN(created_at) AS created_at FROM jobs WHERE batch_id = ?", (batch_id,))
    if not j or j["form_id"] is None:  # older count-only batches have no batches row
        raise KeyError(batch_id)
    return {"id": batch_id, "form_id": j["form_id"], "name": f"batch-{batch_id[:6]}", "created_at": j["created_at"]}


def _job_counts(job_ids: list[int]) -> dict[int, dict]:
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    rows = db.q(
        f"""SELECT fv.job_id AS job_id,
                   COUNT(*) AS total,
                   SUM(CASE WHEN COALESCE(final_value, value) IS NOT NULL AND COALESCE(final_value, value) != '' THEN 1 ELSE 0 END) AS filled,
                   SUM(CASE WHEN review_status = 'pending' AND f.type != 'signature'
                             AND (status IN ({",".join("?" * len(NEEDS_REVIEW_STATUSES))}) OR confidence = 'low') THEN 1 ELSE 0 END) AS needs_review,
                   SUM(CASE WHEN review_status != 'pending' THEN 1 ELSE 0 END) AS reviewed
            FROM field_values fv JOIN fields f ON f.id = fv.field_id
            WHERE fv.job_id IN ({marks}) GROUP BY fv.job_id""",
        (*NEEDS_REVIEW_STATUSES, *job_ids),
    )
    return {r["job_id"]: r for r in rows}


def batch_out(batch_id: str) -> dict:
    b = _get(batch_id)
    form = db.q1("SELECT id, filename, form_type FROM forms WHERE id = ?", (b["form_id"],))
    rows = db.q(
        """SELECT j.id, j.label, j.status, j.progress, j.stage, j.error, j.started_at, j.finished_at, j.stats_json,
                  (SELECT COUNT(*) FROM documents d WHERE d.job_id = j.id) AS documents
           FROM jobs j WHERE j.batch_id = ? ORDER BY j.id""",
        (batch_id,),
    )
    counts = _job_counts([r["id"] for r in rows])
    jobs = []
    for i, r in enumerate(rows, 1):
        c = counts.get(r["id"], {})
        jobs.append(
            {
                "id": r["id"],
                "label": r["label"] or f"set {i}",
                "status": r["status"],
                "progress": r["progress"],
                "stage": r["stage"],
                "error": r["error"],
                "documents": r["documents"],
                "total_fields": c.get("total") or 0,
                "filled": c.get("filled") or 0,
                "needs_review": c.get("needs_review") or 0,
                "reviewed": c.get("reviewed") or 0,
            }
        )
    done = [r for r in rows if r["status"] == "done"]
    out: dict[str, Any] = {
        "id": batch_id,
        "batch_id": batch_id,  # kept for older clients
        "name": b["name"],
        "form": form,
        "created_at": b["created_at"],
        "jobs": jobs,
        "total": len(rows),
        "done": len(done),
        "failed": sum(r["status"] == "failed" for r in rows),
        "running": sum(r["status"] in ("queued", "running") for r in rows),
        "needs_review": sum(j["needs_review"] for j in jobs),
    }
    starts = [r["started_at"] for r in rows if r["started_at"]]
    ends = [r["finished_at"] for r in done if r["finished_at"]]
    if starts and ends:
        span = db.q1("SELECT (julianday(?) - julianday(?)) * 86400.0 AS s", (max(ends), min(starts)))["s"]
        pages = sum(db.loads(r["stats_json"], {}).get("pages", 0) for r in done)
        out["elapsed_s"] = round(span, 1)
        out["forms_per_minute"] = round(len(done) / span * 60, 2) if span else None
        out["pages_per_minute"] = round(pages / span * 60, 2) if span else None
    return out


def list_batches(limit: int = 50) -> list[dict]:
    ids = [r["id"] for r in db.q("SELECT id FROM batches ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))]
    return [batch_out(i) for i in ids]


def batch_job_ids(batch_id: str) -> list[int]:
    return [r["id"] for r in db.q("SELECT id FROM jobs WHERE batch_id = ? ORDER BY id", (batch_id,))]


def delete_batch(batch_id: str) -> None:
    for job_id in batch_job_ids(batch_id):
        delete_job(job_id)
    db.ex("DELETE FROM batches WHERE id = ?", (batch_id,))


# ------------------------------------------------------------------ export
def safe_name(s: str) -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s).strip(" .")
    return s or "unnamed"


def write_outputs(batch_id: str, dest: Path, flatten: bool = False) -> Path:
    """Write <dest>/<set>/... plus <dest>/summary.csv. Returns dest."""
    from app.writeback.export import ExportError, export_csv, export_filled, export_json

    b = batch_out(batch_id)
    dest.mkdir(parents=True, exist_ok=True)
    fields: list[tuple[str, str]] = []  # (key, label) in form order, from the first finished set
    rows: list[dict] = []
    for job in b["jobs"]:
        folder = dest / safe_name(job["label"])
        row = {"set": job["label"], "status": job["status"], "needs_review": job["needs_review"], "filled": job["filled"], "note": ""}
        if job["status"] != "done":
            row["note"] = job["error"] or f"not finished ({job['status']})"
            rows.append(row)
            continue
        folder.mkdir(parents=True, exist_ok=True)
        try:
            path, warnings = export_filled(job["id"], flatten=flatten)
            shutil.copyfile(path, folder / path.name)
            if warnings:
                row["note"] = "; ".join(warnings)
        except ExportError as e:
            row["note"] = f"form not written: {e}"
        (folder / "values.json").write_bytes(export_json(job["id"]))
        (folder / "values.csv").write_bytes(export_csv(job["id"]))
        if not fields:
            fields = [(v["key"], v["label"]) for v in list_values(job["id"]) if v["type"] != "signature"]
        row["values"] = resolved_values(job["id"])
        rows.append(row)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["set", "status", "needs_review", "fields_filled", "note", *[label for _, label in fields]])
    for r in rows:
        vals = r.get("values", {})
        w.writerow([r["set"], r["status"], r["needs_review"], r["filled"], r["note"], *[_cell(vals.get(k)) for k, _ in fields]])
    (dest / "summary.csv").write_text(buf.getvalue(), encoding="utf-8-sig")  # BOM so Excel reads UTF-8
    return dest


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return str(v)


def export_zip(batch_id: str, dest_zip: Path, flatten: bool = False) -> Path:
    b = batch_out(batch_id)
    root = safe_name(b["name"])
    work = dest_zip.parent / f".{dest_zip.stem}-work"
    shutil.rmtree(work, ignore_errors=True)
    try:
        write_outputs(batch_id, work / root, flatten=flatten)
        with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted((work / root).rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(work).as_posix())
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return dest_zip
