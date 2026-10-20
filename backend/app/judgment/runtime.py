"""Runtime for judgment fields: extract facts (lookup pipeline), then decide.

  missing required fact  -> cannot_determine (no model call)
  code rule              -> registered Python function (no model call)
  rubric rule            -> N self-consistency calls to the judgment model; majority answer
"""

from __future__ import annotations

import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from app import db
from app.config import get_config
from app.db.models import CANNOT_DETERMINE, Evidence, FactDef, FieldDef, FieldResult
from app.extraction.extract import run_lookup
from app.extraction.llm_adapter import get_llm
from app.extraction.prompts import judgment_messages
from app.extraction.schema_builder import judgment_schema
from app.ingest.embeddings import JobIndex
from app.judgment.rules import run_rule
from app.judgment.setup import effective_options, ensure_rules_for_template, get_rule
from app.verify.confidence import judgment_confidence

log = logging.getLogger(__name__)

SEP = "__"


def fact_field(jf: FieldDef, fd: FactDef) -> FieldDef:
    return FieldDef(
        key=f"{jf.key}{SEP}{fd.key}",
        label=fd.label,
        type=fd.type if fd.type in ("text", "number", "date", "currency", "checkbox", "choice") else "text",
        kind="lookup",
        section=f"Facts for: {jf.label}",
        instructions=(fd.question or None),
        meta={"fact_for": jf.key},
    )


def load_facts(job_id: int, field_key: str) -> list[dict]:
    rows = db.q("SELECT * FROM facts WHERE job_id = ? AND field_key = ? ORDER BY id", (job_id, field_key))
    return [
        {
            "fact_key": r["fact_key"],
            "label": r["label"],
            "type": r["type"],
            "question": r["question"],
            "value": r["value"],
            "status": r["status"],
            "confidence": r["confidence"],
            "source": r["source"],
            "evidence": db.loads(r["evidence_json"], []),
        }
        for r in rows
    ]


def save_fact(job_id: int, template_id: int | None, field_key: str, fd: FactDef, res: FieldResult | None, *, source: str = "model", value: Any = None) -> None:
    existing = db.q1(
        "SELECT * FROM facts WHERE job_id = ? AND field_key = ? AND fact_key = ?", (job_id, field_key, fd.key)
    )
    if existing and existing["source"] == "user" and source != "user":
        return  # a user's answer wins over a re-run
    if source == "user":
        row = {
            "value": None if value in (None, "") else str(value),
            "status": "verified" if value not in (None, "") else "not_found",
            "confidence": "high",
            "source": "user",
            "evidence_json": db.dumps([Evidence(quote="Answered by user", source="user", verified=True).model_dump()]),
        }
    else:
        assert res is not None
        row = {
            "value": None if res.value is None else str(res.value),
            "status": res.status,
            "confidence": res.confidence,
            "source": "model",
            "evidence_json": db.dumps([e.model_dump() for e in res.evidence]),
        }
    row.update({"label": fd.label, "type": fd.type, "question": fd.question, "template_id": template_id})
    if existing:
        db.update("facts", existing["id"], row)
    else:
        db.insert("facts", {"job_id": job_id, "field_key": field_key, "fact_key": fd.key, **row})


def _facts_verified(facts: list[dict], needed: list[str]) -> bool:
    by_key = {f["fact_key"]: f for f in facts}
    for k in needed:
        f = by_key.get(k)
        if not f or f["value"] in (None, ""):
            return False
        if f["source"] != "user" and f["status"] not in ("verified", "derived"):
            return False
    return True


def _to_field_value(field: FieldDef, answer: str) -> Any:
    if answer == CANNOT_DETERMINE:
        return None
    if field.type == "checkbox":
        return answer.lower() in ("yes", "true", "y")
    return answer


def judgment_fewshot(template_id: int | None, field_key: str, limit: int) -> list[dict]:
    if not template_id:
        return []
    return db.q(
        "SELECT * FROM corrections WHERE template_id = ? AND field_key = ? AND facts_json IS NOT NULL ORDER BY id DESC LIMIT ?",
        (template_id, field_key, limit),
    )


