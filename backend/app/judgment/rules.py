"""Registry of deterministic ("code") judgment rules.

A rule function takes the fact values (fact_key -> value or None), the rule params
stored in judgment_rules.rule_params_json, and the field's allowed answers, and returns
one answer or "cannot_determine". Register new rules with @register.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from app.db.models import CANNOT_DETERMINE
from app.verify.normalize import FALSE_WORDS, TRUE_WORDS, parse_date, parse_decimal


@dataclass
class RuleSpec:
    name: str
    fn: Callable[[dict[str, Any], dict[str, Any], list[str]], tuple[str, str]]
    description: str
    params_example: dict[str, Any]


REGISTRY: dict[str, RuleSpec] = {}


def register(name: str, description: str, params_example: dict[str, Any]):
    def deco(fn):
        REGISTRY[name] = RuleSpec(name, fn, description, params_example)
        return fn

    return deco


class Missing(Exception):
    def __init__(self, keys: list[str]):
        self.keys = keys


def _get(facts: dict[str, Any], key: str) -> Any:
    v = facts.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise Missing([key])
    return v


def _bool(v: Any) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    return None


def _cmp(a: Any, op: str, b: Any) -> bool:
    if op in ("is_true", "is_false"):
        bv = _bool(a)
        if bv is None:
            raise ValueError(f"not a yes/no value: {a}")
        return bv if op == "is_true" else not bv
    if op in ("in", "not_in"):
        vals = [str(x).strip().lower() for x in (b if isinstance(b, list) else [b])]
        hit = str(a).strip().lower() in vals
        return hit if op == "in" else not hit
    if op == "contains":
        return str(b).strip().lower() in str(a).strip().lower()
    if op in ("before", "after", "on_or_before", "on_or_after"):
        da, db_ = parse_date(str(a)), parse_date(str(b))
        if not da or not db_:
            raise ValueError("not a date")
        return {"before": da < db_, "after": da > db_, "on_or_before": da <= db_, "on_or_after": da >= db_}[op]
    na, nb = parse_decimal(a), parse_decimal(b)
    if na is not None and nb is not None and op in (">", ">=", "<", "<=", "==", "!="):
        return {">": na > nb, ">=": na >= nb, "<": na < nb, "<=": na <= nb, "==": na == nb, "!=": na != nb}[op]
    sa, sb = str(a).strip().lower(), str(b).strip().lower()
    if op == "==":
        return sa == sb
    if op == "!=":
        return sa != sb
    raise ValueError(f"cannot compare {a!r} {op} {b!r}")


def _yes_no(params: dict, options: list[str]) -> tuple[str, str]:
    yes = params.get("yes") or next((o for o in options if o.lower() in ("yes", "true", "y")), "Yes")
    no = params.get("no") or next((o for o in options if o.lower() in ("no", "false", "n")), "No")
    return yes, no


def _operand(cond: dict, facts: dict[str, Any]) -> Any:
    if "value_fact" in cond:
        return _get(facts, cond["value_fact"])
    if cond.get("value") == "today":
        return dt.date.today().isoformat()
    return cond.get("value")


@register(
    "conditions",
    "Answer `yes` when all (mode=all) or any (mode=any) of the conditions hold, else `no`. "
    "Ops: == != > >= < <= in not_in contains is_true is_false before after on_or_before on_or_after.",
    {"mode": "all", "conditions": [{"fact": "days_present", "op": ">=", "value": 183}], "yes": "Yes", "no": "No"},
)
def rule_conditions(facts: dict[str, Any], params: dict[str, Any], options: list[str]) -> tuple[str, str]:
    yes, no = _yes_no(params, options)
    mode = params.get("mode", "all")
    results = []
    explain = []
    missing: list[str] = []
    for c in params.get("conditions", []):
        try:
            a = _get(facts, c["fact"])
            b = _operand(c, facts)
            ok = _cmp(a, c.get("op", "=="), b)
        except Missing as m:
            missing.extend(m.keys)
            continue
        results.append(ok)
        explain.append(f"{c['fact']}={a!r} {c.get('op', '==')} {b!r} -> {ok}")
    if mode == "any" and any(results):
        return yes, "; ".join(explain)
    if missing:
        raise Missing(missing)
    ans = (all(results) if mode == "all" else any(results)) if results else None
    if ans is None:
        return CANNOT_DETERMINE, "no conditions could be evaluated"
    return (yes if ans else no), "; ".join(explain)


@register(
    "threshold",
    "Answer `yes` when a numeric fact compares to a threshold (op: >=, >, <=, <), else `no`.",
    {"fact": "days_present", "op": ">=", "value": 183, "yes": "Yes", "no": "No"},
)
def rule_threshold(facts, params, options):
    yes, no = _yes_no(params, options)
    v = _get(facts, params["fact"])
    ok = _cmp(v, params.get("op", ">="), params["value"])
    return (yes if ok else no), f"{params['fact']}={v} {params.get('op', '>=')} {params['value']} -> {ok}"


@register(
    "in_list",
    "Answer `yes` when a fact's value is in a list (case-insensitive), else `no`.",
    {"fact": "visa_type", "values": ["citizen", "permanent resident"], "yes": "Yes", "no": "No"},
)
def rule_in_list(facts, params, options):
    yes, no = _yes_no(params, options)
    v = _get(facts, params["fact"])
    ok = _cmp(v, "in", params.get("values", []))
    return (yes if ok else no), f"{params['fact']}={v!r} in {params.get('values')} -> {ok}"


@register(
    "map_value",
    "Map a fact's value to an answer option via a lookup table; `default` when no entry matches.",
    {"fact": "employment_type", "mapping": {"full-time": "Employee", "contractor": "Contractor"}, "default": "cannot_determine"},
)
def rule_map_value(facts, params, options):
    v = str(_get(facts, params["fact"])).strip().lower()
    mapping = {str(k).lower(): val for k, val in params.get("mapping", {}).items()}
    ans = mapping.get(v, params.get("default", CANNOT_DETERMINE))
    return ans, f"{params['fact']}={v!r} -> {ans}"


@register(
    "age_at_least",
    "Answer `yes` when the person's age on `as_of` (a date, a fact via as_of_fact, or today) is at least `years`.",
    {"fact": "date_of_birth", "years": 18, "as_of": "today", "yes": "Yes", "no": "No"},
)
def rule_age_at_least(facts, params, options):
    yes, no = _yes_no(params, options)
    dob = parse_date(str(_get(facts, params["fact"])))
    if params.get("as_of_fact"):
        ref = parse_date(str(_get(facts, params["as_of_fact"])))
    elif params.get("as_of") and params["as_of"] != "today":
        ref = parse_date(str(params["as_of"]))
    else:
        ref = dt.date.today()
    if not dob or not ref:
        return CANNOT_DETERMINE, "date could not be parsed"
    age = ref.year - dob.year - ((ref.month, ref.day) < (dob.month, dob.day))
    ok = age >= int(params.get("years", 18))
    return (yes if ok else no), f"age {age} on {ref.isoformat()} >= {params.get('years', 18)} -> {ok}"


def run_rule(name: str, facts: dict[str, Any], params: dict[str, Any], options: list[str]) -> tuple[str, str, list[str]]:
    """Returns (answer, explanation, missing_fact_keys)."""
    spec = REGISTRY.get(name)
    if spec is None:
        return CANNOT_DETERMINE, f"unknown rule {name!r}", []
    try:
        ans, why = spec.fn(facts, params, options)
    except Missing as m:
        return CANNOT_DETERMINE, "missing facts: " + ", ".join(m.keys), m.keys
    except (ValueError, KeyError, TypeError, ArithmeticError) as e:
        return CANNOT_DETERMINE, f"rule could not be applied: {e}", []
    if options and ans != CANNOT_DETERMINE and ans not in options:
        match = next((o for o in options if o.lower() == str(ans).lower()), None)
        ans = match or CANNOT_DETERMINE
    return ans, why, []


def registry_listing() -> list[dict[str, Any]]:
    return [
        {"name": s.name, "description": s.description, "params_example": s.params_example} for s in REGISTRY.values()
    ]


_ = Decimal  # used implicitly via parse_decimal comparisons
