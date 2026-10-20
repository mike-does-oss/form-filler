"""HTTP API (spec section 7 plus the endpoints the review UI needs)."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from app import db
from app.config import get_config
from app.extraction.llm_adapter import get_llm
from app.forms.service import create_form, get_fields, render_form_page, update_fields
from app.ingest.normalize import SUPPORTED_EXT, guess_mime
from app.ingest.ocr import ocr_status
from app.jobs import service as jobs
from app.jobs.queue import enqueue

router = APIRouter(prefix="/api")


# ------------------------------------------------------------------ helpers
def _form_out(form_id: int) -> dict:
    f = db.q1("SELECT * FROM forms WHERE id = ?", (form_id,))
    if not f:
        raise HTTPException(404, "form not found")
    tpl = db.q1("SELECT id, name, created_at FROM form_templates WHERE id = ?", (f["template_id"],)) if f["template_id"] else None
    return {
        "id": f["id"],
        "filename": f["filename"],
        "form_type": f["form_type"],
        "fingerprint": f["fingerprint"],
        "template_id": f["template_id"],
        "template_name": tpl["name"] if tpl else None,
        "page_count": f["page_count"],
        "created_at": f["created_at"],
        "fields": [x.model_dump() for x in get_fields(form_id)],
        "template_matched": bool(
            f["template_id"]
            and db.q1("SELECT 1 FROM forms WHERE template_id = ? AND id < ? LIMIT 1", (f["template_id"], form_id))
        ),
    }


def _job_out(job_id: int) -> dict:
    j = db.q1("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not j:
        raise HTTPException(404, "job not found")
    docs = db.q("SELECT * FROM documents WHERE job_id = ? ORDER BY id", (job_id,))
    counts = db.q1(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN review_status != 'pending' THEN 1 ELSE 0 END) AS reviewed
           FROM field_values WHERE job_id = ?""",
        (job_id,),
    )
    form = db.q1("SELECT id, filename, form_type, template_id FROM forms WHERE id = ?", (j["form_id"],))
    return {
        "id": j["id"],
        "form_id": j["form_id"],
        "form": form,
        "status": j["status"],
        "progress": j["progress"],
        "stage": j["stage"],
        "error": j["error"],
        "batch_id": j["batch_id"],
        "stats": db.loads(j["stats_json"], {}),
        "created_at": j["created_at"],
        "started_at": j["started_at"],
        "finished_at": j["finished_at"],
        "reviewed": counts["reviewed"] or 0,
        "total_fields": counts["total"] or 0,
        "documents": [_doc_out(d) for d in docs],
    }


def _doc_out(d: dict) -> dict:
    pages = db.q("SELECT page, width, height, ocr_confidence FROM page_spans WHERE document_id = ? ORDER BY page", (d["id"],))
    return {
        "id": d["id"],
        "filename": d["filename"],
        "mime": d["mime"],
        "page_count": d["page_count"],
        "ocr_used": bool(d["ocr_used"]),
        "status": d["status"],
        "pages": pages,
        "meta": db.loads(d["meta_json"], {}),
    }


