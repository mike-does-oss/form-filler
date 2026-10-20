"""Flat PDF (no AcroForm fields): detect candidate input areas and pair them with labels.

Candidates come from three detectors, merged:
  - vector drawings: thin horizontal lines and empty rectangles (page.get_drawings)
  - text underscores: runs of "_____" in the text layer
  - OpenCV on the rendered page: horizontal lines and boxes (works for scanned forms)
The user can then fix the mapping in the UI (draw a box, assign label/type). The saved
mapping becomes a form template, so repeat forms need no manual mapping.
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf as fitz
import numpy as np

from app.db.models import FieldDef
from app.forms.acroform import nearest_label, page_lines
from app.forms.common import (
    Heading,
    InstructionContext,
    assign_sections,
    clean_label,
    find_format_hint,
    infer_text_type,
    is_heading,
    is_required_label,
    link_instructions,
    question_number,
    slugify,
    unique_key,
)

UNDERSCORES = re.compile(r"_{4,}")


def _iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def _vector_candidates(page: fitz.Page) -> list[tuple[list[float], str]]:
    out: list[tuple[list[float], str]] = []
    try:
        drawings = page.get_drawings()
    except Exception:  # noqa: BLE001
        return out
    for dr in drawings:
        for item in dr.get("items", []):
            kind = item[0]
            if kind == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 1.5 and abs(p2.x - p1.x) > 40:
                    x0, x1 = sorted((p1.x, p2.x))
                    out.append(([x0, p1.y - 14, x1, p1.y - 0.5], "line"))
            elif kind == "re":
                r = item[1]
                if 6 <= r.width <= 16 and 6 <= r.height <= 16:
                    out.append(([r.x0, r.y0, r.x1, r.y1], "box_small"))
                elif r.width > 40 and 10 <= r.height <= 200:
                    out.append(([r.x0 + 1, r.y0 + 1, r.x1 - 1, r.y1 - 1], "box"))
                elif r.width > 40 and r.height < 1.5:
                    out.append(([r.x0, r.y0 - 14, r.x1, r.y0 - 0.5], "line"))
    return out


def _text_underscore_candidates(page: fitz.Page) -> list[tuple[list[float], str, str]]:
    """Underscore runs, each with the text before it on the same line as its label hint."""
    out = []
    for b in page.get_text("rawdict", flags=fitz.TEXTFLAGS_TEXT)["blocks"]:
        for line in b.get("lines", []):
            chars = [c for span in line["spans"] for c in span.get("chars", [])]
            text = "".join(c["c"] for c in chars)
            prev = 0
            for m in UNDERSCORES.finditer(text):
                c0, c1 = chars[m.start()], chars[m.end() - 1]
                hint = text[prev : m.start()]
                prev = m.end()
                out.append(([c0["bbox"][0], c0["bbox"][1] - 2, c1["bbox"][2], c1["bbox"][3]], "underscore", hint))
    return out


def _opencv_candidates(page: fitz.Page, dpi: int = 100) -> list[tuple[list[float], str]]:
    import cv2

    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    bw = cv2.adaptiveThreshold(255 - img, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)
    scale = 72.0 / dpi
    out: list[tuple[list[float], str]] = []
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, pix.width // 25), 1))
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk)
    contours, _ = cv2.findContours(horiz, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if h <= 4:
            out.append(([x * scale, y * scale - 14, (x + w) * scale, y * scale - 0.5], "line"))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(8, pix.height // 80)))
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    grid = cv2.add(horiz, vert)
    contours, _ = cv2.findContours(grid, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        bx = [x * scale, y * scale, (x + w) * scale, (y + h) * scale]
        wpt, hpt = bx[2] - bx[0], bx[3] - bx[1]
        if 6 <= wpt <= 16 and 6 <= hpt <= 16:
            out.append((bx, "box_small"))
        elif wpt > 40 and 12 <= hpt <= 200:
            out.append(([bx[0] + 1, bx[1] + 1, bx[2] - 1, bx[3] - 1], "box"))
    return out


def detect_input_areas(page: fitz.Page) -> list[tuple[list[float], str, str | None]]:
    cands: list[tuple[list[float], str, str | None]] = [(b, k, None) for b, k in _vector_candidates(page)]
    cands += _text_underscore_candidates(page)
    if not cands:
        try:
            cands += [(b, k, None) for b, k in _opencv_candidates(page)]
        except Exception:  # noqa: BLE001
            pass
    words = [list(w[:4]) for w in page.get_text("words") if not UNDERSCORES.fullmatch(w[4])]
    kept: list[tuple[list[float], str, str | None]] = []
    for box, kind, hint in sorted(cands, key=lambda c: (c[0][1], c[0][0])):
        if box[2] - box[0] < 6:
            continue
        # skip areas that already contain text (table cells with content, page borders...)
        inner = [w for w in words if w[0] >= box[0] - 1 and w[2] <= box[2] + 1 and w[1] >= box[1] - 1 and w[3] <= box[3] + 1]
        if inner and kind != "underscore":
            continue
        if box[2] - box[0] > page.rect.width * 0.95 and box[3] - box[1] > page.rect.height * 0.5:
            continue  # page frame
        if any(_iou(box, k[0]) > 0.4 for k in kept):
            continue
        kept.append((box, kind, hint))
    return kept


def parse_flat_pdf(path: Path) -> list[FieldDef]:
    doc = fitz.open(path)
    fields: list[FieldDef] = []
    headings: list[Heading] = []
    used: set[str] = set()
    for page in doc:
        pno = page.number + 1
        lines = page_lines(page)
        sizes = sorted(ln.size for ln in lines)
        body = sizes[len(sizes) // 2] if sizes else 10
        for ln in lines:
            if is_heading(ln, body):
                headings.append(Heading(page=pno, y=ln.bbox[1], text=clean_label(ln.text)))
        areas = detect_input_areas(page)
        area_boxes = [a[0] for a in areas]
        # label lines are text lines minus pure-underscore lines
        label_lines = [ln for ln in lines if clean_label(ln.text)]
        for box, kind, hint in areas:
            is_check = kind == "box_small"
            raw = hint if hint and clean_label(hint) else None
            if raw is None:
                others = [b for b in area_boxes if b is not box]
                lab = nearest_label(box, label_lines, others, prefer_right=is_check)
                if lab is None:
                    continue
                raw = UNDERSCORES.split(lab.text)[0] or lab.text
            label = clean_label(raw)
            if not label:
                continue
            ftype = "checkbox" if is_check else infer_text_type(label, multiline=(box[3] - box[1]) > 30)
            fields.append(
                FieldDef(
                    key="",
                    label=label,
                    type=ftype,
                    required=is_required_label(raw),
                    format_hint=find_format_hint(raw),
                    page=pno,
                    bbox=[round(v, 2) for v in box],
                    source_ref=f"bbox:{pno}:{','.join(str(round(v, 1)) for v in box)}",
                    meta={"element": "overlay", "detector": kind, "question_no": question_number(label)},
                )
            )
    assign_sections(fields, headings)
    link_instructions(fields, InstructionContext())
    fields.sort(key=lambda f: (f.page or 0, round((f.bbox or [0, 0])[1] / 6), (f.bbox or [0])[0]))
    for i, f in enumerate(fields):
        f.order_index = i
        f.key = unique_key(slugify(f.label), used)
    return fields
