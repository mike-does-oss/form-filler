"""Word form parser.

Detects, in order:
  1. content controls (w:sdt)                      -> source_ref "sdt:{i}"
  2. label/value tables (empty cell after a label)  -> source_ref "cell:{t}:{r}:{c}"
  3. label lines with blank runs ("Name: ______")   -> source_ref "blank:{p}:{k}"
  4. checkbox glyph groups ("☐ Yes ☐ No")           -> source_ref "glyph:{p}"
Element indices are document-order positions from `body.iter()`, so write-back can
find the same element again in the original file.
"""

from __future__ import annotations

import re
from pathlib import Path

import docx
from docx.oxml.ns import qn

from app.db.models import FieldDef
from app.forms.common import (
    InstructionContext,
    clean_label,
    find_format_hint,
    infer_text_type,
    is_required_label,
    link_instructions,
    parse_notes_text,
    question_number,
    slugify,
    unique_key,
)

W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
BLANK_RUN = re.compile(r"_{3,}|\.{6,}|…{2,}")
GLYPHS = "☐☒☑"
GLYPH_RE = re.compile(r"[☐☒☑]|\[\s?[xX]?\s?\]")
NOTES_HEADING = re.compile(r"\b(notes?|instructions?|guidance|explanatory|how to complete)\b", re.I)


def el_text(el) -> str:
    return "".join(t.text or "" for t in el.iter(qn("w:t")))


def is_placeholder(sdt) -> bool:
    pr = sdt.find(qn("w:sdtPr"))
    return pr is not None and pr.find(qn("w:showingPlcHdr")) is not None


def leaf_sdts(body) -> list:
    return [s for s in body.iter(qn("w:sdt")) if s.find(".//" + qn("w:sdt")) is None]


def all_paragraphs(body) -> list:
    return list(body.iter(qn("w:p")))


def all_tables(body) -> list:
    return list(body.iter(qn("w:tbl")))


def _ancestor(el, tag: str):
    p = el.getparent()
    while p is not None:
        if p.tag == qn(tag):
            return p
        p = p.getparent()
    return None


def _para_style(p) -> str:
    ppr = p.find(qn("w:pPr"))
    if ppr is None:
        return ""
    ps = ppr.find(qn("w:pStyle"))
    return ps.get(qn("w:val")) if ps is not None else ""


def _para_all_bold(p) -> bool:
    runs = [r for r in p.iter(qn("w:r")) if el_text(r).strip()]
    if not runs:
        return False
    for r in runs:
        rpr = r.find(qn("w:rPr"))
        b = rpr.find(qn("w:b")) if rpr is not None else None
        if b is None or b.get(qn("w:val")) in ("0", "false"):
            return False
    return True


def _is_heading_para(p, style_names: dict[str, str]) -> bool:
    text = el_text(p).strip()
    if not text or len(text) > 90 or BLANK_RUN.search(text) or GLYPH_RE.search(text) or text.endswith(":"):
        return False
    style = style_names.get(_para_style(p), _para_style(p)).lower()
    if style.startswith("heading") or style == "title":
        return True
    if _ancestor(p, "w:tbl") is not None:
        return False
    return _para_all_bold(p) and not text.endswith("?") and len(text) < 70


def _left_cell_text(tc) -> str:
    tr = tc.getparent()
    cells = tr.findall(qn("w:tc"))
    i = cells.index(tc)
    for j in range(i - 1, -1, -1):
        t = el_text(cells[j]).strip()
        if t:
            return t
    return ""


def _header_above_text(tc) -> str:
    tr = tc.getparent()
    tbl = tr.getparent()
    rows = tbl.findall(qn("w:tr"))
    ri = rows.index(tr)
    ci = tr.findall(qn("w:tc")).index(tc)
    for k in range(ri - 1, -1, -1):
        cells = rows[k].findall(qn("w:tc"))
        if ci < len(cells):
            t = el_text(cells[ci]).strip()
            if t:
                return t
    return ""


def _prev_para_text(p, paras: list, pidx: dict) -> str:
    i = pidx.get(id(p), 0)
    for j in range(i - 1, max(-1, i - 4), -1):
        t = el_text(paras[j]).strip()
        if t:
            return t
    return ""


