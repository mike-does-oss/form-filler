"""P2: fixture forms produce schemas matching hand-written expected schemas."""

from __future__ import annotations

import json

import pytest
from rapidfuzz import fuzz

from app.forms.common import strip_question_number
from app.forms.fingerprint import fingerprint
from app.forms.service import detect_form_type, parse_form_file

from .conftest import FIXTURES

SETS = sorted(p for p in FIXTURES.iterdir() if p.is_dir() and (p / "expected_schema.json").exists())


@pytest.mark.parametrize("set_dir", SETS, ids=[s.name for s in SETS])
def test_schema_matches_expected(set_dir):
    form = next(set_dir.glob("form.*"))
    fields = parse_form_file(form, detect_form_type(form))
    expected = json.loads((set_dir / "expected_schema.json").read_text())
    assert len(fields) == len(expected), [f.label for f in fields]
    for exp in expected:
        best = max(fields, key=lambda f: fuzz.ratio(exp["label"].lower(), strip_question_number(f.label).lower()))
        score = fuzz.ratio(exp["label"].lower(), strip_question_number(best.label).lower())
        assert score >= 90, f"{exp['label']!r} best match {best.label!r} ({score})"
        assert best.type == exp["type"], f"{exp['label']}: type {best.type} != {exp['type']}"
        if "options" in exp:
            assert best.options == exp["options"], f"{exp['label']}: options {best.options}"
        if "section" in exp:
            assert best.section == exp["section"], f"{exp['label']}: section {best.section!r}"


def test_form_types():
    assert detect_form_type(FIXTURES / "01_acroform_basic" / "form.pdf") == "acroform"
    assert detect_form_type(FIXTURES / "02_docx_form" / "form.docx") == "docx"
    assert detect_form_type(FIXTURES / "07_flat_pdf" / "form.pdf") == "flat_pdf"


def test_notes_page_linked_to_question():
    fields = parse_form_file(FIXTURES / "01_acroform_basic" / "form.pdf", "acroform")
    q13 = next(f for f in fields if "resident for tax" in f.label.lower())
    assert "183 days" in (q13.instructions or "")
    total = next(f for f in fields if "total income" in f.label.lower())
    assert "sum of your annual salary" in (total.instructions or "")
    assert "add your salary" in (total.instructions or "")  # note printed under the field


def test_radio_on_states_and_required():
    fields = parse_form_file(FIXTURES / "01_acroform_basic" / "form.pdf", "acroform")
    marital = next(f for f in fields if f.label.endswith("Marital status"))
    assert marital.meta["on_states"]["De facto"] == "Defacto"
    assert next(f for f in fields if f.label.endswith("Full name")).required is True


def test_fingerprint_stable_and_distinct():
    a = fingerprint("acroform", FIXTURES / "01_acroform_basic" / "form.pdf")
    b = fingerprint("acroform", FIXTURES / "01_acroform_basic" / "form.pdf")
    c = fingerprint("acroform", FIXTURES / "04_conflicting_sources" / "form.pdf")
    assert a == b and a != c
    assert fingerprint("docx", FIXTURES / "02_docx_form" / "form.docx")


def test_docx_element_paths():
    fields = parse_form_file(FIXTURES / "02_docx_form" / "form.docx", "docx")
    refs = {f.label: f.source_ref for f in fields}
    assert refs["Full name"] == "sdt:0"
    assert refs["Email"].startswith("cell:")
    assert refs["Relationship"] == refs["Emergency contact name"][:-1] + "1"
    assert refs["Do you hold a current driver licence?"].startswith("glyph:")
