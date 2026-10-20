"""Exports: filled form (original format), flattened PDF, JSON with evidence, CSV."""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from app import db
from app.forms.service import get_fields
from app.jobs.service import job_dir, list_values, resolved_values
from app.verify.normalize import format_for_form


class ExportError(RuntimeError):
    pass


def _form_for_job(job_id: int) -> dict:
    row = db.q1("SELECT f.* FROM jobs j JOIN forms f ON f.id = j.form_id WHERE j.id = ?", (job_id,))
    if not row:
        raise KeyError(job_id)
    return row


def export_filled(job_id: int, flatten: bool = False) -> tuple[Path, list[str]]:
    form = _form_for_job(job_id)
    fields = get_fields(form["id"])
    values = resolved_values(job_id)
    src = Path(form["stored_path"])
    stem = Path(form["filename"]).stem
    out_dir = job_dir(job_id) / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ft = form["form_type"]
    if ft == "acroform":
        from app.writeback.acroform import fill_acroform

        dest = out_dir / f"{stem}_{'flattened' if flatten else 'filled'}.pdf"
        return dest, fill_acroform(src, dest, fields, values, flatten=flatten)
    if ft == "flat_pdf":
        from app.writeback.flat_pdf import fill_flat_pdf

        dest = out_dir / f"{stem}_filled.pdf"
        return dest, fill_flat_pdf(src, dest, fields, values)
    if ft == "docx":
        from app.writeback.docx_form import fill_docx

        dest = out_dir / f"{stem}_filled.docx"
        warnings = fill_docx(src, dest, fields, values)
        if not flatten:
            return dest, warnings
        pdf = docx_to_pdf(dest)
        return pdf, warnings
    raise ExportError(f"unknown form type {ft}")


def docx_to_pdf(path: Path) -> Path:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise ExportError("Flattened PDF export of Word forms needs LibreOffice (soffice) installed locally.")
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf", "--outdir", td, str(path)],
            check=True,
            capture_output=True,
            timeout=180,
        )
        out = Path(td) / (path.stem + ".pdf")
        dest = path.with_name(path.stem + "_flattened.pdf")
        shutil.copyfile(out, dest)
    return dest


def export_json(job_id: int) -> bytes:
    values = list_values(job_id)
    resolved = resolved_values(job_id)
    docs = {r["id"]: r["filename"] for r in db.q("SELECT id, filename FROM documents WHERE job_id = ?", (job_id,))}
    out = []
    for v in values:
        for e in v["evidence"]:
            e["document"] = docs.get(e.get("document_id"))
        out.append(
            {
                "key": v["key"],
                "label": v["label"],
                "type": v["type"],
                "kind": v["kind"],
                "value": resolved.get(v["key"]),
                "model_value": v["value"],
                "status": v["status"],
                "confidence": v["confidence"],
                "review_status": v["review_status"],
                "evidence": v["evidence"],
                "candidates": v["candidates"],
                "validator_errors": v["validator_errors"],
                "derivation": v["derivation"],
                "reasoning": v["reasoning"],
                "facts": v["facts"],
                "missing_facts": v["missing_facts"],
                "run_agreement": v["run_agreement"],
                "requires_manual_signing": v["requires_manual_signing"],
            }
        )
    return json.dumps({"job_id": job_id, "fields": out}, indent=2, ensure_ascii=False, default=str).encode()


def export_csv(job_id: int) -> bytes:
    values = list_values(job_id)
    resolved = resolved_values(job_id)
    docs = {r["id"]: r["filename"] for r in db.q("SELECT id, filename FROM documents WHERE job_id = ?", (job_id,))}
    fields = {f.key: f for f in get_fields(_form_for_job(job_id)["id"])}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["key", "label", "value", "status", "confidence", "review_status", "quote", "document", "page"])
    for v in values:
        ev = next((e for e in v["evidence"] if e.get("verified")), v["evidence"][0] if v["evidence"] else {})
        val = resolved.get(v["key"])
        w.writerow(
            [
                v["key"],
                v["label"],
                format_for_form(fields[v["key"]], val) if val is not None else "",
                v["status"],
                v["confidence"],
                v["review_status"],
                ev.get("quote", ""),
                docs.get(ev.get("document_id"), ""),
                ev.get("page", ""),
            ]
        )
    return buf.getvalue().encode()


def preview_pages(job_id: int, page: int, dpi: int = 110) -> bytes:
    import pymupdf as fitz

    form = _form_for_job(job_id)
    if form["form_type"] == "docx":
        raise ExportError("use the HTML preview for Word forms")
    path, _ = export_filled(job_id, flatten=form["form_type"] == "acroform")
    doc = fitz.open(path)
    if page < 1 or page > len(doc):
        raise FileNotFoundError(page)
    return doc[page - 1].get_pixmap(dpi=dpi).tobytes("png")


def preview_html(job_id: int) -> str:
    from app.ingest.normalize import docx_to_html

    path, _ = export_filled(job_id)
    if path.suffix != ".docx":
        raise ExportError("HTML preview is only for Word forms")
    return docx_to_html(path)
