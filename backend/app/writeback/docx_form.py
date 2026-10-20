"""Write values into a Word form at the element paths recorded by the parser,
preserving each run's formatting."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import docx
from docx.oxml.ns import qn

from app.db.models import FieldDef
from app.forms.docx_form import BLANK_RUN, GLYPH_RE, W14, all_paragraphs, all_tables, leaf_sdts
from app.verify.normalize import format_for_form

CHECKED, UNCHECKED = "☒", "☐"


def _set_run_text(r, text: str) -> None:
    """Replace a run's text, keeping rPr; newlines become <w:br/>."""
    for child in list(r):
        if child.tag in (qn("w:t"), qn("w:br"), qn("w:tab")):
            r.remove(child)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i:
            r.append(r.makeelement(qn("w:br"), {}))
        t = r.makeelement(qn("w:t"), {})
        t.text = line
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        r.append(t)


def _set_sdt(sdt, f: FieldDef, value: Any) -> None:
    pr = sdt.find(qn("w:sdtPr"))
    content = sdt.find(qn("w:sdtContent"))
    if pr is not None:
        ph = pr.find(qn("w:showingPlcHdr"))
        if ph is not None:
            pr.remove(ph)
    if content is None:
        return
    runs = list(content.iter(qn("w:r")))
    if f.type == "checkbox" and pr is not None and pr.find(f"{{{W14}}}checkbox") is not None:
        cb = pr.find(f"{{{W14}}}checkbox")
        on = value in (True, "true", "True", "yes", "Yes")
        checked = cb.find(f"{{{W14}}}checked")
        if checked is None:
            checked = cb.makeelement(f"{{{W14}}}checked", {})
            cb.insert(0, checked)
        checked.set(f"{{{W14}}}val", "1" if on else "0")
        state = cb.find(f"{{{W14}}}checkedState" if on else f"{{{W14}}}uncheckedState")
        glyph = chr(int(state.get(f"{{{W14}}}val"), 16)) if state is not None else (CHECKED if on else UNCHECKED)
        if runs:
            _set_run_text(runs[0], glyph)
        return
    text = format_for_form(f, value)
    if pr is not None and pr.find(qn("w:date")) is not None and f.type == "date" and value:
        pr.find(qn("w:date")).set(qn("w:fullDate"), f"{value}T00:00:00Z")
    if not runs:
        p = content.find(qn("w:p"))
        target = p if p is not None else content
        r = target.makeelement(qn("w:r"), {})
        target.append(r)
        runs = [r]
    first = runs[0]
    # drop placeholder styling
    rpr = first.find(qn("w:rPr"))
    if rpr is not None:
        rs = rpr.find(qn("w:rStyle"))
        if rs is not None and "placeholder" in (rs.get(qn("w:val")) or "").lower():
            rpr.remove(rs)
    _set_run_text(first, text)
    for r in runs[1:]:
        r.getparent().remove(r)


def _set_cell(tc, f: FieldDef, value: Any) -> None:
    text = format_for_form(f, value)
    p = tc.find(qn("w:p"))
    if p is None:
        p = tc.makeelement(qn("w:p"), {})
        tc.append(p)
    runs = list(p.iter(qn("w:r")))
    if runs:
        _set_run_text(runs[0], text)
        for r in runs[1:]:
            if any(t.text for t in r.iter(qn("w:t"))):
                r.getparent().remove(r)
        return
    # empty cell: borrow run formatting from the label cell (or paragraph mark) so fonts match
    r = p.makeelement(qn("w:r"), {})
    tr = tc.getparent()
    cells = tr.findall(qn("w:tc"))
    i = cells.index(tc)
    src_rpr = None
    for j in range(i - 1, -1, -1):
        rr = list(cells[j].iter(qn("w:r")))
        if rr and rr[-1].find(qn("w:rPr")) is not None:
            src_rpr = copy.deepcopy(rr[-1].find(qn("w:rPr")))
            # values aren't bold just because labels are
            for b in (qn("w:b"), qn("w:bCs")):
                el = src_rpr.find(b)
                if el is not None:
                    src_rpr.remove(el)
            break
    if src_rpr is None:
        ppr = p.find(qn("w:pPr"))
        mark = ppr.find(qn("w:rPr")) if ppr is not None else None
        if mark is not None:
            src_rpr = copy.deepcopy(mark)
    if src_rpr is not None:
        r.append(src_rpr)
    p.append(r)
    _set_run_text(r, text)


