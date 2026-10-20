"""Batched structured extraction for lookup fields (and hidden fact fields)."""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from typing import Any, Callable

from app import db
from app.config import get_config
from app.db.models import Candidate, Evidence, FieldDef, FieldResult
from app.extraction.llm_adapter import LLMError, get_llm
from app.extraction.prompts import extraction_messages
from app.extraction.retrieval import retrieve
from app.extraction.schema_builder import batch_schema
from app.ingest.embeddings import JobIndex
from app.verify.confidence import lookup_confidence, lookup_status
from app.verify.evidence import verify_evidence
from app.verify.normalize import normalize_value, parse_date, parse_decimal
from app.verify.validators import validate_field

log = logging.getLogger(__name__)


def make_batches(fields: list[FieldDef], bmin: int, bmax: int) -> list[tuple[str | None, list[FieldDef]]]:
    """Group by section in form order; split big sections, merge small adjacent ones."""
    groups: list[tuple[str | None, list[FieldDef]]] = []
    for f in fields:
        if groups and groups[-1][0] == f.section:
            groups[-1][1].append(f)
        else:
            groups.append((f.section, [f]))
    split: list[tuple[str | None, list[FieldDef]]] = []
    for sec, fs in groups:
        for i in range(0, len(fs), bmax):
            split.append((sec, fs[i : i + bmax]))
    merged: list[tuple[str | None, list[FieldDef]]] = []
    for sec, fs in split:
        if merged and (len(merged[-1][1]) < bmin or len(fs) < bmin) and len(merged[-1][1]) + len(fs) <= bmax:
            prev_sec, prev = merged[-1]
            label = prev_sec if prev_sec == sec else " / ".join(s for s in (prev_sec, sec) if s) or None
            merged[-1] = (label, prev + fs)
        else:
            merged.append((sec, list(fs)))
    return merged


def get_fewshot(template_id: int | None, keys: list[str], limit: int) -> list[dict]:
    if not template_id or not keys:
        return []
    ph = ",".join("?" * len(keys))
    return db.q(
        f"""SELECT * FROM corrections WHERE template_id = ? AND field_key IN ({ph})
            ORDER BY created_at DESC, id DESC LIMIT ?""",
        (template_id, *keys, limit),
    )


def _evidence_list(items: Any) -> list[Evidence]:
    out = []
    for e in items or []:
        if not isinstance(e, dict):
            continue
        q = str(e.get("quote") or "").strip()
        if not q:
            continue
        cid = e.get("chunk_id")
        try:
            cid = int(cid) if cid is not None else None
        except (TypeError, ValueError):
            cid = None
        out.append(Evidence(chunk_id=cid, quote=q))
    return out


def compute_derivation(op: str, values: list[Any], field: FieldDef) -> Any:
    if op in ("date_diff_days", "age_years"):
        dates = [parse_date(str(v)) for v in values]
        dates = [d for d in dates if d]
        if op == "age_years" and dates:
            ref = dates[1] if len(dates) > 1 else dt.date.today()
            dob = dates[0]
            return ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day))
        if op == "date_diff_days" and len(dates) >= 2:
            return abs((dates[1] - dates[0]).days)
        return None
    nums = [parse_decimal(v) for v in values]
    if any(n is None for n in nums) or not nums:
        return None if op != "count" else len(values)
    if op == "sum":
        return sum(nums, Decimal(0))
    if op == "difference":
        return nums[0] - sum(nums[1:], Decimal(0))
    if op == "product":
        out = Decimal(1)
        for n in nums:
            out *= n
        return out
    if op == "ratio":
        return nums[0] / nums[1] if len(nums) > 1 and nums[1] != 0 else None
    if op == "average":
        return sum(nums, Decimal(0)) / len(nums)
    if op == "min":
        return min(nums)
    if op == "max":
        return max(nums)
    if op == "count":
        return len(nums)
    return None


