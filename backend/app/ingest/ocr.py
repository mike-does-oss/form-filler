"""OCR engines. All return PageLayouts in PDF points (top-left origin).

Docling is the main path (layout + tables + OCR), PaddleOCR an alternative, Tesseract
the minimal CPU fallback. If the configured engine is not installed we fall back to
Tesseract and say so in /api/health.
"""

from __future__ import annotations

import io
import logging
import shutil
from functools import lru_cache
from pathlib import Path

import pymupdf as fitz

from app.config import get_config
from app.ingest.layout import Block, PageLayout, table_to_markdown

log = logging.getLogger(__name__)


def render_page_png(page: fitz.Page, dpi: int) -> bytes:
    pix = page.get_pixmap(dpi=dpi)
    return pix.tobytes("png")


# ---------------------------------------------------------------- Tesseract
def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


_LANG_MAP = {"en": "eng", "de": "deu", "fr": "fra", "es": "spa", "it": "ita", "nl": "nld", "pt": "por"}


def ocr_page_tesseract(page: fitz.Page, dpi: int, language: str) -> PageLayout:
    return ocr_png_tesseract(render_page_png(page, dpi), page.number + 1, page.rect.width, page.rect.height, dpi, language)


def ocr_png_tesseract(png: bytes, pno: int, width: float, height: float, dpi: int, language: str) -> PageLayout:
    import pytesseract
    from PIL import Image

    img = Image.open(io.BytesIO(png))
    lang = _LANG_MAP.get(language, language)
    data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT, config="--psm 3")
    scale = 72.0 / dpi
    layout = PageLayout(page=pno, width=width, height=height, ocr_used=True, engine="tesseract")
    groups: dict[tuple[int, int], dict[tuple[int], list]] = {}
    confs: list[float] = []
    n = len(data["text"])
    for i in range(n):
        word = (data["text"][i] or "").strip()
        conf = float(data["conf"][i])
        if not word or conf < 0:
            continue
        confs.append(conf)
        box = [
            data["left"][i] * scale,
            data["top"][i] * scale,
            (data["left"][i] + data["width"][i]) * scale,
            (data["top"][i] + data["height"][i]) * scale,
        ]
        layout.words.append((word, box))
        gkey = (data["block_num"][i], data["par_num"][i])
        groups.setdefault(gkey, {}).setdefault((data["line_num"][i],), []).append((word, box))
    for _, lines in sorted(groups.items()):
        line_texts = []
        boxes = []
        for _, words in sorted(lines.items()):
            line_texts.append(" ".join(w for w, _ in words))
            boxes.extend(b for _, b in words)
        bbox = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        layout.blocks.append(Block(text="\n".join(line_texts), bbox=bbox))
    layout.ocr_confidence = sum(confs) / len(confs) if confs else 0.0
    return layout


# ---------------------------------------------------------------- PaddleOCR
@lru_cache(maxsize=1)
def _paddle(language: str):
    from paddleocr import PaddleOCR  # type: ignore

    return PaddleOCR(use_angle_cls=True, lang=language)


def paddle_available() -> bool:
    try:
        import paddleocr  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def ocr_page_paddle(page: fitz.Page, dpi: int, language: str) -> PageLayout:
    import numpy as np
    from PIL import Image

    png = render_page_png(page, dpi)
    img = np.array(Image.open(io.BytesIO(png)).convert("RGB"))
    result = _paddle(language).ocr(img)
    scale = 72.0 / dpi
    layout = PageLayout(
        page=page.number + 1, width=page.rect.width, height=page.rect.height, ocr_used=True, engine="paddleocr"
    )
    confs: list[float] = []
    lines = result[0] if result and isinstance(result, list) else []
    for item in lines or []:
        try:
            pts, (text, conf) = item[0], item[1]
        except (TypeError, ValueError, IndexError):
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        box = [min(xs) * scale, min(ys) * scale, max(xs) * scale, max(ys) * scale]
        confs.append(float(conf) * 100)
        layout.blocks.append(Block(text=text, bbox=box))
        # approximate word boxes by splitting the line box evenly
        words = text.split()
        if words:
            w = (box[2] - box[0]) / max(1, len(text))
            x = box[0]
            for word in words:
                wb = [x, box[1], x + w * len(word), box[3]]
                layout.words.append((word, wb))
                x += w * (len(word) + 1)
    layout.ocr_confidence = sum(confs) / len(confs) if confs else 0.0
    return layout


# ---------------------------------------------------------------- Docling
def docling_available() -> bool:
    try:
        import docling  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


@lru_cache(maxsize=1)
def _docling_converter(language: str):
    from docling.datamodel.base_models import InputFormat  # type: ignore
    from docling.datamodel.pipeline_options import PdfPipelineOptions  # type: ignore
    from docling.document_converter import DocumentConverter, PdfFormatOption  # type: ignore

    opts = PdfPipelineOptions()
    opts.do_ocr = True
    opts.do_table_structure = True
    try:
        opts.ocr_options.force_full_page_ocr = True
        opts.ocr_options.lang = [language]
    except Exception:  # noqa: BLE001
        pass
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