def _sdt_meta(sdt) -> tuple[str, list[str], dict, str]:
    pr = sdt.find(qn("w:sdtPr"))
    ftype, options, meta, label = "text", [], {}, ""
    if pr is None:
        return ftype, options, meta, label
    alias = pr.find(qn("w:alias"))
    tag = pr.find(qn("w:tag"))
    if alias is not None and alias.get(qn("w:val")):
        label = alias.get(qn("w:val"))
    elif tag is not None and tag.get(qn("w:val")) and not re.fullmatch(r"[\w\-]*\d+", tag.get(qn("w:val"))):
        label = re.sub(r"[_\-]+", " ", tag.get(qn("w:val")))
    if pr.find(qn("w:date")) is not None:
        ftype = "date"
        fmt = pr.find(qn("w:date")).find(qn("w:dateFormat"))
        if fmt is not None:
            meta["date_format"] = fmt.get(qn("w:val"))
    elif pr.find(f"{{{W14}}}checkbox") is not None:
        ftype = "checkbox"
    else:
        lst = pr.find(qn("w:dropDownList"))
        if lst is None:
            lst = pr.find(qn("w:comboBox"))
        if lst is not None:
            ftype = "choice"
            for li in lst.findall(qn("w:listItem")):
                disp = li.get(qn("w:displayText")) or li.get(qn("w:value"))
                if disp and not disp.lower().startswith("choose an item"):
                    options.append(disp)
        elif pr.find(qn("w:text")) is not None:
            t = pr.find(qn("w:text"))
            if t.get(qn("w:multiLine")) in ("1", "true"):
                meta["multiline"] = True
    return ftype, options, meta, label


def _text_before_in_para(el, p) -> str:
    """Text in paragraph p that precedes element el."""
    out = []
    for node in p.iter():
        if node is el:
            break
        if node.tag == qn("w:t") and _ancestor(node, "w:sdt") is None:
            out.append(node.text or "")
    return "".join(out)


def parse_docx_form(path: Path) -> list[FieldDef]:
    d = docx.Document(str(path))
    body = d.element.body
    style_names = {s.style_id: s.name for s in d.styles}
    # keep the lxml proxies alive so id() stays stable for the whole parse
    all_els = list(body.iter())
    order = {id(el): i for i, el in enumerate(all_els)}
    paras = all_paragraphs(body)
    pidx = {id(p): i for i, p in enumerate(paras)}
    tables = all_tables(body)
    tidx = {id(t): i for i, t in enumerate(tables)}

    candidates: list[tuple[int, FieldDef]] = []  # (doc order, field)
    covered_paras: set[int] = set()
    covered_cells: set[int] = set()

    # 1. content controls
    for i, sdt in enumerate(leaf_sdts(body)):
        ftype, options, meta, label = _sdt_meta(sdt)
        p = sdt if sdt.tag == qn("w:p") else _ancestor(sdt, "w:p")
        tc = _ancestor(sdt, "w:tc")
        if not label:
            before = _text_before_in_para(sdt, p) if p is not None else ""
            if clean_label(before):
                label = before
            elif tc is not None:
                label = _left_cell_text(tc) or _header_above_text(tc)
            if not clean_label(label) and p is not None:
                label = _prev_para_text(p, paras, pidx)
        if tc is not None:
            covered_cells.add(id(tc))
        if p is not None:
            covered_paras.add(id(p))
        for inner_p in sdt.iter(qn("w:p")):
            covered_paras.add(id(inner_p))
        if ftype == "text":
            ftype = infer_text_type(clean_label(label), multiline=bool(meta.get("multiline")))
        meta["question_no"] = question_number(clean_label(label))
        candidates.append(
            (
                order[id(sdt)],
                FieldDef(
                    key="",
                    label=clean_label(label) or f"Field {i + 1}",
                    type=ftype,
                    options=options,
                    required=is_required_label(label),
                    format_hint=find_format_hint(label, meta.get("date_format")),
                    source_ref=f"sdt:{i}",
                    meta={**meta, "element": "sdt", "placeholder": is_placeholder(sdt)},
                ),
            )
        )

    # 2. label/value table cells
    for tbl in tables:
        ti = tidx[id(tbl)]
        for ri, tr in enumerate(tbl.findall(qn("w:tr"))):
            cells = tr.findall(qn("w:tc"))
            for ci, tc in enumerate(cells):
                if id(tc) in covered_cells or ci == 0:
                    continue
                txt = el_text(tc)
                if txt.strip() and not re.fullmatch(r"[\s_.…]*", txt):
                    continue
                if GLYPH_RE.search(txt):
                    continue
                left = el_text(cells[ci - 1]).strip()
                label = left if left and not re.fullmatch(r"[\s_.…]*", left) else ""
                header = ""
                if not label:
                    header = _header_above_text(tc)
                    label = header
                if not label or len(label) > 160:
                    continue
                if header:
                    # data-table rows: number repeated column headers by row
                    label = f"{header} (row {ri})"
                for p in tc.iter(qn("w:p")):
                    covered_paras.add(id(p))
                covered_cells.add(id(tc))
                lab = clean_label(label)
                candidates.append(
                    (
                        order[id(tc)],
                        FieldDef(
                            key="",
                            label=lab,
                            type=infer_text_type(lab),
                            required=is_required_label(label),
                            format_hint=find_format_hint(label),
                            source_ref=f"cell:{ti}:{ri}:{ci}",
                            meta={"element": "cell", "question_no": question_number(lab)},
                        ),
                    )
                )

    # 3 + 4. blank runs and checkbox glyphs in paragraphs
    for p in paras:
        if id(p) in covered_paras or _ancestor(p, "w:sdt") is not None:
            continue
        text = el_text(p)
        pi = pidx[id(p)]
        if GLYPH_RE.search(text):
            fd = _glyph_field(p, text, pi, paras, pidx)
            if fd:
                candidates.append((order[id(p)], fd))
                continue
        blanks = list(BLANK_RUN.finditer(text))
        prev_end = 0
        for k, m in enumerate(blanks):
            before = text[prev_end : m.start()]
            prev_end = m.end()
            label = clean_label(before)
            if not label:
                tc = _ancestor(p, "w:tc")
                label = clean_label(_left_cell_text(tc)) if tc is not None else ""
            if not label:
                label = clean_label(_prev_para_text(p, paras, pidx))
            if not label:
                continue
            candidates.append(
                (
                    order[id(p)] + k * 1e-3,
                    FieldDef(
                        key="",
                        label=label,
                        type=infer_text_type(label),
                        required=is_required_label(before),
                        format_hint=find_format_hint(before, text[m.end() : m.end() + 40]),
                        source_ref=f"blank:{pi}:{k}",
                        meta={"element": "blank", "question_no": question_number(label), "blank_len": len(m.group(0))},
                    ),
                )
            )

    candidates.sort(key=lambda c: c[0])
    fields = [f for _, f in candidates]

    # sections (nearest preceding heading) and notes sections
    headings: list[tuple[int, str]] = []
    ctx = InstructionContext()
    in_notes = False
    notes_lines: list[str] = []
    for p in paras:
        if _ancestor(p, "w:sdt") is not None:
            continue
        if _is_heading_para(p, style_names):
            t = el_text(p).strip()
            in_notes = bool(NOTES_HEADING.search(t))
            if not in_notes:
                headings.append((order[id(p)], clean_label(t)))
            continue
        if in_notes:
            notes_lines.append(el_text(p))
    ctx.notes = parse_notes_text(0, notes_lines)

    # inline notes: a paragraph right after a field's paragraph that starts with "Note"
    for (pos, f) in candidates:
        if f.meta.get("element") in ("blank", "glyph", "sdt"):
            ref_p = None
            if f.meta["element"] in ("blank", "glyph"):
                ref_p = paras[int(f.source_ref.split(":")[1])]
            else:
                sdt = leaf_sdts(body)[int(f.source_ref.split(":")[1])]
                ref_p = sdt if sdt.tag == qn("w:p") else _ancestor(sdt, "w:p")
            if ref_p is None:
                continue
            j = pidx.get(id(ref_p))
            if j is not None and j + 1 < len(paras):
                nxt = el_text(paras[j + 1]).strip()
                if re.match(r"^(note|hint|e\.g\.|format|\()", nxt, re.I):
                    f.instructions = nxt

    hs = sorted(headings)
    for pos, f in candidates:
        cur = None
        for hpos, htxt in hs:
            if hpos <= pos:
                cur = htxt
            else:
                break
        f.section = cur

    link_instructions(fields, ctx)
    used: set[str] = set()
    for i, f in enumerate(fields):
        f.order_index = i
        f.key = unique_key(slugify(f.label), used)
    return fields


