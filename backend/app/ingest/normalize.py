"""Convert any supported source document into a PDF so that every source has pages,
page images and a single bbox coordinate system (PDF points, top-left origin).

- PDF: used as-is.
- Images (png/jpg/tiff/bmp/webp, multi-page tiff): wrapped into an image-only PDF (OCR needed).
- DOCX: rendered to PDF via PyMuPDF Story (HTML layout engine), tables preserved.
- TXT/MD: rendered to PDF the same way.
"""

from __future__ import annotations

import html
import mimetypes
from pathlib import Path

import pymupdf as fitz

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif"}
DOCX_EXT = {".docx"}
TEXT_EXT = {".txt", ".md"}
PDF_EXT = {".pdf"}

SUPPORTED_EXT = IMAGE_EXT | DOCX_EXT | TEXT_EXT | PDF_EXT


def guess_mime(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if ext == ".md":
        return "text/markdown"
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def normalize_to_pdf(src: Path, dest: Path) -> Path:
    ext = src.suffix.lower()
    if ext in PDF_EXT:
        if src != dest:
            dest.write_bytes(src.read_bytes())
        return dest
    if ext in IMAGE_EXT:
        return _image_to_pdf(src, dest)
    if ext in DOCX_EXT:
        return _html_to_pdf(docx_to_html(src), dest)
    if ext in TEXT_EXT:
        text = src.read_text(errors="replace")
        body = "".join(f"<p>{html.escape(line) or '&nbsp;'}</p>" for line in text.splitlines())
        return _html_to_pdf(body, dest)
    raise ValueError(f"Unsupported source document type: {ext}")


def _image_to_pdf(src: Path, dest: Path) -> Path:
    import io

    from PIL import Image, ImageSequence

    out = fitz.open()
    with Image.open(src) as im:
        for frame in ImageSequence.Iterator(im):
            fr = frame.convert("RGB") if frame.mode not in ("RGB", "L") else frame.copy()
            dpi = frame.info.get("dpi", (150, 150))[0] or 150
            dpi = dpi if 50 <= dpi <= 1200 else 150
            buf = io.BytesIO()
            fr.save(buf, format="PNG")
            w, h = fr.size
            p = out.new_page(width=w * 72 / dpi, height=h * 72 / dpi)
            p.insert_image(p.rect, stream=buf.getvalue())
    out.save(dest)
    return dest


_CSS = """
body { font-family: sans-serif; font-size: 10pt; }
h1 { font-size: 16pt; } h2 { font-size: 13pt; } h3 { font-size: 11pt; }
table { border-collapse: collapse; }
td, th { border: 1px solid #555; padding: 2px 4px; vertical-align: top; }
p { margin: 0 0 4pt 0; }
"""


def _html_to_pdf(body_html: str, dest: Path) -> Path:
    story = fitz.Story(html=f"<body>{body_html}</body>", user_css=_CSS)
    writer = fitz.DocumentWriter(str(dest))
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (54, 54, -54, -54)
    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    return dest


def docx_to_html(path: Path) -> str:
    """Body-order traversal of paragraphs and tables into simple HTML."""
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(str(path))
    parts: list[str] = []
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, d)
            text = _para_text(para)
            style = (para.style.name if para.style is not None else "") or ""
            if style.startswith("Heading"):
                level = "".join(c for c in style if c.isdigit()) or "2"
                level = str(min(int(level), 3))
                parts.append(f"<h{level}>{html.escape(text)}</h{level}>")
            elif style == "Title":
                parts.append(f"<h1>{html.escape(text)}</h1>")
            else:
                parts.append(f"<p>{html.escape(text) or '&nbsp;'}</p>")
        elif tag == "tbl":
            table = Table(child, d)
            rows = []
            for row in table.rows:
                cells = []
                seen = set()
                for cell in row.cells:
                    if id(cell._tc) in seen:
                        continue
                    seen.add(id(cell._tc))
                    cells.append(f"<td>{html.escape(cell.text)}</td>")
                rows.append("<tr>" + "".join(cells) + "</tr>")
            parts.append("<table>" + "".join(rows) + "</table><p>&nbsp;</p>")
        elif tag == "sdt":
            text = "".join(t.text or "" for t in child.iter() if t.tag.endswith("}t"))
            parts.append(f"<p>{html.escape(text)}</p>")
    return "\n".join(parts)


def _para_text(para) -> str:
    # include text inside content controls / hyperlinks too
    return "".join(t.text or "" for t in para._p.iter() if t.tag.endswith("}t"))
