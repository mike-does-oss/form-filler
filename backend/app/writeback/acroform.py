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
    if flatten:
        flatten_doc(doc)
    else:
        doc.need_appearances(True)
    doc.save(dest, garbage=3, deflate=True)
    return warnings


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
