"""Field values: persistence, review actions, corrections capture and the audit log."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from app import db
from app.config import get_config
from app.db.models import FieldDef, FieldResult
from app.forms.service import get_fields, row_to_field
from app.verify.normalize import normalize_value
from app.verify.validators import validate_field


def audit(job_id: int | None, field_id: int | None, action: str, old: Any, new: Any, actor: str = "user") -> None:
    db.insert(
        "audit_log",
        {
            "job_id": job_id,
            "field_id": field_id,
            "action": action,
            "old_value": None if old is None else db.dumps(old),
            "new_value": None if new is None else db.dumps(new),
            "actor": actor,
        },
    )


def _enc(v: Any) -> str | None:
    return None if v is None else db.dumps(v)


def _dec(v: str | None) -> Any:
    return None if v is None else db.loads(v)


def save_results(job_id: int, fields: list[FieldDef], results: dict[str, FieldResult]) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM field_values WHERE job_id = ?", (job_id,))
        for f in fields:
            r = results.get(f.key) or FieldResult(key=f.key)
            db.insert("field_values", result_row(job_id, f, r))
            audit(job_id, f.id, "model_fill", None, r.value, actor="model")


def result_row(job_id: int, f: FieldDef, r: FieldResult) -> dict:
    return {
        "job_id": job_id,
        "field_id": f.id,
        "value": _enc(r.value),
        "status": r.status,
        "confidence": r.confidence,
        "evidence_json": db.dumps([e.model_dump() for e in r.evidence]),
        "candidates_json": db.dumps([c.model_dump() for c in r.candidates]),
        "validator_errors_json": db.dumps(r.validator_errors),
        "derivation_json": db.dumps(r.derivation) if r.derivation else None,
        "reasoning": r.reasoning,
        "facts_used_json": db.dumps(r.facts_used),
        "missing_facts_json": db.dumps(r.missing_facts),
        "run_agreement": r.run_agreement,
        "rule_json": db.dumps(r.rule) if r.rule else None,
        "review_status": "pending",
        "user_edited": 0,
        "final_value": None,
    }


def value_out(fv: dict, f: FieldDef, facts: list[dict] | None = None) -> dict:
    return {
        "field_id": f.id,
        "key": f.key,
        "label": f.label,
        "type": f.type,
        "kind": f.kind,
        "section": f.section,
        "required": f.required,
        "options": f.options,
        "format_hint": f.format_hint,
        "instructions": f.instructions,
        "page": f.page,
        "bbox": f.bbox,
        "order_index": f.order_index,
        "value": _dec(fv.get("value")) if fv else None,
        "final_value": _dec(fv.get("final_value")) if fv else None,
        "status": fv.get("status") if fv else None,
        "confidence": fv.get("confidence") if fv else None,
        "evidence": db.loads(fv.get("evidence_json"), []) if fv else [],
        "candidates": db.loads(fv.get("candidates_json"), []) if fv else [],
        "validator_errors": db.loads(fv.get("validator_errors_json"), []) if fv else [],
        "derivation": db.loads(fv.get("derivation_json")) if fv else None,
        "reasoning": fv.get("reasoning") if fv else None,
        "facts_used": db.loads(fv.get("facts_used_json"), []) if fv else [],
        "missing_facts": db.loads(fv.get("missing_facts_json"), []) if fv else [],
        "run_agreement": fv.get("run_agreement") if fv else None,
        "rule": db.loads(fv.get("rule_json")) if fv else None,
        "review_status": fv.get("review_status", "pending") if fv else "pending",
        "user_edited": bool(fv.get("user_edited")) if fv else False,
        "facts": facts or [],
        "requires_manual_signing": f.type == "signature",
    }


def list_values(job_id: int) -> list[dict]:
    from app.judgment.runtime import load_facts

    job = db.q1("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job:
        raise KeyError(job_id)
    fields = get_fields(job["form_id"])
    fvs = {r["field_id"]: r for r in db.q("SELECT * FROM field_values WHERE job_id = ?", (job_id,))}
    out = []
    for f in fields:
        facts = load_facts(job_id, f.key) if f.kind == "judgment" else None
        out.append(value_out(fvs.get(f.id, {}), f, facts))
    return out


def get_value(job_id: int, field_id: int) -> dict:
    from app.judgment.runtime import load_facts

    f_row = db.q1("SELECT * FROM fields WHERE id = ?", (field_id,))
    if not f_row:
        raise KeyError(field_id)
    f = row_to_field(f_row)
    fv = db.q1("SELECT * FROM field_values WHERE job_id = ? AND field_id = ?", (job_id, field_id)) or {}
    return value_out(fv, f, load_facts(job_id, f.key) if f.kind == "judgment" else None)


def _template_id(job_id: int) -> int | None:
    r = db.q1("SELECT f.template_id FROM jobs j JOIN forms f ON f.id = j.form_id WHERE j.id = ?", (job_id,))
    return r["template_id"] if r else None


def save_correction(job_id: int, f: FieldDef, fv: dict, new_value: Any, note: str | None) -> None:
    from app.judgment.runtime import load_facts

    tid = _template_id(job_id)
    if not tid:
        return
    model_value = _dec(fv.get("value"))
    facts_json = None
    if f.kind == "judgment":
        facts_json = db.dumps({x["fact_key"]: x["value"] for x in load_facts(job_id, f.key)})
    db.insert(
        "corrections",
        {
            "template_id": tid,
            "field_key": f.key,
            "field_label": f.label,
            "model_value": None if model_value is None else str(model_value),
            "user_value": None if new_value is None else str(new_value),
            "evidence_json": fv.get("evidence_json") or "[]",
            "facts_json": facts_json,
            "user_note": note,
            "job_id": job_id,
        },
    )
    if f.kind == "judgment":
        _maybe_flag_rubric(tid, f.key)


def _maybe_flag_rubric(template_id: int, field_key: str) -> None:
    """If corrections keep contradicting a judgment rule, flag it for review."""
    rule = db.q1("SELECT * FROM judgment_rules WHERE template_id = ? AND field_key = ?", (template_id, field_key))
    if not rule:
        return
    rows = db.q(
        "SELECT * FROM corrections WHERE template_id = ? AND field_key = ? AND created_at >= ? ORDER BY id DESC",
        (template_id, field_key, rule["updated_at"]),
    )
    contradictions = [r for r in rows if (r["model_value"] or "") != (r["user_value"] or "")]
    if len(contradictions) >= 2:
        db.update(
            "judgment_rules",
            rule["id"],
            {
                "flagged": 1,
                "flag_reason": f"{len(contradictions)} reviewer corrections contradict this rule since it was last edited",
            },
        )


def apply_review(
    job_id: int,
    field_id: int,
    action: str,
    value: Any = None,
    candidate_index: int | None = None,
    note: str | None = None,
    actor: str = "user",
) -> dict:
    f_row = db.q1("SELECT * FROM fields WHERE id = ?", (field_id,))
    if not f_row:
        raise KeyError(field_id)
    f = row_to_field(f_row)
    fv = db.q1("SELECT * FROM field_values WHERE job_id = ? AND field_id = ?", (job_id, field_id))
    if not fv:
        raise KeyError(f"no value for field {field_id} in job {job_id}")
    model_value = _dec(fv["value"])
    old_final = _dec(fv["final_value"]) if fv["review_status"] != "pending" else model_value
    upd: dict[str, Any] = {"updated_at": _now()}
    if action == "accept":
        if f.type == "signature":
            raise ValueError("signature fields are signed manually")
        new = model_value
        upd.update(review_status="accepted", final_value=_enc(new))
    elif action == "edit":
        new, err = normalize_value(f, value)
        upd.update(review_status="edited", final_value=_enc(new), user_edited=1)
        errs = validate_field(f, new)
        if err and new is not None:
            errs.insert(0, err)
        upd["validator_errors_json"] = db.dumps(errs)
        if _differs(new, model_value):
            save_correction(job_id, f, fv, new, note)
    elif action == "choose_candidate":
        cands = db.loads(fv["candidates_json"], [])
        if candidate_index is None or not 0 <= candidate_index < len(cands):
            raise ValueError("invalid candidate_index")
        new = cands[candidate_index]["value"]
        upd.update(review_status="edited", final_value=_enc(new), user_edited=1)
        if _differs(new, model_value):
            save_correction(job_id, f, fv, new, note)
    elif action == "not_applicable":
        new = None
        upd.update(review_status="not_applicable", final_value=None)
        if model_value is not None:
            save_correction(job_id, f, fv, None, note or "not applicable")
    elif action == "reset":
        new = model_value
        upd.update(review_status="pending", final_value=None, user_edited=0)
    else:
        raise ValueError(f"unknown action {action!r}")
    db.update("field_values", fv["id"], upd)
    audit(job_id, field_id, action, old_final, new, actor=actor)
    return get_value(job_id, field_id)


def _differs(a: Any, b: Any) -> bool:
    return str(a if a is not None else "").strip().lower() != str(b if b is not None else "").strip().lower()


def answer_facts(job_id: int, field_id: int, answers: dict[str, Any]) -> dict:
    """User answers for missing facts -> saved as facts with source 'user' -> decision re-run."""
    from app.db.models import FactDef
    from app.judgment.runtime import decide, save_fact
    from app.judgment.setup import get_rule

    f = row_to_field(db.q1("SELECT * FROM fields WHERE id = ?", (field_id,)))
    tid = _template_id(job_id)
    rule = get_rule(tid, f.key)
    if not rule:
        raise ValueError("no judgment rule for this field")
    defs = {d["key"]: FactDef.model_validate(d) for d in rule["facts"]}
    for k, v in answers.items():
        fd = defs.get(k) or FactDef(key=k, label=k)
        old = db.q1("SELECT value FROM facts WHERE job_id = ? AND field_key = ? AND fact_key = ?", (job_id, f.key, k))
        save_fact(job_id, tid, f.key, fd, None, source="user", value=v)
        audit(job_id, field_id, "answer_fact", old["value"] if old else None, {k: v})
    res = decide(job_id, f, rule, tid)
    fv = db.q1("SELECT * FROM field_values WHERE job_id = ? AND field_id = ?", (job_id, field_id))
    row = result_row(job_id, f, res)
    if fv:
        db.update("field_values", fv["id"], {k: v for k, v in row.items() if k not in ("job_id", "field_id")})
    else:
        db.insert("field_values", row)
    audit(job_id, field_id, "redecide", _dec(fv["value"]) if fv else None, res.value, actor="model")
    return get_value(job_id, field_id)


def resolved_values(job_id: int) -> dict[str, Any]:
    """Values that go into the exported form."""
    out: dict[str, Any] = {}
    for v in list_values(job_id):
        if v["type"] == "signature":
            continue
        rs = v["review_status"]
        if rs in ("accepted", "edited"):
            out[v["key"]] = v["final_value"]
        elif rs == "not_applicable":
            out[v["key"]] = None
        elif v["kind"] == "judgment":
            out[v["key"]] = None  # never auto-accepted
        elif v["status"] in ("not_found", "conflict") and v["value"] is None:
            out[v["key"]] = None
        else:
            out[v["key"]] = v["value"]
    return out


def delete_job(job_id: int) -> None:
    from app.ingest.embeddings import drop_index

    cfg = get_config()
    with db.transaction() as conn:
        conn.execute("DELETE FROM audit_log WHERE job_id = ?", (job_id,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))  # cascades: documents, chunks, spans, facts, values
    audit(None, None, "job_deleted", None, {"job_id": job_id}, actor="user")
    shutil.rmtree(cfg.data_path / "jobs" / str(job_id), ignore_errors=True)
    drop_index(job_id)


def job_dir(job_id: int) -> Path:
    p = get_config().data_path / "jobs" / str(job_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
