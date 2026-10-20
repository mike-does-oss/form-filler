"""Deterministic validators, pluggable per field type and per template (cross-field rules)."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Any, Callable

from app.db.models import FieldDef, FieldResult
from app.verify.normalize import parse_date, parse_decimal

Validator = Callable[[FieldDef, Any], str | None]
_TYPE_VALIDATORS: dict[str, list[Validator]] = {}
_LABEL_VALIDATORS: list[tuple[re.Pattern, Validator]] = []
_CROSS_RULES: dict[str, Callable[[dict, dict[str, FieldResult], dict[str, FieldDef]], list[tuple[str, str]]]] = {}


def for_type(*types: str):
    def deco(fn: Validator) -> Validator:
        for t in types:
            _TYPE_VALIDATORS.setdefault(t, []).append(fn)
        return fn

    return deco


def for_label(pattern: str):
    def deco(fn: Validator) -> Validator:
        _LABEL_VALIDATORS.append((re.compile(pattern, re.I), fn))
        return fn

    return deco


def cross_rule(name: str):
    def deco(fn):
        _CROSS_RULES[name] = fn
        return fn

    return deco


# ---------------------------------------------------------------- per type
@for_type("date")
def v_date(field: FieldDef, value: Any) -> str | None:
    d = parse_date(str(value))
    if d is None:
        return "not a valid date"
    lab = field.label.lower()
    today = dt.date.today()
    if re.search(r"\b(birth|dob|born)\b", lab):
        if d > today:
            return "date of birth is in the future"
        if today.year - d.year > 120:
            return "date of birth is implausibly old"
    if d.year < 1900 or d.year > today.year + 50:
        return "date is outside the plausible range"
    if re.search(r"\b(issue|issued)\b", lab) and d > today:
        return "issue date is in the future"
    return None


@for_type("number", "currency")
def v_number(field: FieldDef, value: Any) -> str | None:
    d = parse_decimal(value)
    if d is None:
        return f"not a valid {field.type}"
    if re.search(r"\b(age|days|number of|how many)\b", field.label.lower()) and d < 0:
        return "must not be negative"
    return None


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


@for_type("email")
def v_email(field: FieldDef, value: Any) -> str | None:
    return None if EMAIL_RE.match(str(value).strip()) else "not a valid email address"


@for_type("phone")
def v_phone(field: FieldDef, value: Any) -> str | None:
    s = str(value).strip()
    digits = re.sub(r"\D", "", s)
    if not re.fullmatch(r"[+()\d\s.\-]+(ext\.?\s*\d+)?", s, re.I):
        return "phone contains invalid characters"
    if not 6 <= len(digits) <= 15:
        return "phone number has the wrong number of digits"
    return None


@for_type("choice")
def v_choice(field: FieldDef, value: Any) -> str | None:
    if field.options and value not in field.options:
        return "value is not one of the allowed options"
    return None


@for_type("text", "multiline")
def v_maxlen(field: FieldDef, value: Any) -> str | None:
    ml = field.meta.get("max_length")
    if ml and len(str(value)) > int(ml):
        return f"longer than the field's maximum length ({ml})"
    return None


# ---------------------------------------------------------------- per label
def abn_valid(abn: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", abn)]
    if len(digits) != 11:
        return False
    digits[0] -= 1
    weights = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
    return sum(d * w for d, w in zip(digits, weights)) % 89 == 0


@for_label(r"\bABN\b|australian business number")
def v_abn(field: FieldDef, value: Any) -> str | None:
    return None if abn_valid(str(value)) else "ABN checksum is invalid"


def validate_field(field: FieldDef, value: Any) -> list[str]:
    errors: list[str] = []
    if value is None or value == "":
        if field.required:
            errors.append("required field is empty")
        return errors
    for fn in _TYPE_VALIDATORS.get(field.type, []):
        e = fn(field, value)
        if e:
            errors.append(e)
    for pat, fn in _LABEL_VALIDATORS:
        if pat.search(field.label) or pat.search(field.key.replace("_", " ")):
            e = fn(field, value)
            if e:
                errors.append(e)
    return errors


# ---------------------------------------------------------------- cross-field rules
@cross_rule("date_after")
def r_date_after(rule: dict, results: dict[str, FieldResult], fields: dict[str, FieldDef]):
    a, b = results.get(rule["field"]), results.get(rule["after"])
    if not a or not b or a.value is None or b.value is None:
        return []
    da, db_ = parse_date(str(a.value)), parse_date(str(b.value))
    if da and db_ and not da > db_:
        return [(rule["field"], f"must be after {fields[rule['after']].label if rule['after'] in fields else rule['after']}")]
    return []


@cross_rule("sum_equals")
def r_sum_equals(rule: dict, results: dict[str, FieldResult], fields: dict[str, FieldDef]):
    total = results.get(rule["total"])
    if not total or total.value is None:
        return []
    parts = [parse_decimal(results[p].value) for p in rule["parts"] if p in results and results[p].value is not None]
    t = parse_decimal(total.value)
    if t is None or not parts:
        return []
    if abs(sum(parts, Decimal(0)) - t) > Decimal(str(rule.get("tolerance", "0.01"))):
        return [(rule["total"], "total does not equal the sum of its parts")]
    return []


@cross_rule("required_if")
def r_required_if(rule: dict, results: dict[str, FieldResult], fields: dict[str, FieldDef]):
    cond = results.get(rule["if_field"])
    target = results.get(rule["field"])
    if cond and str(cond.value).lower() == str(rule.get("equals", "true")).lower():
        if not target or target.value in (None, ""):
            return [(rule["field"], f"required because {rule['if_field']} is {rule.get('equals', 'true')}")]
    return []


def apply_cross_rules(rules: list[dict], results: dict[str, FieldResult], fields: dict[str, FieldDef]) -> None:
    for rule in rules:
        fn = _CROSS_RULES.get(rule.get("type", ""))
        if not fn:
            continue
        try:
            for key, msg in fn(rule, results, fields):
                if key in results and msg not in results[key].validator_errors:
                    results[key].validator_errors.append(msg)
        except (KeyError, TypeError):
            continue


def registered_cross_rules() -> list[str]:
    return sorted(_CROSS_RULES)
