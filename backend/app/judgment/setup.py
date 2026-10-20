"""Per-template setup of judgment fields: propose facts + rubric (drafts until saved)."""

from __future__ import annotations

import logging
import re
from typing import Any

from app import db
from app.db.models import FactDef, FieldDef
from app.extraction.llm_adapter import get_llm
from app.forms.common import slugify
from app.judgment.rules import REGISTRY

log = logging.getLogger(__name__)

PROPOSE_SYSTEM = """You help set up a form-filling system. A form question needs a judgment: its answer is decided by applying criteria to facts about the person.
Propose:
1. facts: the minimal set of concrete, checkable facts the decision depends on. Each fact must be something stated in a person's documents (a date, a number, a country, a status), NOT the answer itself. Give each a snake_case key, a short label, a type (text, number, date, currency, checkbox, choice) and a direct question to ask the user if the fact can't be found (e.g. "How many days were you present in Australia between 1 July 2023 and 30 June 2024?").
2. rubric: plain-language criteria: "Answer X if ... Answer Y if ... Otherwise cannot determine." Base it on the form's own instructions when present; do not invent legal thresholds that the form doesn't state unless they are well-known and you say so.
3. code_rule: if the rubric is clear-cut (thresholds, comparisons, list membership), express it as conditions over the fact keys; else null.
Allowed condition ops: == != > >= < <= in not_in contains is_true is_false before after on_or_before on_or_after."""


def proposal_schema(options: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "label": {"type": "string"},
                        "type": {"type": "string", "enum": ["text", "number", "date", "currency", "checkbox", "choice"]},
                        "question": {"type": "string"},
                    },
                    "required": ["key", "label", "type", "question"],
                },
            },
            "rubric": {"type": "string"},
            "code_rule": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string", "enum": ["all", "any"]},
                            "conditions": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "fact": {"type": "string"},
                                        "op": {"type": "string"},
                                        "value": {"type": ["string", "number", "boolean", "null"]},
                                    },
                                    "required": ["fact", "op", "value"],
                                },
                            },
                            "yes": {"type": "string"},
                            "no": {"type": "string"},
                        },
                        "required": ["mode", "conditions", "yes", "no"],
                    },
                ]
            },
        },
        "required": ["facts", "rubric", "code_rule"],
    }


def effective_options(field: FieldDef) -> list[str]:
    if field.options:
        return list(field.options)
    if field.type == "checkbox":
        return ["Yes", "No"]
    return []


def propose_rule(field: FieldDef) -> dict[str, Any]:
    opts = effective_options(field)
    msgs = [
        {"role": "system", "content": PROPOSE_SYSTEM},
        {
            "role": "user",
            "content": "\n".join(
                [
                    f"Question: {field.label}",
                    f"Answer options: {opts or 'free text'}",
                    f"Section: {field.section or '-'}",
                    f"Form instructions for this question: {field.instructions or '(none)'}",
                ]
            ),
        },
    ]
    out = get_llm().chat_json(msgs, proposal_schema(opts))
    facts: list[FactDef] = []
    seen: set[str] = set()
    for f in out.get("facts", [])[:8]:
        key = slugify(f.get("key") or f.get("label") or "fact", 40)
        if key in seen:
            continue
        seen.add(key)
        facts.append(
            FactDef(key=key, label=f.get("label") or key, type=f.get("type") or "text", question=f.get("question"))
        )
    code = out.get("code_rule")
    params: dict[str, Any] = {}
    if isinstance(code, dict) and code.get("conditions"):
        conds = [c for c in code["conditions"] if slugify(str(c.get("fact", "")), 40) in seen]
        for c in conds:
            c["fact"] = slugify(str(c["fact"]), 40)
        yes, no = code.get("yes"), code.get("no")
        if opts:
            yes = next((o for o in opts if str(yes).strip().lower() == o.lower()), None) or next((o for o in opts if o.lower() in ("yes", "true")), None)
            no = next((o for o in opts if str(no).strip().lower() == o.lower()), None) or next((o for o in opts if o.lower() in ("no", "false")), None)
        if conds and yes and no and yes != no:
            params = {"suggested": {"rule_code": "conditions", "params": {**code, "conditions": conds, "yes": yes, "no": no}}}
    return {
        "facts": [f.model_dump() for f in facts],
        "rubric": out.get("rubric") or "",
        "params": params,
    }


