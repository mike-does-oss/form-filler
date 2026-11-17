"""Write values into AcroForm widgets with PyMuPDF and regenerate appearance streams."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf as fitz

from app.db.models import FieldDef
from app.verify.normalize import format_for_form


def fill_acroform(src: Path, dest: Path, fields: list[FieldDef], values: dict[str, Any], flatten: bool = False) -> list[str]:
    """Returns a list of warnings."""
    warnings: list[str] = []
    doc = fitz.open(src)
    by_name: dict[str, FieldDef] = {}
    radio_by_xref: dict[int, FieldDef] = {}
    radio_value: dict[int, str] = {}  # radio field xref -> on-state name (or "Off")
    for f in fields:
        if f.meta.get("widget_type") == "radio":
            for x in f.meta.get("xrefs", []):
                radio_by_xref[x] = f
        elif f.source_ref:
            by_name[f.source_ref] = f
    for page in doc:
        for w in page.widgets():
            if w.xref in radio_by_xref:
                f = radio_by_xref[w.xref]
                chosen = values.get(f.key)
                on_states = f.meta.get("on_states", {})
                target_state = on_states.get(chosen) if chosen is not None else None
                is_on = target_state is not None and str(w.on_state()) == str(target_state)
                w.field_value = bool(is_on)  # PyMuPDF: True/False toggles the group correctly
                w.update()
                # PyMuPDF sets the widget's /AS but (before 2.0) not the group's /V, which is what
                # readers report; write it ourselves so the result is the same on every version.
                group = _radio_group_xref(doc, w.xref)
                if is_on:
                    radio_value[group] = str(target_state)
                else:
                    radio_value.setdefault(group, "Off")
                continue
            f = by_name.get(w.field_name or "")
            if f is None or f.key not in values:
                continue
            v = values[f.key]
            if f.type == "signature":
                continue
            try:
                if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                    w.field_value = v in (True, "true", "True", "yes", "Yes")
                elif w.field_type in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
                    if v is None:
                        w.field_value = ""
                    else:
                        # map display label to export value when they differ
                        labels = f.options
                        exports = f.meta.get("option_values") or labels
                        w.field_value = exports[labels.index(v)] if v in labels else str(v)
                else:
                    text = format_for_form(f, v)
                    ml = f.meta.get("max_length")
                    if ml and len(text) > int(ml):
                        warnings.append(f"{f.key}: value truncated to {ml} characters")
                        text = text[: int(ml)]
                    w.field_value = text
                w.update()
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{f.key}: could not set value ({e})")
    for xref, state in radio_value.items():
        doc.xref_set_key(xref, "V", _pdf_name(state))
    if flatten:
        flatten_doc(doc)
    else:
        doc.need_appearances(True)
    doc.save(dest, garbage=3, deflate=True)
    return warnings


def _radio_group_xref(doc: fitz.Document, widget_xref: int) -> int:
    """The field object that holds a radio group's /V: the widget's parent, or the widget itself."""
    kind, val = doc.xref_get_key(widget_xref, "Parent")
    if kind == "xref":
        return int(val.split()[0])
    return widget_xref


def _pdf_name(state: str) -> str:
    """Encode an on-state as a PDF name object (/Name), escaping bytes that may not appear raw."""
    out = []
    for ch in state.encode("utf-8"):
        c = chr(ch)
        if 0x21 <= ch <= 0x7E and c not in "#/()<>[]{}%":
            out.append(c)
        else:
            out.append(f"#{ch:02X}")
    return "/" + "".join(out)


def flatten_doc(doc: fitz.Document) -> None:
    if hasattr(doc, "bake"):
        doc.bake(annots=True, widgets=True)
        return
    # older PyMuPDF: draw appearances by re-inserting each page as an image-free copy
    for page in doc:
        for w in list(page.widgets()):
            r = w.rect
            text = w.field_value if isinstance(w.field_value, str) else ""
            page.delete_widget(w)
            if text and text != "Off":
                page.insert_textbox(r, text, fontsize=9)
