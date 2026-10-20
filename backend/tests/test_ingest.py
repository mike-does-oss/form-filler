"""P1: a scanned PDF and a native PDF both produce chunks with correct page and bbox; page images render."""

from __future__ import annotations

import shutil

import pymupdf as fitz
import pytest

from app import db
from app.ingest.normalize import guess_mime
from app.ingest.pipeline import ingest_document, render_document_page
from app.jobs.service import job_dir
from app.verify.evidence import locate_quote

from .conftest import FIXTURES


def _doc(path) -> int:
    fid = db.insert("forms", {"filename": "f", "stored_path": "f", "form_type": "acroform", "fingerprint": "x"})
    jid = db.insert("jobs", {"form_id": fid})
    did = db.insert("documents", {"job_id": jid, "filename": path.name, "stored_path": "", "mime": guess_mime(path.name)})
    dest = job_dir(jid) / "docs" / f"{did}{path.suffix}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, dest)
    db.ex("UPDATE documents SET stored_path = ? WHERE id = ?", (str(dest), did))
    return did


def test_native_pdf(app_env):
    did = _doc(FIXTURES / "01_acroform_basic" / "sources" / "bank_statement.pdf")
    info = ingest_document(did)
    assert info["ocr_pages"] == []
    chunks = db.q("SELECT * FROM chunks WHERE document_id = ?", (did,))
    assert chunks and all(c["page"] == 1 for c in chunks)
    table = next(c for c in chunks if "Total interest earned" in c["text"])
    assert "| Total interest earned |  |  | $1,284.37 |" in table["text"]  # tables kept as Markdown
    for c in chunks:
        x0, y0, x1, y1 = db.loads(c["bbox_json"])
        assert 0 <= x0 < x1 <= 600 and 0 <= y0 < y1 <= 850
        assert c["embedding"] is not None
    boxes = locate_quote(did, 1, "Account holder: Priya Natarajan")
    assert boxes and boxes[0][1] < 200  # near the top of the page
    png = render_document_page(did, 1)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_scanned_pdf_and_image(app_env):
    did = _doc(FIXTURES / "03_scanned_source" / "sources" / "receipt_scan.pdf")
    info = ingest_document(did)
    assert info["ocr_pages"] == [1]
    text = " ".join(c["text"] for c in db.q("SELECT text FROM chunks WHERE document_id = ?", (did,)))
    assert "XKD-482" in text and "18,750" in text
    boxes = locate_quote(did, 1, "Registration XKD-482")
    assert boxes
    page = db.q1("SELECT width, height FROM page_spans WHERE document_id = ?", (did,))
    for b in boxes:
        assert 0 <= b[0] < b[2] <= page["width"] and 0 <= b[1] < b[3] <= page["height"]

    did2 = _doc(FIXTURES / "03_scanned_source" / "sources" / "licence_scan.png")
    info2 = ingest_document(did2)
    assert info2["ocr_pages"] == [1]
    text2 = " ".join(c["text"] for c in db.q("SELECT text FROM chunks WHERE document_id = ?", (did2,)))
    assert "2847 1956" in text2
    assert render_document_page(did2, 1)[:4] == b"\x89PNG"


def test_docx_source_rendered(app_env):
    did = _doc(FIXTURES / "02_docx_form" / "sources" / "resume.docx")
    ingest_document(did)
    text = " ".join(c["text"] for c in db.q("SELECT text FROM chunks WHERE document_id = ?", (did,)))
    assert "amara.okafor@example.org" in text and "Chidi Okafor (brother)" in text
    d = db.q1("SELECT stored_path FROM documents WHERE id = ?", (did,))
    assert fitz.open(d["stored_path"].replace(".docx", ".normalized.pdf")).page_count >= 1