def build_result(field: FieldDef, raw: dict | None, chunks: dict[int, dict], threshold: int) -> FieldResult:
    res = FieldResult(key=field.key)
    if not isinstance(raw, dict):
        res.status, res.confidence = "not_found", ("low" if field.required else "medium")
        res.validator_errors = validate_field(field, None)
        return res
    value, err = normalize_value(field, raw.get("value"))
    res.evidence = _evidence_list(raw.get("evidence"))

    # candidates (conflicting sources)
    cands: list[Candidate] = []
    for c in raw.get("candidates") or []:
        if not isinstance(c, dict):
            continue
        cv, _ = normalize_value(field, c.get("value"))
        if cv is None:
            continue
        ev = _evidence_list([c])
        ev = verify_evidence(field, cv, ev, chunks, threshold)
        existing = next((x for x in cands if str(x.value) == str(cv)), None)
        if existing:
            existing.evidence.extend(ev)
        else:
            cands.append(Candidate(value=cv, evidence=ev))
    # derivation (backend does the arithmetic)
    deriv = raw.get("derivation") if raw.get("derived") else None
    if isinstance(deriv, dict) and deriv.get("inputs"):
        op = deriv.get("op") or "sum"
        inputs = []
        num_field = FieldDef(key="_in", label="input", type="date" if op in ("date_diff_days", "age_years") else "number")
        vals = []
        for inp in deriv["inputs"]:
            if not isinstance(inp, dict):
                continue
            ev = verify_evidence(num_field, inp.get("value"), _evidence_list([inp]), chunks, threshold)
            e0 = ev[0] if ev else Evidence(quote=str(inp.get("quote") or ""))
            norm_in = inp.get("value")
            if num_field.type == "date":
                d = parse_date(str(norm_in))
                norm_in = d.isoformat() if d else norm_in
                e0.value_in_quote = bool(d) and _date_in_quote(d, e0.quote)
            inputs.append(
                {
                    "value": inp.get("value"),
                    "quote": e0.quote,
                    "document_id": e0.document_id,
                    "chunk_id": e0.chunk_id,
                    "page": e0.page,
                    "bbox": e0.bbox,
                    "boxes": e0.boxes,
                    "verified": e0.verified,
                    "value_in_quote": e0.value_in_quote,
                }
            )
            vals.append(norm_in)
        computed = compute_derivation(op, vals, field)
        res.derivation = {"op": op, "inputs": inputs, "result": None if computed is None else str(computed)}
        if computed is not None:
            value, err = normalize_value(field, str(computed))
        res.evidence = [Evidence(**{k: i[k] for k in ("quote", "document_id", "chunk_id", "page", "bbox", "boxes", "verified", "value_in_quote")}) for i in inputs]
    else:
        res.evidence = verify_evidence(field, value, res.evidence, chunks, threshold) if value is not None else []
        distinct = {str(c.value) for c in cands}
        if value is not None and cands and str(value) not in distinct:
            cands.insert(0, Candidate(value=value, evidence=res.evidence))
            distinct.add(str(value))
        if len(distinct) > 1:
            res.resolved_by_model = value is not None
        else:
            # zero or one distinct candidate: not a conflict; fold its evidence into the main evidence
            if cands:
                if value is None:
                    value = cands[0].value
                for e in cands[0].evidence:
                    if not any(x.quote == e.quote for x in res.evidence):
                        res.evidence.append(e)
            cands = []
    res.value = value
    res.candidates = cands
    res.status = lookup_status(res)  # type: ignore[assignment]
    if res.status == "not_found":
        res.value = None
    res.validator_errors = validate_field(field, res.value)
    if err and res.value is not None and err not in res.validator_errors:
        res.validator_errors.insert(0, err)
    res.confidence = lookup_confidence(field, res)  # type: ignore[assignment]
    return res


def _date_in_quote(d: dt.date, quote: str) -> bool:
    from app.verify.evidence import value_in_quote

    return value_in_quote(FieldDef(key="_d", label="d", type="date"), d.isoformat(), quote, 90)


def run_lookup(
    job_id: int,
    fields: list[FieldDef],
    template_id: int | None,
    index: JobIndex,
    progress: Callable[[float, str], None] | None = None,
    model: str | None = None,
) -> dict[str, FieldResult]:
    cfg = get_config()
    fillable = [f for f in fields if f.type != "signature"]
    results: dict[str, FieldResult] = {}
    filenames = {r["id"]: r["filename"] for r in db.q("SELECT id, filename FROM documents WHERE job_id = ?", (job_id,))}
    batches = make_batches(fillable, cfg.extraction.batch_min, cfg.extraction.batch_max)
    threshold = cfg.verification.fuzzy_threshold
    done = 0

    def one(item: tuple[int, tuple[str | None, list[FieldDef]]]) -> dict[str, FieldResult]:
        bi, (section, batch) = item
        chunks = retrieve(index, section, batch)
        fewshot = get_fewshot(template_id, [f.key for f in batch], cfg.extraction.max_fewshot)
        msgs = extraction_messages(batch, chunks, filenames, fewshot)
        try:
            out = get_llm().chat_json(msgs, batch_schema(batch), model=model)
        except LLMError as e:
            log.error("extraction batch %s failed: %s", bi, e)
            raise
        if isinstance(out, list):  # tolerate list-of-objects output
            out = {o.get("key"): o for o in out if isinstance(o, dict)}
        if isinstance(out, dict) and "fields" in out and isinstance(out["fields"], (list, dict)) and not any(f.key == "fields" for f in batch):
            inner = out["fields"]
            out = inner if isinstance(inner, dict) else {o.get("key"): o for o in inner if isinstance(o, dict)}
        return {f.key: build_result(f, (out or {}).get(f.key), index.by_id, threshold) for f in batch}

    workers = max(1, min(cfg.extraction.parallel_batches, len(batches)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(one, enumerate(batches)):
            results.update(part)
            done += 1
            if progress:
                progress(done / len(batches), f"extracted batch {done}/{len(batches)}")
    for f in fields:
        if f.type == "signature":
            results[f.key] = FieldResult(key=f.key, status="not_found", confidence="low")
    return results
