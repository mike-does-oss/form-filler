"""Normalise raw model values to the field type, and format them for the form."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from dateutil import parser as dateparser
from rapidfuzz import fuzz, process

from app.db.models import FieldDef

TRUE_WORDS = {"true", "yes", "y", "1", "x", "checked", "on", "ticked", "✓", "☒", "☑"}
FALSE_WORDS = {"false", "no", "n", "0", "unchecked", "off", ""}
NUM_RE = re.compile(r"[-+]?\(?\$?\s?\d[\d,\s]*(?:\.\d+)?\)?")


def dayfirst_for(field: FieldDef | None, default: bool = True) -> bool:
    hint = (field.format_hint or "").upper() if field else ""
    if hint.startswith("MM"):
        return False
    if hint.startswith("DD") or hint.startswith("YYYY"):
        return True
    return default


def parse_date(text: str, dayfirst: bool = True) -> dt.date | None:
    t = str(text).strip()
    if not t:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        try:
            return dt.date.fromisoformat(t)
        except ValueError:
            return None
    if not re.search(r"\d", t):
        return None
    try:
        d = dateparser.parse(t, dayfirst=dayfirst, fuzzy=True, default=dt.datetime(1900, 1, 1))
    except (ValueError, OverflowError, TypeError):
        return None
    if d is None or (d.year == 1900 and "1900" not in t):
        return None
    return d.date()


def parse_decimal(text: Any) -> Decimal | None:
    if text is None:
        return None
    if isinstance(text, (int, float, Decimal)):
        return Decimal(str(text))
    t = str(text).strip()
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    m = re.search(r"\d[\d,\s]*(?:\.\d+)?", t)
    if not m:
        return None
    digits = re.sub(r"[,\s]", "", m.group(0))
    try:
        v = Decimal(digits)
    except InvalidOperation:
        return None
    scale = 1
    tail = t[m.end():].strip().lower()
    if re.match(r"(k|thousand)\b", tail):
        scale = 1000
    elif re.match(r"(m|mn|million)\b", tail):
        scale = 1_000_000
    v = v * scale
    return -v if neg else v


def match_option(value: str, options: list[str]) -> str | None:
    if not options:
        return value
    v = str(value).strip()
    for o in options:
        if o.lower() == v.lower():
            return o
    best = process.extractOne(v, options, scorer=fuzz.WRatio)
    if best and best[1] >= 80:
        return best[0]
    return None


def normalize_value(field: FieldDef, raw: Any, dayfirst_default: bool = True) -> tuple[Any, str | None]:
    """Return (normalised value, error). None value means empty."""
    if raw is None:
        return None, None
    if isinstance(raw, str) and not raw.strip():
        return None, None
    t = field.type
    if t == "date":
        d = parse_date(str(raw), dayfirst_for(field, dayfirst_default))
        return (d.isoformat(), None) if d else (str(raw).strip(), "could not parse date")
    if t in ("number", "currency"):
        d = parse_decimal(raw)
        if d is None:
            return str(raw).strip(), f"could not parse {t}"
        if t == "currency":
            return f"{d.quantize(Decimal('0.01'))}", None
        return format(d.normalize(), "f"), None
    if t == "checkbox":
        s = str(raw).strip().lower()
        if isinstance(raw, bool):
            return raw, None
        if s in TRUE_WORDS:
            return True, None
        if s in FALSE_WORDS:
            return False, None
        return None, f"not a checkbox value: {raw}"
    if t == "choice":
        opt = match_option(str(raw), field.options)
        if opt is None:
            return str(raw).strip(), "value is not one of the allowed options"
        return opt, None
    if t == "email":
        return str(raw).strip().lower(), None
    if t == "phone":
        return re.sub(r"\s+", " ", str(raw).strip()), None
    return str(raw).strip(), None


def format_for_form(field: FieldDef, value: Any) -> str:
    """Render a normalised value as text for the form (applies format hints)."""
    if value is None:
        return ""
    if field.type == "date":
        d = parse_date(str(value), True) if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)) else dt.date.fromisoformat(str(value))
        if d is None:
            return str(value)
        hint = (field.format_hint or field.meta.get("date_format") or "DD/MM/YYYY").upper()
        hint = hint.replace("YYYY", "%Y").replace("YY", "%y").replace("MM", "%m").replace("DD", "%d")
        try:
            return d.strftime(hint)
        except ValueError:
            return d.isoformat()
    if field.type == "currency":
        d = parse_decimal(value)
        return f"{d:,.2f}" if d is not None else str(value)
    if field.type == "checkbox":
        return "Yes" if value in (True, "true", "True", "yes") else ""
    return str(value)
