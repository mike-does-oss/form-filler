"""P6/P8: templates by fingerprint; a flat PDF mapped once is reused without re-mapping."""

from __future__ import annotations

from app import db
from app.forms.service import create_form, get_fields, update_fields
from app.writeback.flat_pdf import fill_flat_pdf

from .conftest import FIXTURES

FLAT = FIXTURES / "07_flat_pdf" / "form.pdf"


def test_flat_mapping_saved_and_reused(app_env, tmp_path):
    f1 = create_form(FLAT, "form.pdf")
    fields = get_fields(f1)
    assert all(f.bbox and f.page == 1 for f in fields)
    # user fixes the mapping: renames one field, deletes another, draws a new box
    edited = [f.model_dump() for f in fields if f.label != "Signature"]
    for f in edited:
        if f["label"] == "Preferred branch":
            f["label"] = "Preferred library branch"
    edited.append({"label": "Member number", "type": "text", "page": 1, "bbox": [400, 60, 540, 76]})
    update_fields(f1, edited, replace=True)
    labels1 = {f.label for f in get_fields(f1)}
    assert "Member number" in labels1 and "Signature" not in labels1

    # the same blank form uploaded again: template matched, mapping reused verbatim
    f2 = create_form(FLAT, "form-again.pdf")
    t1 = db.q1("SELECT template_id FROM forms WHERE id = ?", (f1,))["template_id"]
    t2 = db.q1("SELECT template_id FROM forms WHERE id = ?", (f2,))["template_id"]
    assert t1 == t2
    fields2 = get_fields(f2)
    assert {f.label for f in fields2} == labels1
    member = next(f for f in fields2 if f.label == "Member number")
    assert member.bbox == [400, 60, 540, 76] and member.source_ref.startswith("bbox:1:")

    out = tmp_path / "filled.pdf"
    fill_flat_pdf(FLAT, out, fields2, {member.key: "M-0042"})
    import pymupdf as fitz

    assert "M-0042" in fitz.open(out)[0].get_text()


def test_kind_edit_persists_to_template(app_env):
    f1 = create_form(FIXTURES / "04_conflicting_sources" / "form.pdf", "form.pdf")
    key = get_fields(f1)[0].key
    update_fields(f1, [{"key": key, "kind": "judgment"}])
    f2 = create_form(FIXTURES / "04_conflicting_sources" / "form.pdf", "form.pdf")
    assert next(f for f in get_fields(f2) if f.key == key).kind == "judgment"
    # a judgment rule draft was created for the newly-judgment field
    assert db.q1("SELECT status FROM judgment_rules WHERE field_key = ?", (key,))["status"] == "draft"