def decide(job_id: int, field: FieldDef, rule: dict, template_id: int | None) -> FieldResult:
    cfg = get_config()
    facts = load_facts(job_id, field.key)
    fact_defs = [FactDef.model_validate(x) for x in rule.get("facts", [])]
    needed = [fd.key for fd in fact_defs if fd.required]
    by_key = {f["fact_key"]: f for f in facts}
    options = effective_options(field)
    res = FieldResult(key=field.key, rule={k: rule.get(k) for k in ("rule_type", "rule_code", "rule_params", "rubric_text", "status")})
    res.facts_used = [
        {"fact_key": f["fact_key"], "label": f["label"], "value": f["value"], "status": f["status"], "source": f["source"], "evidence": f["evidence"]}
        for f in facts
    ]
    missing = [k for k in needed if not by_key.get(k) or by_key[k]["value"] in (None, "")]
    if missing:
        res.status = "cannot_determine"
        res.missing_facts = [
            {"fact_key": k, "label": fd.label, "question": fd.question or f"What is the {fd.label.lower()}?"}
            for fd in fact_defs
            for k in [fd.key]
            if k in missing
        ]
        res.reasoning = "Required facts were not found in the source documents: " + ", ".join(missing)
        res.confidence = "low"
        return res

    values = {f["fact_key"]: f["value"] for f in facts}
    if rule.get("rule_type") == "code" and rule.get("rule_code"):
        params = dict(rule.get("rule_params") or {})
        params.pop("suggested", None)
        answer, why, miss = run_rule(rule["rule_code"], values, params, options)
        res.reasoning = f"Rule {rule['rule_code']}: {why}"
        if miss:
            res.missing_facts = [
                {"fact_key": fd.key, "label": fd.label, "question": fd.question} for fd in fact_defs if fd.key in miss
            ]
    else:
        answer = _rubric_vote(field, rule, facts, options, template_id, res, cfg)
    res.value = _to_field_value(field, answer)
    res.status = "cannot_determine" if answer == CANNOT_DETERMINE else "judged"
    if res.status == "cannot_determine" and not res.missing_facts:
        res.missing_facts = [
            {"fact_key": fd.key, "label": fd.label, "question": fd.question} for fd in fact_defs if fd.key in needed
        ]
    res.confidence = judgment_confidence(res, rule.get("rule_type", "rubric"), _facts_verified(facts, needed))  # type: ignore[assignment]
    return res


def _rubric_vote(field, rule, facts, options, template_id, res: FieldResult, cfg) -> str:
    n = max(1, cfg.judgment.self_consistency_runs)
    fewshot = judgment_fewshot(template_id, field.key, cfg.extraction.max_fewshot)
    msgs = judgment_messages(field, rule.get("rubric_text") or "", facts, fewshot)
    schema = judgment_schema(options, [f["fact_key"] for f in facts])
    runs: list[dict] = []

    def one(i: int) -> dict | None:
        try:
            out = get_llm().chat_json(
                msgs, schema, model=cfg.judgment_model, temperature=cfg.judgment.temperature if n > 1 else 0.0, seed=1000 + i
            )
        except Exception as e:  # noqa: BLE001
            log.warning("judgment run %s for %s failed: %s", i, field.key, e)
            return None
        ans = str(out.get("answer", CANNOT_DETERMINE))
        if options and ans not in options and ans != CANNOT_DETERMINE:
            ans = next((o for o in options if o.lower() == ans.lower()), CANNOT_DETERMINE)
        out["answer"] = ans
        return out

    # runs are independent; the server can batch them and reuses the cached prompt
    with ThreadPoolExecutor(max_workers=max(1, min(n, cfg.judgment.parallel_runs))) as ex:
        runs = [r for r in ex.map(one, range(n)) if r is not None]
    if not runs:
        res.reasoning = "The judgment model could not be reached."
        res.run_agreement = 0.0
        return CANNOT_DETERMINE
    votes = Counter(r["answer"] for r in runs)
    answer, count = votes.most_common(1)[0]
    res.run_agreement = round(count / n, 3)
    first = next(r for r in runs if r["answer"] == answer)
    res.reasoning = first.get("reasoning")
    used_keys = {u.get("fact_key") for u in first.get("facts_used", []) if isinstance(u, dict)}
    for fu in res.facts_used:
        fu["used"] = fu["fact_key"] in used_keys
    if answer == CANNOT_DETERMINE:
        by_key = {f["fact_key"]: f for f in facts}
        defs = {d["key"]: d for d in rule.get("facts", [])}
        res.missing_facts = [
            {"fact_key": k, "label": by_key.get(k, {}).get("label", k), "question": defs.get(k, {}).get("question")}
            for k in first.get("missing_facts", [])
        ]
    res.rule = {**(res.rule or {}), "votes": dict(votes), "runs": n}
    return answer


def run_judgments(
    job_id: int,
    fields: list[FieldDef],
    template_id: int | None,
    index: JobIndex,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, FieldResult]:
    jfields = [f for f in fields if f.kind == "judgment" and f.type != "signature"]
    if not jfields:
        return {}
    if template_id:
        ensure_rules_for_template(template_id, jfields)
    rules = {f.key: get_rule(template_id, f.key) for f in jfields}
    fact_fields: list[FieldDef] = []
    fact_map: dict[str, tuple[FieldDef, FactDef]] = {}
    for jf in jfields:
        rule = rules.get(jf.key)
        if not rule:
            continue
        for fd_raw in rule["facts"]:
            fd = FactDef.model_validate(fd_raw)
            ff = fact_field(jf, fd)
            fact_fields.append(ff)
            fact_map[ff.key] = (jf, fd)
    if fact_fields:
        fact_results = run_lookup(job_id, fact_fields, template_id, index)
        for key, res in fact_results.items():
            jf, fd = fact_map[key]
            save_fact(job_id, template_id, jf.key, fd, res)
    if progress:
        progress(0.5, "facts extracted")
    out: dict[str, FieldResult] = {}
    for i, jf in enumerate(jfields):
        rule = rules.get(jf.key)
        if not rule:
            out[jf.key] = FieldResult(key=jf.key, status="cannot_determine", confidence="low", reasoning="No rule configured.")
            continue
        out[jf.key] = decide(job_id, jf, rule, template_id)
        if progress:
            progress(0.5 + 0.5 * (i + 1) / len(jfields), f"decided {jf.key}")
    return out
