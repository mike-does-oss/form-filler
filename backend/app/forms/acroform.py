"""Fillable PDF (AcroForm) parser.

Widget names are often meaningless (`Text12`), so each field's label is derived from
the tooltip (when meaningful) or the nearest text to the left of / above the widget
(right of it for checkboxes and radio buttons).
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf as fitz

from app.db.models import FieldDef
from app.forms.common import (
    FOOTNOTE_LINE,
    Heading,
    InstructionContext,
    NoteBlock,
    TextLine,
    assign_sections,
    clean_label,
    find_format_hint,
    footnote_marker,
    infer_text_type,
    is_heading,
    is_meaningless_name,
    is_required_label,
    link_instructions,
    parse_notes_text,
    question_number,
    slugify,
    unique_key,
)

WT = {
    fitz.PDF_WIDGET_TYPE_TEXT: "text",
    fitz.PDF_WIDGET_TYPE_CHECKBOX: "checkbox",
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "radio",
    fitz.PDF_WIDGET_TYPE_COMBOBOX: "choice",
    fitz.PDF_WIDGET_TYPE_LISTBOX: "choice",
    fitz.PDF_WIDGET_TYPE_SIGNATURE: "signature",
    fitz.PDF_WIDGET_TYPE_BUTTON: "button",
}
FLAG_REQUIRED = 1 << 1
FLAG_MULTILINE = 1 << 12


def is_acroform(path: Path) -> bool:
    try:
        doc = fitz.open(path)
    except Exception:  # noqa: BLE001
        return False
    if not doc.is_pdf:
        return False
    return any(True for page in doc for _ in page.widgets())


def page_lines(page: fitz.Page) -> list[TextLine]:
    out: list[TextLine] = []
    d = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
    for b in d["blocks"]:
        for line in b.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            # split lines at big horizontal gaps (e.g. "Name: ____   Date: ____")
            groups: list[list[dict]] = [[spans[0]]]
            for s in spans[1:]:
                if s["bbox"][0] - groups[-1][-1]["bbox"][2] > 25:
                    groups.append([s])
                else:
                    groups[-1].append(s)
            for g in groups:
                text = "".join(s["text"] for s in g).strip()
                bbox = [g[0]["bbox"][0], min(s["bbox"][1] for s in g), g[-1]["bbox"][2], max(s["bbox"][3] for s in g)]
                out.append(
                    TextLine(
                        page=page.number + 1,
                        text=text,
                        bbox=bbox,
                        size=max(s["size"] for s in g),
                        bold=all(bool(s["flags"] & 16) or "Bold" in s.get("font", "") for s in g),
                    )
                )
    return out


def _voverlap(a: list[float], b: list[float]) -> float:
    return max(0.0, min(a[3], b[3]) - max(a[1], b[1])) / max(1e-6, min(a[3] - a[1], b[3] - b[1]))


def _hoverlap(a: list[float], b: list[float]) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) / max(1e-6, min(a[2] - a[0], b[2] - b[0]))


def _inside_any(box: list[float], rects: list[list[float]]) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in rects)


def nearest_label(wrect: list[float], lines: list[TextLine], widget_rects: list[list[float]], prefer_right: bool) -> TextLine | None:
    cands: list[tuple[float, TextLine]] = []
    for ln in lines:
        if _inside_any(ln.bbox, widget_rects):
            continue
        # left on same line
        if _voverlap(ln.bbox, wrect) > 0.4 and ln.bbox[2] <= wrect[0] + 4:
            dist = wrect[0] - ln.bbox[2]
            if dist < 260:
                cands.append((dist * (1.5 if prefer_right else 1.0), ln))
        # right on same line (checkboxes)
        elif _voverlap(ln.bbox, wrect) > 0.4 and ln.bbox[0] >= wrect[2] - 4:
            dist = ln.bbox[0] - wrect[2]
            if dist < (60 if prefer_right else 0):
                cands.append((dist * (0.5 if prefer_right else 3.0), ln))
        # above
        elif ln.bbox[3] <= wrect[1] + 3 and wrect[1] - ln.bbox[3] < 28 and (
            _hoverlap(ln.bbox, wrect) > 0.2 or abs(ln.bbox[0] - wrect[0]) < 40
        ):
            dist = (wrect[1] - ln.bbox[3]) * 2 + abs(ln.bbox[0] - wrect[0]) * 0.2 + 15
            cands.append((dist, ln))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    return cands[0][1]


def parse_acroform(path: Path) -> list[FieldDef]:
    doc = fitz.open(path)
    fields: list[FieldDef] = []
    used: set[str] = set()
    headings: list[Heading] = []
    ctx = InstructionContext()
    radio_groups: dict[str, FieldDef] = {}
    all_lines: list[TextLine] = []
    pages_with_widgets: set[int] = set()

    for page in doc:
        if any(True for _ in page.widgets()):
            pages_with_widgets.add(page.number + 1)

    for page in doc:
        pno = page.number + 1
        lines = page_lines(page)
        all_lines.extend(lines)
        sizes = sorted(ln.size for ln in lines)
        body = sizes[len(sizes) // 2] if sizes else 10
        widgets = list(page.widgets())
        wrects = [list(w.rect) for w in widgets]

        if pno not in pages_with_widgets:
            # instruction page: "Notes to question 7 ..." blocks
            ctx.notes.extend(parse_notes_text(pno, [ln.text for ln in lines]))
            continue

        label_lines: set[int] = set()
        for ln in lines:
            if is_heading(ln, body):
                headings.append(Heading(page=pno, y=ln.bbox[1], text=clean_label(ln.text)))
            fm = FOOTNOTE_LINE.match(ln.text)
            if fm and ln.size < body and ln.bbox[1] > page.rect.height * 0.75:
                ctx.footnotes[fm.group(1)] = fm.group(2).strip()

        for w in widgets:
            wtype = WT.get(w.field_type, "text")
            if wtype == "button":
                continue
            rect = list(w.rect)
            name = w.field_name or f"field_{w.xref}"
            prefer_right = wtype in ("checkbox", "radio")
            lab_line = nearest_label(rect, lines, wrects, prefer_right)
            tooltip = (w.field_label or "").strip()
            raw_label = ""
            if tooltip and not is_meaningless_name(tooltip):
                raw_label = tooltip
            elif lab_line is not None:
                raw_label = lab_line.text
                label_lines.add(id(lab_line))
            elif not is_meaningless_name(name):
                raw_label = re.sub(r"[_\-.]+", " ", name.split(".")[-1])
            else:
                raw_label = name
            label = clean_label(raw_label) or name
            required = bool(w.field_flags & FLAG_REQUIRED) or is_required_label(raw_label)

            if wtype == "radio":
                # one choice field per radio group, options from each button's label
                on = w.on_state()
                opt_label_line = nearest_label(rect, lines, wrects, prefer_right=True)
                opt_label = clean_label(opt_label_line.text) if opt_label_line else str(on)
                grp = radio_groups.get(name)
                if grp is None:
                    # the group label is the nearest non-option text to the left / above the first button
                    q_line = _group_question_line(rect, lines, wrects, opt_label_line)
                    glabel = clean_label(q_line.text) if q_line else (label if not is_meaningless_name(name) else name)
                    if tooltip and not is_meaningless_name(tooltip):
                        glabel = clean_label(tooltip)
                    grp = FieldDef(
                        key="",
                        label=glabel,
                        type="choice",
                        required=required,
                        page=pno,
                        bbox=rect,
                        source_ref=name,
                        meta={"widget_type": "radio", "on_states": {}, "xrefs": []},
                    )
                    radio_groups[name] = grp
                    fields.append(grp)
                grp.options.append(opt_label)
                grp.meta["on_states"][opt_label] = on
                grp.meta["xrefs"].append(w.xref)
                grp.bbox = [min(grp.bbox[0], rect[0]), min(grp.bbox[1], rect[1]), max(grp.bbox[2], rect[2]), max(grp.bbox[3], rect[3])]
                continue

            meta: dict = {"widget_type": wtype, "xref": w.xref, "widget_name": name}
            ftype = wtype
            options: list[str] = []
            if wtype == "checkbox":
                meta["on_state"] = w.on_state()
            elif wtype == "choice":
                vals = w.choice_values or []
                options = [v[1] if isinstance(v, (list, tuple)) else str(v) for v in vals]
                meta["option_values"] = [v[0] if isinstance(v, (list, tuple)) else str(v) for v in vals]
            elif wtype == "text":
                ftype = infer_text_type(label, multiline=bool(w.field_flags & FLAG_MULTILINE))
                if w.text_maxlen:
                    meta["max_length"] = w.text_maxlen
            meta["question_no"] = question_number(label)
            mk = footnote_marker(raw_label)
            if mk:
                meta["footnote_marker"] = mk
            fields.append(
                FieldDef(
                    key="",
                    label=label,
                    type=ftype,
                    required=required,
                    options=options,
                    format_hint=find_format_hint(raw_label),
                    page=pno,
                    bbox=rect,
                    source_ref=name,
                    meta=meta,
                )
            )

        # notes beside / below fields: small "Note:" text close under a widget
        for f in [f for f in fields if f.page == pno]:
            for ln in lines:
                if id(ln) in label_lines or _inside_any(ln.bbox, wrects):
                    continue
                t = ln.text.strip()
                below = 0 <= ln.bbox[1] - f.bbox[3] < 22 and _hoverlap(ln.bbox, f.bbox) > 0.3
                beside = _voverlap(ln.bbox, f.bbox) > 0.4 and 0 <= ln.bbox[0] - f.bbox[2] < 120
                if (below or beside) and re.match(r"^(note|hint|e\.g\.|example|\(|format|if )", t, re.I):
                    f.instructions = ((f.instructions or "") + " " + t).strip()
                    hint = find_format_hint(t)
                    if hint and not f.format_hint:
                        f.format_hint = hint

    for f in fields:
        f.meta.setdefault("question_no", question_number(f.label))
        f.meta["label_raw"] = f.label
    assign_sections(fields, headings)
    link_instructions(fields, ctx)
    for i, f in enumerate(sorted(fields, key=lambda x: (x.page or 0, round((x.bbox or [0, 0])[1] / 6), (x.bbox or [0])[0]))):
        f.order_index = i
        if f.type == "choice" and f.meta.get("widget_type") == "radio" and not f.options:
            f.options = list(f.meta.get("on_states", {}))
    fields.sort(key=lambda f: f.order_index)
    for f in fields:
        f.key = unique_key(slugify(f.label), used)
    return fields


def _group_question_line(rect, lines, wrects, opt_line):
    best = None
    best_d = 1e9
    for ln in lines:
        if ln is opt_line or _inside_any(ln.bbox, wrects):
            continue
        if _voverlap(ln.bbox, rect) > 0.4 and ln.bbox[2] <= rect[0] + 2:
            d = rect[0] - ln.bbox[2]
        elif ln.bbox[3] <= rect[1] + 2 and rect[1] - ln.bbox[3] < 30:
            d = (rect[1] - ln.bbox[3]) * 2 + abs(ln.bbox[0] - rect[0]) * 0.3 + 20
        else:
            continue
        if d < best_d:
            best, best_d = ln, d
    return best


def first_page_text(path: Path) -> str:
    doc = fitz.open(path)
    return doc[0].get_text("text") if len(doc) else ""


def widget_names(path: Path) -> list[str]:
    doc = fitz.open(path)
    return sorted({w.field_name or "" for page in doc for w in page.widgets()})


_ = NoteBlock  # re-export for type users
