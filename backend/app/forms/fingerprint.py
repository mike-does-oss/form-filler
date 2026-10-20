"""Form fingerprint: hash of page count, sorted field names and normalised first-page text."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf as fitz


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())[:2000]


def fingerprint(form_type: str, path: Path) -> str:
    if form_type == "docx":
        from app.forms.docx_form import docx_first_text, docx_field_refs

        pages = 1
        names = docx_field_refs(path)
        first = docx_first_text(path)
    else:
        doc = fitz.open(path)
        pages = len(doc)
        names = sorted({w.field_name or "" for page in doc for w in page.widgets()})
        first = doc[0].get_text("text") if pages else ""
    h = hashlib.sha256()
    h.update(f"{form_type}|{pages}|".encode())
    h.update("\x1f".join(names).encode())
    h.update(b"|")
    h.update(_normalise(first).encode())
    return h.hexdigest()[:32]
