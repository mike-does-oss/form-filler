"""Ingest one source document: normalise -> text layer / OCR -> chunks -> embeddings."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import pymupdf as fitz

from app import db
from app.config import get_config
from app.ingest.chunking import chunk_page
from app.ingest.embeddings import embed_chunks
from app.ingest.layout import Block, PageLayout, extract_text_layer, has_text_layer
from app.ingest.normalize import normalize_to_pdf
from app.ingest.ocr import ocr_pages, render_page_png

log = logging.getLogger(__name__)

VISION_PROMPT = (
    "Transcribe all text on this page exactly as written, in reading order. "
    "Render tables as Markdown tables. Do not add commentary."
)


def normalized_pdf_path(doc_row: dict) -> Path:
    return Path(doc_row["stored_path"]).with_suffix(".normalized.pdf")


def ingest_document(document_id: int, progress: Callable[[float, str], None] | None = None) -> dict:
    cfg = get_config()
    doc_row = db.q1("SELECT * FROM documents WHERE id = ?", (document_id,))
    if doc_row is None:
        raise ValueError(f"document {document_id} not found")
    src = Path(doc_row["stored_path"])
    pdf_path = normalized_pdf_path(doc_row)
    normalize_to_pdf(src, pdf_path)
    pdf = fitz.open(pdf_path)

    layouts: dict[int, PageLayout] = {}
    need_ocr: list[int] = []
    for page in pdf:
        pno = page.number + 1
        if has_text_layer(page, cfg.ocr.text_layer_min_chars):
            layouts[pno] = extract_text_layer(page)
        else:
            need_ocr.append(pno)
    if progress:
        progress(0.2, f"text layer done, OCR on {len(need_ocr)} page(s)")
    if need_ocr:
        layouts.update(ocr_pages(pdf_path, need_ocr))
    if progress:
        progress(0.6, "OCR done")

    # Optional: send low-confidence OCR pages to the vision model.
    if cfg.vision.enabled:
        from app.extraction.llm_adapter import get_llm

        for pno in need_ocr:
            lay = layouts[pno]
            if lay.ocr_confidence is not None and lay.ocr_confidence >= cfg.vision.min_ocr_confidence:
                continue
            try:
                text = get_llm().vision_transcribe(render_page_png(pdf[pno - 1], 150), VISION_PROMPT)
            except Exception as e:  # noqa: BLE001
                log.warning("vision transcription failed on page %s: %s", pno, e)
                continue
            box = [0.0, 0.0, lay.width, lay.height]
            lay.blocks = [Block(text=p.strip(), bbox=box) for p in text.split("\n\n") if p.strip()]
            lay.engine = "vision"
            # keep OCR word boxes for highlighting when the words agree

    # Persist page spans + chunks
    max_tokens = cfg.retrieval.chunk_tokens
    all_chunks = []
    with db.transaction() as conn:
        conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        conn.execute("DELETE FROM page_spans WHERE document_id = ?", (document_id,))
        for pno in sorted(layouts):
            lay = layouts[pno]
            conn.execute(
                "INSERT INTO page_spans (document_id, page, width, height, spans_json, ocr_confidence) VALUES (?,?,?,?,?,?)",
                (document_id, pno, lay.width, lay.height, db.dumps(lay.words), lay.ocr_confidence),
            )
            chunks = chunk_page(lay, document_id, max_tokens, start_index=len(all_chunks))
            for ch in chunks:
                ch.meta["engine"] = lay.engine
                ch.id = int(
                    conn.execute(
                        "INSERT INTO chunks (document_id, page, bbox_json, text, chunk_index, meta_json) VALUES (?,?,?,?,?,?)",
                        (document_id, ch.page, db.dumps(ch.bbox), ch.text, ch.chunk_index, db.dumps(ch.meta)),
                    ).lastrowid
                )
            all_chunks.extend(chunks)
        conn.execute(
            "UPDATE documents SET page_count = ?, ocr_used = ?, status = 'chunked', meta_json = ? WHERE id = ?",
            (
                len(pdf),
                1 if need_ocr else 0,
                db.dumps(
                    {
                        "ocr_pages": need_ocr,
                        "engines": sorted({lay.engine for lay in layouts.values()}),
                        "ocr_confidence": {p: layouts[p].ocr_confidence for p in need_ocr},
                    }
                ),
                document_id,
            ),
        )
    if progress:
        progress(0.75, f"{len(all_chunks)} chunks, embedding")
    embed_chunks([c.id for c in all_chunks], [c.text for c in all_chunks])
    db.ex("UPDATE documents SET status = 'ingested' WHERE id = ?", (document_id,))
    if progress:
        progress(1.0, "ingested")
    return {"pages": len(pdf), "ocr_pages": need_ocr, "chunks": len(all_chunks)}


def render_document_page(document_id: int, page: int, dpi: int = 110) -> bytes:
    doc_row = db.q1("SELECT * FROM documents WHERE id = ?", (document_id,))
    if doc_row is None:
        raise FileNotFoundError(document_id)
    pdf_path = normalized_pdf_path(doc_row)
    if not pdf_path.exists():
        normalize_to_pdf(Path(doc_row["stored_path"]), pdf_path)
    pdf = fitz.open(pdf_path)
    if page < 1 or page > len(pdf):
        raise FileNotFoundError(page)
    return render_page_png(pdf[page - 1], dpi)