def _text_nodes(p) -> list:
    return [t for t in p.iter(qn("w:t")) if t.getparent().tag == qn("w:r")]


def _replace_span(p, start: int, end: int, replacement: str) -> None:
    """Replace characters [start, end) of the paragraph's text, across runs."""
    pos = 0
    placed = False
    for t in _text_nodes(p):
        s = t.text or ""
        a, b = pos, pos + len(s)
        pos = b
        if b <= start or a >= end:
            continue
        lo, hi = max(start, a) - a, min(end, b) - a
        if not placed:
            t.text = s[:lo] + replacement + s[hi:]
            placed = True
        else:
            t.text = s[:lo] + s[hi:]
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _set_blank(p, k: int, f: FieldDef, value: Any) -> None:
    text = "".join(t.text or "" for t in _text_nodes(p))
    blanks = list(BLANK_RUN.finditer(text))
    if k >= len(blanks):
        return
    m = blanks[k]
    val = format_for_form(f, value)
    _replace_span(p, m.start(), m.end(), val if val else m.group(0))


def _set_glyph(p, f: FieldDef, value: Any) -> None:
    opts = f.meta.get("glyph_options") or f.options
    if f.type == "checkbox":
        chosen_idx = {0} if value in (True, "true", "True", "yes", "Yes") else set()
    else:
        chosen_idx = {i for i, o in enumerate(opts) if value is not None and str(o).lower() == str(value).lower()}
    text = "".join(t.text or "" for t in _text_nodes(p))
    matches = list(GLYPH_RE.finditer(text))
    # right-to-left so earlier offsets stay valid when a replacement changes length
    for i in range(len(matches) - 1, -1, -1):
        m = matches[i]
        if m.group(0).startswith("["):
            new = "[X]" if i in chosen_idx else "[ ]"
        else:
            new = CHECKED if i in chosen_idx else UNCHECKED
        _replace_span(p, m.start(), m.end(), new)


def fill_docx(src: Path, dest: Path, fields: list[FieldDef], values: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    d = docx.Document(str(src))
    body = d.element.body
    sdts = leaf_sdts(body)
    paras = all_paragraphs(body)
    tables = all_tables(body)
    for f in order_for_blanks(fields):
        if f.key not in values or f.type == "signature":
            continue
        v = values[f.key]
        ref = f.source_ref or ""
        try:
            kind, *idx = ref.split(":")
            if kind == "sdt":
                _set_sdt(sdts[int(idx[0])], f, v)
            elif kind == "cell":
                tbl = tables[int(idx[0])]
                tr = tbl.findall(qn("w:tr"))[int(idx[1])]
                tc = tr.findall(qn("w:tc"))[int(idx[2])]
                _set_cell(tc, f, v)
            elif kind == "blank":
                _set_blank(paras[int(idx[0])], int(idx[1]), f, v)
            elif kind == "glyph":
                _set_glyph(paras[int(idx[0])], f, v)
            else:
                warnings.append(f"{f.key}: unknown element path {ref!r}")
        except (IndexError, ValueError) as e:
            warnings.append(f"{f.key}: element {ref!r} not found ({e})")
    d.save(str(dest))
    return warnings


def order_for_blanks(fields: list[FieldDef]) -> list[FieldDef]:
    """Blanks in the same paragraph must be written right-to-left."""

    def key(f: FieldDef):
        m = re.match(r"blank:(\d+):(\d+)", f.source_ref or "")
        return (int(m.group(1)), -int(m.group(2))) if m else (10**9, 0)

    return sorted(fields, key=key)
