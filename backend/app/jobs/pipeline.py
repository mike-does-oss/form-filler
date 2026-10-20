"""The full job: ingest sources -> extract lookups -> cross-field rules -> judgments -> save."""

from __future__ import annotations

import logging
import time
import traceback

from app import db
from app.forms.service import get_fields
from app.ingest.embeddings import get_index
from app.ingest.pipeline import ingest_document
from app.jobs.service import save_results
from app.verify.confidence import lookup_confidence
from app.verify.validators import apply_cross_rules

log = logging.getLogger(__name__)


class Progress:
    def __init__(self, job_id: int):
        self.job_id = job_id
        self._last = 0.0

    def set(self, value: float, stage: str, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last < 0.3:
            return
        self._last = now
        db.ex("UPDATE jobs SET progress = ?, stage = ? WHERE id = ?", (round(min(1.0, max(0.0, value)), 3), stage, self.job_id))

    def scoped(self, lo: float, hi: float, prefix: str):
        return lambda frac, msg: self.set(lo + (hi - lo) * frac, f"{prefix}: {msg}")


def run_job(job_id: int) -> dict:
    job = db.q1("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise KeyError(job_id)
    prog = Progress(job_id)
    db.ex("UPDATE jobs SET status = 'running', started_at = datetime('now'), error = NULL WHERE id = ?", (job_id,))
    stats: dict = {}
    t0 = time.monotonic()
    try:
        docs = db.q("SELECT * FROM documents WHERE job_id = ? ORDER BY id", (job_id,))
        if not docs:
            raise ValueError("no source documents uploaded")
        ocr_pages = 0
        ocr_time = 0.0
        for i, d in enumerate(docs):
            if d["status"] == "ingested":
                continue
            lo, hi = 0.4 * i / len(docs), 0.4 * (i + 1) / len(docs)
            ts = time.monotonic()
            info = ingest_document(d["id"], prog.scoped(lo, hi, f"ingest {d['filename']}"))
            dt_ = time.monotonic() - ts
            if info["ocr_pages"]:
                ocr_pages += len(info["ocr_pages"])
                ocr_time += dt_
        stats["ingest_s"] = round(time.monotonic() - t0, 2)
        stats["ocr_pages"] = ocr_pages
        stats["ocr_s_per_page"] = round(ocr_time / ocr_pages, 2) if ocr_pages else None
        prog.set(0.4, "indexing", force=True)

        index = get_index(job_id, refresh=True)
        form = db.q1("SELECT * FROM forms WHERE id = ?", (job["form_id"],))
        template_id = form["template_id"]
        fields = get_fields(form["id"])
        lookup = [f for f in fields if f.kind == "lookup"]
        t1 = time.monotonic()
        from app.extraction.extract import run_lookup

        results = run_lookup(job_id, lookup, template_id, index, prog.scoped(0.4, 0.8, "extract"))
        stats["extract_s"] = round(time.monotonic() - t1, 2)

        tpl = db.q1("SELECT cross_field_rules_json FROM form_templates WHERE id = ?", (template_id,)) if template_id else None
        rules = db.loads(tpl["cross_field_rules_json"], []) if tpl else []
        if rules:
            by_key = {f.key: f for f in fields}
            apply_cross_rules(rules, results, by_key)
            for k, r in results.items():
                if k in by_key and by_key[k].kind == "lookup":
                    r.confidence = lookup_confidence(by_key[k], r)  # type: ignore[assignment]

        t2 = time.monotonic()
        from app.judgment.runtime import run_judgments

        results.update(run_judgments(job_id, fields, template_id, index, prog.scoped(0.8, 0.99, "judgment")))
        stats["judgment_s"] = round(time.monotonic() - t2, 2)
        save_results(job_id, fields, results)
        stats["total_s"] = round(time.monotonic() - t0, 2)
        stats["fields"] = len(fields)
        stats["pages"] = sum(r["page_count"] for r in db.q("SELECT page_count FROM documents WHERE job_id = ?", (job_id,)))
        db.ex(
            "UPDATE jobs SET status = 'done', progress = 1, stage = 'done', finished_at = datetime('now'), stats_json = ? WHERE id = ?",
            (db.dumps(stats), job_id),
        )
        return stats
    except Exception as e:  # noqa: BLE001
        log.error("job %s failed: %s\n%s", job_id, e, traceback.format_exc())
        db.ex(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = datetime('now'), stats_json = ? WHERE id = ?",
            (f"{type(e).__name__}: {e}", db.dumps(stats), job_id),
        )
        raise
