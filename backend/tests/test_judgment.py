"""P7: code rules, missing facts -> cannot_determine, rubric self-consistency, user answers."""

from __future__ import annotations

from app import db
from app.db.models import CANNOT_DETERMINE, FieldDef
from app.judgment.rules import run_rule
from app.judgment.runtime import decide, save_fact
from app.db.models import FactDef, FieldResult, Evidence


def test_code_rules():
    params = {"mode": "all", "conditions": [{"fact": "days", "op": ">=", "value": 183}]}
    assert run_rule("conditions", {"days": "241"}, params, ["Yes", "No"])[0] == "Yes"
    assert run_rule("conditions", {"days": "100"}, params, ["Yes", "No"])[0] == "No"
    ans, _, missing = run_rule("conditions", {"days": None}, params, ["Yes", "No"])
    assert ans == CANNOT_DETERMINE and missing == ["days"]
    assert run_rule("age_at_least", {"dob": "1958-02-02"}, {"fact": "dob", "years": 65, "as_of": "2025-01-01"}, ["Yes", "No"])[0] == "Yes"
    assert run_rule("in_list", {"visa": "Citizen"}, {"fact": "visa", "values": ["citizen"]}, ["Yes", "No"])[0] == "Yes"
    assert run_rule("nope", {}, {}, [])[0] == CANNOT_DETERMINE


def _setup_job():
    tid = db.insert("form_templates", {"fingerprint": "t", "name": "t", "form_type": "acroform"})
    fid = db.insert("forms", {"filename": "f", "stored_path": "f", "form_type": "acroform", "fingerprint": "t", "template_id": tid})
    jid = db.insert("jobs", {"form_id": fid})
    return tid, fid, jid


FIELD = FieldDef(key="resident", label="Were you a resident?", type="choice", kind="judgment", options=["Yes", "No"])
RULE = {
    "facts": [{"key": "days", "label": "Days present", "type": "number", "question": "How many days were you present?", "required": True}],
    "rule_type": "rubric",
    "rubric_text": "Yes if days >= 183, else No.",
    "rule_params": {},
}


def _verified(value):
    return FieldResult(key="x", value=value, status="verified", confidence="high", evidence=[Evidence(quote=f"{value} days", verified=True)])


def test_missing_fact_gives_cannot_determine_without_llm(app_env):
    tid, _, jid = _setup_job()
    save_fact(jid, tid, "resident", FactDef(key="days", label="Days present"), FieldResult(key="x", status="not_found"))
    res = decide(jid, FIELD, RULE, tid)
    assert res.status == "cannot_determine" and res.confidence == "low"
    assert res.missing_facts[0]["question"] == "How many days were you present?"
    assert app_env.calls == []  # no model call


def test_rubric_self_consistency(app_env):
    tid, _, jid = _setup_job()
    save_fact(jid, tid, "resident", FactDef(key="days", label="Days present"), _verified("241"))
    answers = iter(["Yes", "Yes", "No", "Yes", "Yes"])

    def responder(messages, schema, temperature=None, seed=None):
        if "answer" in schema.get("properties", {}):
            return {"facts_used": [{"fact_key": "days", "value": "241"}], "reasoning": "241 >= 183", "answer": next(answers), "missing_facts": []}

    app_env.responder = responder
    res = decide(jid, FIELD, RULE, tid)
    assert res.value == "Yes" and res.run_agreement == 0.8 and res.confidence == "low"
    assert len(app_env.calls) == 5 and all(c["temperature"] == 0.6 for c in app_env.calls)

    app_env.calls.clear()
    app_env.responder = lambda m, s, **k: {"facts_used": [], "reasoning": "ok", "answer": "Yes", "missing_facts": []}
    res = decide(jid, FIELD, RULE, tid)
    assert res.run_agreement == 1.0 and res.confidence == "medium" and res.status == "judged"


def test_code_rule_and_user_answer(app_env):
    tid, _, jid = _setup_job()
    rule = {**RULE, "rule_type": "code", "rule_code": "threshold", "rule_params": {"fact": "days", "op": ">=", "value": 183}}
    save_fact(jid, tid, "resident", FactDef(key="days", label="Days present"), FieldResult(key="x", status="not_found"))
    assert decide(jid, FIELD, rule, tid).status == "cannot_determine"
    save_fact(jid, tid, "resident", FactDef(key="days", label="Days present"), None, source="user", value="200")
    # a later model re-run must not overwrite the user's answer
    save_fact(jid, tid, "resident", FactDef(key="days", label="Days present"), FieldResult(key="x", status="not_found"))
    res = decide(jid, FIELD, rule, tid)
    assert res.value == "Yes" and res.status == "judged" and res.confidence == "medium"
    assert app_env.calls == []