def ocr_document_docling(pdf_path: Path, pages: list[int], language: str) -> dict[int, PageLayout]:
    """Run Docling over a PDF and return layouts for the requested 1-based pages."""
    conv = _docling_converter(language)
    src = fitz.open(pdf_path)
    # Extract only the pages that need OCR into a temp PDF to save time.
    sub = fitz.open()
    for p in pages:
        sub.insert_pdf(src, from_page=p - 1, to_page=p - 1)
    tmp = pdf_path.with_suffix(".ocr_subset.pdf")
    sub.save(tmp)
    try:
        result = conv.convert(str(tmp))
    finally:
        pass
    doc = result.document
    out: dict[int, PageLayout] = {}
    for i, p in enumerate(pages, start=1):
        pg = src[p - 1]
        out[p] = PageLayout(
            page=p, width=pg.rect.width, height=pg.rect.height, ocr_used=True, engine="docling", ocr_confidence=None
        )
    from docling_core.types.doc import TableItem  # type: ignore

    for item, _level in doc.iterate_items():
        prov = getattr(item, "prov", None)
        if not prov:
            continue
        pv = prov[0]
        sub_page = pv.page_no
        if sub_page < 1 or sub_page > len(pages):
            continue
        real_page = pages[sub_page - 1]
        layout = out[real_page]
        bb = pv.bbox
        try:
            bb = bb.to_top_left_origin(page_height=layout.height)
        except Exception:  # noqa: BLE001
            pass
        box = [float(bb.l), float(bb.t), float(bb.r), float(bb.b)]
        if isinstance(item, TableItem):
            try:
                md = item.export_to_markdown(doc=doc)
            except TypeError:
                md = item.export_to_markdown()
            if not md:
                try:
                    df = item.export_to_dataframe()
                    md = table_to_markdown([list(map(str, df.columns))] + df.astype(str).values.tolist())
                except Exception:  # noqa: BLE001
                    md = ""
            layout.blocks.append(Block(text=md, bbox=box, kind="table"))
            layout.words.append((md.replace("|", " "), box))
        else:
            text = getattr(item, "text", "") or ""
            if not text.strip():
                continue
            label = str(getattr(item, "label", ""))
            kind = "heading" if "header" in label or "title" in label else "text"
            layout.blocks.append(Block(text=text, bbox=box, kind=kind))
            # Docling gives block-level boxes; split into per-word pseudo boxes by line
            words = text.split()
            w = (box[2] - box[0]) / max(1, len(text))
            x = box[0]
            for word in words:
                layout.words.append((word, [x, box[1], min(box[2], x + w * len(word)), box[3]]))
                x += w * (len(word) + 1)
    tmp.unlink(missing_ok=True)
    return out


# ---------------------------------------------------------------- dispatch
def resolve_engine() -> str:
    """The OCR engine actually in use (configured engine if installed, else tesseract)."""
    engine = get_config().ocr.engine
    if engine == "docling" and docling_available():
        return "docling"
    if engine == "paddleocr" and paddle_available():
        return "paddleocr"
    return "tesseract"


def ocr_status() -> dict:
    cfg = get_config()
    return {
        "configured_engine": cfg.ocr.engine,
        "active_engine": resolve_engine(),
        "docling": docling_available(),
        "paddleocr": paddle_available(),
        "tesseract": tesseract_available(),
    }


def ocr_pages(pdf_path: Path, page_numbers: list[int]) -> dict[int, PageLayout]:
    cfg = get_config()
    if not page_numbers:
        return {}
    engine = resolve_engine()
    if engine == "docling":
        try:
            return ocr_document_docling(pdf_path, page_numbers, cfg.ocr.language)
        except Exception as e:  # noqa: BLE001
            log.exception("Docling OCR failed, falling back to tesseract: %s", e)
            engine = "tesseract"
    doc = fitz.open(pdf_path)
    out: dict[int, PageLayout] = {}
    from concurrent.futures import ThreadPoolExecutor

    def run(p: int) -> PageLayout:
        page = doc[p - 1]
        if engine == "paddleocr":
            return ocr_page_paddle(page, cfg.ocr.dpi, cfg.ocr.language)
        return ocr_page_tesseract(page, cfg.ocr.dpi, cfg.ocr.language)

    if engine == "tesseract":
        # pytesseract shells out, so threads give real parallelism; render pages first
        # (PyMuPDF documents are not thread safe).
        rendered = {p: _render_for_thread(doc[p - 1], cfg.ocr.dpi) for p in page_numbers}
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {p: ex.submit(_ocr_rendered, p, rendered[p], cfg.ocr.dpi, cfg.ocr.language) for p in page_numbers}
            for p, f in futs.items():
                out[p] = f.result()
    else:
        for p in page_numbers:
            out[p] = run(p)
    return out


def _render_for_thread(page: fitz.Page, dpi: int) -> tuple[bytes, float, float]:
    return render_page_png(page, dpi), page.rect.width, page.rect.height


def _ocr_rendered(pno: int, rendered: tuple[bytes, float, float], dpi: int, language: str) -> PageLayout:
    png, width, height = rendered
    return ocr_png_tesseract(png, pno, width, height, dpi, language)