async def _save_upload(upload: UploadFile, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as fh:
        while chunk := await upload.read(1 << 20):
            fh.write(chunk)


# ------------------------------------------------------------------ health
@router.get("/health")
def health() -> dict:
    cfg = get_config()
    llm = get_llm().health()
    ocr = ocr_status()
    models_ok = llm.get("reachable") and all(m["available"] for m in llm.get("models", {}).values())
    ocr_ok = ocr["tesseract"] or ocr["active_engine"] != "tesseract"
    return {
        "status": "ok" if models_ok and ocr_ok else "degraded",
        "llm": llm,
        "ocr": ocr,
        "storage": {"data_dir": str(cfg.data_path)},
        "config": {
            "llm_model": cfg.llm.model,
            "judgment_model": cfg.judgment_model,
            "embedding_model": cfg.embeddings.model,
            "vision_enabled": cfg.vision.enabled,
            "backend": cfg.llm.backend,
        },
    }


# ------------------------------------------------------------------ forms
@router.post("/forms")
async def upload_form(file: UploadFile = File(...)) -> dict:
    name = Path(file.filename or "form").name
    if Path(name).suffix.lower() not in (".pdf", ".docx"):
        raise HTTPException(400, "form must be a .pdf or .docx")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / name
        await _save_upload(file, tmp)
        try:
            form_id = await asyncio.to_thread(create_form, tmp, name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
    return _form_out(form_id)


@router.get("/forms")
def list_forms() -> list[dict]:
    return db.q("SELECT id, filename, form_type, template_id, page_count, created_at FROM forms ORDER BY id DESC")


@router.get("/forms/{form_id}")
def get_form(form_id: int) -> dict:
    return _form_out(form_id)


class FieldsUpdate(BaseModel):
    fields: list[dict[str, Any]]
    replace: bool = False


@router.put("/forms/{form_id}/fields")
def put_fields(form_id: int, body: FieldsUpdate) -> dict:
    try:
        update_fields(form_id, body.fields, replace=body.replace)
    except KeyError as e:
        raise HTTPException(404, "form not found") from e
    return _form_out(form_id)


@router.get("/forms/{form_id}/pages/{page}")
def form_page(form_id: int, page: int, dpi: int = 110) -> Response:
    try:
        return Response(render_form_page(form_id, page, dpi), media_type="image/png")
    except FileNotFoundError as e:
        raise HTTPException(404, "page not found") from e


@router.post("/forms/{form_id}/detect")
def redetect_flat(form_id: int) -> dict:
    """Re-run automatic input-area detection on a flat PDF (replaces the mapping)."""
    from app.forms.flat_pdf import parse_flat_pdf

    f = db.q1("SELECT * FROM forms WHERE id = ?", (form_id,))
    if not f or f["form_type"] != "flat_pdf":
        raise HTTPException(400, "only flat PDFs use detection")
    fields = parse_flat_pdf(Path(f["stored_path"]))
    update_fields(form_id, [x.model_dump() for x in fields], replace=True)
    return _form_out(form_id)


# ------------------------------------------------------------------ jobs
class JobCreate(BaseModel):
    form_id: int


@router.post("/jobs")
def create_job(body: JobCreate) -> dict:
    if not db.q1("SELECT id FROM forms WHERE id = ?", (body.form_id,)):
        raise HTTPException(404, "form not found")
    job_id = db.insert("jobs", {"form_id": body.form_id, "status": "created"})
    return _job_out(job_id)


@router.get("/jobs")
def list_jobs(limit: int = 50) -> list[dict]:
    rows = db.q(
        """SELECT j.id, j.form_id, j.status, j.progress, j.stage, j.created_at, j.batch_id, f.filename AS form_filename
           FROM jobs j JOIN forms f ON f.id = j.form_id ORDER BY j.id DESC LIMIT ?""",
        (limit,),
    )
    return rows


@router.get("/jobs/{job_id}")
def get_job(job_id: int) -> dict:
    return _job_out(job_id)


@router.delete("/jobs/{job_id}")
def delete_job(job_id: int) -> dict:
    if not db.q1("SELECT id FROM jobs WHERE id = ?", (job_id,)):
        raise HTTPException(404, "job not found")
    jobs.delete_job(job_id)
    return {"deleted": job_id}


@router.post("/jobs/{job_id}/documents")
async def upload_documents(job_id: int, files: list[UploadFile] = File(...)) -> list[dict]:
    if not db.q1("SELECT id FROM jobs WHERE id = ?", (job_id,)):
        raise HTTPException(404, "job not found")
    out = []
    for up in files:
        name = Path(up.filename or "document").name
        ext = Path(name).suffix.lower()
        if ext not in SUPPORTED_EXT:
            raise HTTPException(400, f"unsupported document type {ext}; supported: {sorted(SUPPORTED_EXT)}")
        doc_id = db.insert(
            "documents", {"job_id": job_id, "filename": name, "stored_path": "", "mime": guess_mime(name)}
        )
        dest = jobs.job_dir(job_id) / "docs" / f"{doc_id}{ext}"
        await _save_upload(up, dest)
        db.ex("UPDATE documents SET stored_path = ? WHERE id = ?", (str(dest), doc_id))
        out.append(_doc_out(db.q1("SELECT * FROM documents WHERE id = ?", (doc_id,))))
    return out


@router.delete("/jobs/{job_id}/documents/{doc_id}")
def delete_document(job_id: int, doc_id: int) -> dict:
    d = db.q1("SELECT * FROM documents WHERE id = ? AND job_id = ?", (doc_id, job_id))
    if not d:
        raise HTTPException(404, "document not found")
    db.ex("DELETE FROM documents WHERE id = ?", (doc_id,))
    for p in Path(d["stored_path"]).parent.glob(f"{doc_id}.*"):
        p.unlink(missing_ok=True)
    return {"deleted": doc_id}


@router.post("/jobs/{job_id}/run")
def run_job(job_id: int) -> dict:
    j = db.q1("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not j:
        raise HTTPException(404, "job not found")
    if j["status"] in ("queued", "running"):
        raise HTTPException(409, f"job is already {j['status']}")
    if not db.q1("SELECT id FROM documents WHERE job_id = ?", (job_id,)):
        raise HTTPException(400, "upload at least one source document first")
    enqueue(job_id)
    return _job_out(job_id)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: int) -> StreamingResponse:
    """Server-sent events with job progress until it finishes."""

    async def gen():
        last = None
        while True:
            j = db.q1("SELECT status, progress, stage, error FROM jobs WHERE id = ?", (job_id,))
            if j is None:
                yield f"event: error\ndata: {json.dumps({'error': 'job not found'})}\n\n"
                return
            if j != last:
                yield f"data: {json.dumps(j)}\n\n"
                last = j
            if j["status"] in ("done", "failed", "created"):
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/jobs/{job_id}/values")
def get_values(job_id: int) -> dict:
    try:
        values = jobs.list_values(job_id)
    except KeyError as e:
        raise HTTPException(404, "job not found") from e
    return {"job_id": job_id, "fields": values}


class ReviewAction(BaseModel):
    action: Literal["accept", "edit", "choose_candidate", "not_applicable", "reset"]
    value: Any = None
    candidate_index: int | None = None
    note: str | None = None


@router.patch("/jobs/{job_id}/values/{field_id}")
def patch_value(job_id: int, field_id: int, body: ReviewAction) -> dict:
    try:
        return jobs.apply_review(job_id, field_id, body.action, body.value, body.candidate_index, body.note)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


class FactAnswers(BaseModel):
    answers: dict[str, Any]


@router.post("/jobs/{job_id}/values/{field_id}/facts")
def post_fact_answers(job_id: int, field_id: int, body: FactAnswers) -> dict:
    try:
        return jobs.answer_facts(job_id, field_id, body.answers)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/jobs/{job_id}/audit")
def job_audit(job_id: int) -> list[dict]:
    return db.q("SELECT * FROM audit_log WHERE job_id = ? ORDER BY id", (job_id,))


class ExportReq(BaseModel):
    format: Literal["filled", "flattened", "json", "csv"]


@router.post("/jobs/{job_id}/export")
def export(job_id: int, body: ExportReq) -> Response:
    from app.writeback.export import ExportError, export_csv, export_filled, export_json

    if not db.q1("SELECT id FROM jobs WHERE id = ?", (job_id,)):
        raise HTTPException(404, "job not found")
    jobs.audit(job_id, None, f"export_{body.format}", None, None)
    try:
        if body.format == "json":
            return Response(export_json(job_id), media_type="application/json",
                            headers={"Content-Disposition": f'attachment; filename="job_{job_id}.json"'})
        if body.format == "csv":
            return Response(export_csv(job_id), media_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="job_{job_id}.csv"'})
        path, warnings = export_filled(job_id, flatten=body.format == "flattened")
    except ExportError as e:
        raise HTTPException(400, str(e)) from e
    media = "application/pdf" if path.suffix == ".pdf" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    headers = {"X-Export-Warnings": json.dumps(warnings)[:4000]}
    return FileResponse(path, media_type=media, filename=path.name, headers=headers)


@router.get("/jobs/{job_id}/preview/pages/{page}")
def preview_page(job_id: int, page: int, dpi: int = 110) -> Response:
    from app.writeback.export import ExportError, preview_pages

    try:
        return Response(preview_pages(job_id, page, dpi), media_type="image/png")
    except ExportError as e:
        raise HTTPException(400, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, "page not found") from e


@router.get("/jobs/{job_id}/preview/html")
def preview_html(job_id: int) -> HTMLResponse:
    from app.writeback.export import ExportError, preview_html as _ph

    try:
        return HTMLResponse(_ph(job_id))
    except ExportError as e:
        raise HTTPException(400, str(e)) from e


# ------------------------------------------------------------------ documents
@router.get("/documents/{doc_id}")
def get_document(doc_id: int) -> dict:
    d = db.q1("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if not d:
        raise HTTPException(404, "document not found")
    return _doc_out(d)


@router.get("/documents/{doc_id}/pages/{page}")
def document_page(doc_id: int, page: int, dpi: int = 110) -> Response:
    from app.ingest.pipeline import render_document_page

    try:
        png = render_document_page(doc_id, page, dpi)
    except FileNotFoundError as e:
        raise HTTPException(404, "page not found") from e
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


@router.get("/documents/{doc_id}/chunks")
def document_chunks(doc_id: int) -> list[dict]:
    return db.q("SELECT id, page, bbox_json, text, chunk_index FROM chunks WHERE document_id = ? ORDER BY chunk_index", (doc_id,))


# ------------------------------------------------------------------ templates + judgment rules
@router.get("/templates")
def list_templates() -> list[dict]:
    rows = db.q(
        """SELECT t.id, t.name, t.form_type, t.fingerprint, t.created_at, t.updated_at,
                  (SELECT COUNT(*) FROM forms f WHERE f.template_id = t.id) AS form_count,
                  (SELECT COUNT(*) FROM corrections c WHERE c.template_id = t.id) AS correction_count,
                  (SELECT COUNT(*) FROM judgment_rules r WHERE r.template_id = t.id) AS rule_count,
                  (SELECT COUNT(*) FROM judgment_rules r WHERE r.template_id = t.id AND r.flagged = 1) AS flagged_count
           FROM form_templates t ORDER BY t.id DESC"""
    )
    return rows


@router.get("/templates/{template_id}")
def get_template(template_id: int) -> dict:
    from app.judgment.setup import rule_out

    t = db.q1("SELECT * FROM form_templates WHERE id = ?", (template_id,))
    if not t:
        raise HTTPException(404, "template not found")
    latest_form = db.q1("SELECT id FROM forms WHERE template_id = ? ORDER BY id DESC LIMIT 1", (template_id,))
    return {
        "id": t["id"],
        "name": t["name"],
        "form_type": t["form_type"],
        "fingerprint": t["fingerprint"],
        "fields": db.loads(t["field_schema_json"], []),
        "cross_field_rules": db.loads(t["cross_field_rules_json"], []),
        "rules": [rule_out(r) for r in db.q("SELECT * FROM judgment_rules WHERE template_id = ? ORDER BY id", (template_id,))],
        "latest_form_id": latest_form["id"] if latest_form else None,
        "corrections": db.q(
            "SELECT id, field_key, field_label, model_value, user_value, user_note, facts_json, created_at FROM corrections WHERE template_id = ? ORDER BY id DESC LIMIT 200",
            (template_id,),
        ),
    }


class TemplatePatch(BaseModel):
    name: str | None = None
    cross_field_rules: list[dict] | None = None


@router.patch("/templates/{template_id}")
def patch_template(template_id: int, body: TemplatePatch) -> dict:
    upd: dict[str, Any] = {}
    if body.name is not None:
        upd["name"] = body.name
    if body.cross_field_rules is not None:
        upd["cross_field_rules_json"] = db.dumps(body.cross_field_rules)
    db.update("form_templates", template_id, upd)
    return get_template(template_id)


class RuleUpdate(BaseModel):
    facts: list[dict] | None = None
    rule_type: Literal["code", "rubric"] | None = None
    rule_code: str | None = None
    rule_params: dict | None = None
    rubric_text: str | None = None
    status: Literal["draft", "saved"] = "saved"


@router.put("/templates/{template_id}/rules/{field_key}")
def put_rule(template_id: int, field_key: str, body: RuleUpdate) -> dict:
    from app.judgment.setup import save_rule

    try:
        return save_rule(template_id, field_key, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/templates/{template_id}/rules/{field_key}/propose")
def propose(template_id: int, field_key: str) -> dict:
    from app.forms.service import row_to_field
    from app.judgment.setup import ensure_rules_for_template, get_rule

    form = db.q1("SELECT id FROM forms WHERE template_id = ? ORDER BY id DESC LIMIT 1", (template_id,))
    if not form:
        raise HTTPException(404, "no form for template")
    f = db.q1("SELECT * FROM fields WHERE form_id = ? AND key = ?", (form["id"], field_key))
    if not f:
        raise HTTPException(404, "field not found")
    ensure_rules_for_template(template_id, [row_to_field(f)], force_keys={field_key})
    return get_rule(template_id, field_key)


@router.get("/rules/registry")
def rules_registry() -> list[dict]:
    from app.judgment.rules import registry_listing

    return registry_listing()


@router.get("/validators/cross-rules")
def cross_rules() -> list[str]:
    from app.verify.validators import registered_cross_rules

    return registered_cross_rules()


# ------------------------------------------------------------------ batch mode
class BatchCreate(BaseModel):
    form_id: int
    count: int


@router.post("/batches")
def create_batch(body: BatchCreate) -> dict:
    """Create N jobs for one form; upload documents to each job, then POST /batches/{id}/run."""
    batch_id = uuid.uuid4().hex[:12]
    ids = [db.insert("jobs", {"form_id": body.form_id, "status": "created", "batch_id": batch_id}) for _ in range(body.count)]
    return {"batch_id": batch_id, "job_ids": ids}


@router.post("/batches/{batch_id}/run")
def run_batch(batch_id: str) -> dict:
    ids = [r["id"] for r in db.q("SELECT id FROM jobs WHERE batch_id = ?", (batch_id,))]
    for i in ids:
        enqueue(i)
    return batch_status(batch_id)


@router.get("/batches/{batch_id}")
def batch_status(batch_id: str) -> dict:
    rows = db.q("SELECT id, status, progress, started_at, finished_at, stats_json FROM jobs WHERE batch_id = ?", (batch_id,))
    if not rows:
        raise HTTPException(404, "batch not found")
    done = [r for r in rows if r["status"] == "done"]
    out: dict[str, Any] = {
        "batch_id": batch_id,
        "jobs": [{k: r[k] for k in ("id", "status", "progress")} for r in rows],
        "done": len(done),
        "failed": sum(r["status"] == "failed" for r in rows),
        "total": len(rows),
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


_ = shutil
