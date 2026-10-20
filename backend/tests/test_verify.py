from __future__ import annotations

from app.db.models import Candidate, Evidence, FieldDef, FieldResult
from app.extraction.extract import compute_derivation
from app.verify.confidence import judgment_confidence, lookup_confidence, lookup_status
from app.verify.evidence import quote_score, value_in_quote
from app.verify.normalize import format_for_form, normalize_value
from app.verify.validators import abn_valid, apply_cross_rules, validate_field


def F(type_="text", label="Field", **kw):
    return FieldDef(key="k", label=label, type=type_, **kw)


def test_normalise_types():
    assert normalize_value(F("date"), "14 March 1990")[0] == "1990-03-14"
    assert normalize_value(F("date", format_hint="DD/MM/YYYY"), "03/04/1990")[0] == "1990-04-03"
    assert normalize_value(F("date", format_hint="MM/DD/YYYY"), "03/04/1990")[0] == "1990-03-04"
    assert normalize_value(F("currency"), "$1,284.37")[0] == "1284.37"
    assert normalize_value(F("currency"), "92,500")[0] == "92500.00"
    assert normalize_value(F("number"), "148,920 km")[0] == "148920"
    assert normalize_value(F("checkbox"), "Yes")[0] is True
    assert normalize_value(F("choice", options=["Single", "Married"]), "married")[0] == "Married"
    assert normalize_value(F("choice", options=["Single", "Married"]), "Widowed")[1]


def test_format_for_form():
    assert format_for_form(F("date", format_hint="DD/MM/YYYY"), "1990-03-14") == "14/03/1990"
    assert format_for_form(F("currency"), "93784.37") == "93,784.37"


def test_quote_and_value_checks():
    chunk = "Customer Profile\n| Date of birth | 14 March 1990 |\n| Email | priya@example.com |"
    assert quote_score("Date of birth | 14 March 1990", chunk) == 100
    assert quote_score("Date of birth: 14  march 1990", chunk) >= 90
    assert quote_score("Born on 3 June 1985", chunk) < 90
    assert value_in_quote(F("date"), "1990-03-14", "Date of birth: 14 March 1990", 90)
    assert not value_in_quote(F("date"), "1990-03-15", "Date of birth: 14 March 1990", 90)
    assert value_in_quote(F("currency"), "1284.37", "Total interest earned $1,284.37", 90)
    assert value_in_quote(F("phone"), "0412 345 678", "Mobile 0412345678", 90)
    assert value_in_quote(F("text"), "Priya Natarajan", "Account holder: Priya Natarajan", 90)


def test_abn_and_validators():
    assert abn_valid("51 824 753 556")
    assert not abn_valid("51 824 753 557")
    assert validate_field(F("text", label="Employer ABN"), "51 824 753 557") == ["ABN checksum is invalid"]
    assert validate_field(F("date", label="Date of birth"), "2999-01-01")
    assert validate_field(F("email"), "not-an-email")
    assert validate_field(F("phone"), "0412 345 678") == []
    assert validate_field(F("text", required=True), None) == ["required field is empty"]


def test_cross_rules():
    fields = {k: FieldDef(key=k, label=k, type="date") for k in ("start", "end")}
    results = {"start": FieldResult(key="start", value="2024-05-01"), "end": FieldResult(key="end", value="2024-01-01")}
    apply_cross_rules([{"type": "date_after", "field": "end", "after": "start"}], results, fields)
    assert results["end"].validator_errors
    fields2 = {k: FieldDef(key=k, label=k, type="currency") for k in ("a", "b", "t")}
    res2 = {"a": FieldResult(key="a", value="1"), "b": FieldResult(key="b", value="2"), "t": FieldResult(key="t", value="4")}
    apply_cross_rules([{"type": "sum_equals", "total": "t", "parts": ["a", "b"]}], res2, fields2)
    assert res2["t"].validator_errors == ["total does not equal the sum of its parts"]


def test_derivation_arithmetic_in_code():
    assert str(compute_derivation("sum", ["$92,500.00", "1,284.37"], F("currency"))) == "93784.37"
    assert str(compute_derivation("difference", ["100", "40"], F("number"))) == "60"
    assert compute_derivation("age_years", ["1958-02-02", "2025-01-01"], F("number")) == 66


def test_status_and_confidence():
    f = F("text")
    ok = FieldResult(key="k", value="x", evidence=[Evidence(quote="x", verified=True, value_in_quote=True)])
    ok.status = lookup_status(ok)
    assert ok.status == "verified" and lookup_confidence(f, ok) == "high"
    ok.validator_errors = ["bad"]
    assert lookup_confidence(f, ok) == "medium"
    bad = FieldResult(key="k", value="x", evidence=[Evidence(quote="y", verified=False)])
    bad.status = lookup_status(bad)
    assert bad.status == "unverified" and lookup_confidence(f, bad) == "low"
    conf = FieldResult(key="k", value=None, candidates=[Candidate(value="a"), Candidate(value="b")])
    conf.status = lookup_status(conf)
    assert conf.status == "conflict" and lookup_confidence(f, conf) == "low"
    nf = FieldResult(key="k")
    nf.status = lookup_status(nf)
    assert nf.status == "not_found"
    assert lookup_confidence(F("text", required=True), nf) == "low"
    # judgment fields are never high
    j = FieldResult(key="k", value="Yes", status="judged", run_agreement=1.0)
    assert judgment_confidence(j, "rubric", True) == "medium"
    j.run_agreement = 0.8
    assert judgment_confidence(j, "rubric", True) == "low"
    assert judgment_confidence(j, "code", True) == "medium"
    assert judgment_confidence(j, "code", False) == "low"