def fallback_rule(field: FieldDef) -> dict[str, Any]:
    """Without an LLM: one fact per 'if ...' clause is too fragile, so use a single generic fact."""
    key = slugify(re.sub(r"^(are|is|do|does|did|have|has)\s+(you|the applicant)\s+", "", field.label.lower()), 40)
    return {
        "facts": [
            FactDef(
                key=key or "relevant_fact",
                label=f"Information needed to answer: {field.label}",
                question=f"{field.label}",
            ).model_dump()
        ],
        "rubric": field.instructions or f"Answer based on the facts. If unclear, cannot determine.",
        "params": {},
    }


def ensure_rules_for_template(template_id: int, fields: list[FieldDef], force_keys: set[str] | None = None) -> None:
    existing = {r["field_key"]: r for r in db.q("SELECT * FROM judgment_rules WHERE template_id = ?", (template_id,))}
    for f in fields:
        if f.kind != "judgment" or f.type == "signature":
            continue
        if f.key in existing and not (force_keys and f.key in force_keys):
            continue
        try:
            prop = propose_rule(f)
        except Exception as e:  # noqa: BLE001
            log.warning("fact proposal for %s failed, using fallback: %s", f.key, e)
            prop = fallback_rule(f)
        row = {
            "template_id": template_id,
            "field_key": f.key,
            "depends_on_json": db.dumps([x["key"] for x in prop["facts"]]),
            "facts_json": db.dumps(prop["facts"]),
            "rule_type": "rubric",
            "rule_code": None,
            "rule_params_json": db.dumps(prop["params"]),
            "rubric_text": prop["rubric"],
            "status": "draft",
        }
        if f.key in existing:
            db.update("judgment_rules", existing[f.key]["id"], {**row, "updated_at": _now()})
        else:
            db.insert("judgment_rules", row)


def _now() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_rule(template_id: int | None, field_key: str) -> dict | None:
    if not template_id:
        return None
    r = db.q1("SELECT * FROM judgment_rules WHERE template_id = ? AND field_key = ?", (template_id, field_key))
    if not r:
        return None
    return rule_out(r)


def rule_out(r: dict) -> dict:
    return {
        "id": r["id"],
        "template_id": r["template_id"],
        "field_key": r["field_key"],
        "depends_on": db.loads(r["depends_on_json"], []),
        "facts": db.loads(r["facts_json"], []),
        "rule_type": r["rule_type"],
        "rule_code": r["rule_code"],
        "rule_params": db.loads(r["rule_params_json"], {}),
        "rubric_text": r["rubric_text"],
        "status": r["status"],
        "flagged": bool(r["flagged"]),
        "flag_reason": r["flag_reason"],
        "updated_at": r["updated_at"],
    }


def save_rule(template_id: int, field_key: str, data: dict) -> dict:
    r = db.q1("SELECT * FROM judgment_rules WHERE template_id = ? AND field_key = ?", (template_id, field_key))
    facts = [FactDef.model_validate(x).model_dump() for x in data.get("facts", db.loads(r["facts_json"], []) if r else [])]
    rule_code = data.get("rule_code")
    if data.get("rule_type") == "code" and rule_code not in REGISTRY:
        raise ValueError(f"unknown code rule {rule_code!r}; registered: {sorted(REGISTRY)}")
    row = {
        "facts_json": db.dumps(facts),
        "depends_on_json": db.dumps(data.get("depends_on") or [f["key"] for f in facts]),
        "rule_type": data.get("rule_type", r["rule_type"] if r else "rubric"),
        "rule_code": rule_code if data.get("rule_type") == "code" else (r["rule_code"] if r else None),
        "rule_params_json": db.dumps(data.get("rule_params", db.loads(r["rule_params_json"], {}) if r else {})),
        "rubric_text": data.get("rubric_text", r["rubric_text"] if r else ""),
        "status": data.get("status", "saved"),
        "flagged": 0 if data.get("status", "saved") == "saved" else (r["flagged"] if r else 0),
        "flag_reason": None if data.get("status", "saved") == "saved" else (r["flag_reason"] if r else None),
        "updated_at": _now(),
    }
    if r:
        db.update("judgment_rules", r["id"], row)
    else:
        db.insert("judgment_rules", {"template_id": template_id, "field_key": field_key, **row})
    return get_rule(template_id, field_key)  # type: ignore[return-value]