def _glyph_field(p, text: str, pi: int, paras, pidx) -> FieldDef | None:
    parts = GLYPH_RE.split(text)
    before = parts[0]
    opts = [clean_label(x) for x in parts[1:]]
    opts = [o for o in opts if o]
    label = clean_label(before)
    if not label:
        tc = _ancestor(p, "w:tc")
        label = clean_label(_left_cell_text(tc)) if tc is not None else ""
    if len(opts) == 1 and not label:
        # "☐ I agree to the terms" -> a single checkbox labelled by its text
        return FieldDef(
            key="", label=opts[0], type="checkbox", source_ref=f"glyph:{pi}",
            meta={"element": "glyph", "glyph_options": opts, "question_no": question_number(opts[0])},
        )
    if not label:
        label = clean_label(_prev_para_text(p, paras, pidx))
    if not label or not opts:
        return None
    if len(opts) == 1:
        return FieldDef(
            key="", label=f"{label}: {opts[0]}", type="checkbox", source_ref=f"glyph:{pi}",
            meta={"element": "glyph", "glyph_options": opts, "question_no": question_number(label)},
        )
    return FieldDef(
        key="",
        label=label,
        type="choice",
        options=opts,
        required=is_required_label(before),
        source_ref=f"glyph:{pi}",
        meta={"element": "glyph", "glyph_options": opts, "question_no": question_number(label)},
    )


def docx_first_text(path: Path) -> str:
    d = docx.Document(str(path))
    return "\n".join(el_text(p) for p in all_paragraphs(d.element.body))[:4000]


def docx_field_refs(path: Path) -> list[str]:
    return sorted(f.source_ref or "" for f in parse_docx_form(path))
