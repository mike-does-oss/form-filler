"""Overlay write-back for flat PDFs: insert_textbox at the mapped box, shrinking the font to fit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pymupdf as fitz

from app.db.models import FieldDef
from app.verify.normalize import format_for_form

MAX_FONT = 10.0
MIN_FONT = 6.0


def fit_text(page: fitz.Page, rect: fitz.Rect, text: str, multiline: bool) -> tuple[bool, float]:
    """Insert text into rect at the largest size that fits (>= MIN_FONT). Returns (fits, size)."""
    size = MAX_FONT if multiline else min(MAX_FONT, max(MIN_FONT, rect.height * 0.75))
    # dry run on a scratch page to measure overflow
    while size >= MIN_FONT:
        scratch = fitz.open()
        sp = scratch.new_page(width=page.rect.width, height=page.rect.height)
        rc = sp.insert_textbox(rect, text, fontsize=size, fontname="helv", align=fitz.TEXT_ALIGN_LEFT)
        if rc >= 0:
            page.insert_textbox(rect, text, fontsize=size, fontname="helv", color=(0, 0, 0.55))
            return True, size
        size -= 0.5
    # overflow: write at minimum size anyway and flag it
    page.insert_textbox(rect, text, fontsize=MIN_FONT, fontname="helv", color=(0, 0, 0.55))
    return False, MIN_FONT


def fill_flat_pdf(src: Path, dest: Path, fields: list[FieldDef], values: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    doc = fitz.open(src)
    for f in fields:
        if f.key not in values or not f.bbox or not f.page or f.type == "signature":
            continue
        v = values[f.key]
        if v is None or v == "":
            continue
        page = doc[f.page - 1]
        rect = fitz.Rect(*f.bbox)
        if f.type == "checkbox":
            if v in (True, "true", "True", "yes", "Yes"):
                s = min(rect.width, rect.height)
                c = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
                page.insert_text(fitz.Point(c.x - s * 0.32, c.y + s * 0.35), "X", fontsize=s * 0.95, fontname="helv")
            continue
        text = format_for_form(f, v)
        # give single-line areas a little vertical slack so text sits on the line
        if rect.height < 12:
            rect = fitz.Rect(rect.x0, rect.y1 - 13, rect.x1, rect.y1 + 1)
        fits, _ = fit_text(page, rect + (1, 0, 0, 0), text, multiline=f.type == "multiline" or rect.height > 30)
        if not fits:
            warnings.append(f"{f.key}: text overflows its box even at {MIN_FONT}pt")
    doc.save(dest, garbage=3, deflate=True)
    return warnings
