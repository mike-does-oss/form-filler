"""Page layout model + native text-layer extraction with PyMuPDF."""

from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf as fitz


@dataclass
class Block:
    text: str
    bbox: list[float]
    kind: str = "text"  # text | table | heading


@dataclass
class PageLayout:
    page: int  # 1-based
    width: float
    height: float
    blocks: list[Block] = field(default_factory=list)
    words: list[tuple[str, list[float]]] = field(default_factory=list)
    ocr_used: bool = False
    ocr_confidence: float | None = None
    engine: str = "text_layer"


def union_bbox(boxes: list[list[float]]) -> list[float] | None:
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _overlap_frac(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return ix * iy / area


def has_text_layer(page: fitz.Page, min_chars: int) -> bool:
    return len(page.get_text("text").strip()) >= min_chars


def table_to_markdown(rows: list[list[str | None]]) -> str:
    rows = [[(c or "").replace("\n", " ").replace("|", "/").strip() for c in r] for r in rows if r]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def _table_rows(page: fitz.Page, table) -> list[list[str | None]]:
    """Cell text via clipped text extraction. Table.extract() can drop spaces and
    punctuation for some generators, so read each cell box from the page instead."""
    rows: list[list[str | None]] = []
    try:
        for row in table.rows:
            cells = []
            for cell in row.cells:
                if cell is None:
                    cells.append(None)
                    continue
                txt = page.get_textbox(fitz.Rect(cell))
                cells.append(" ".join(txt.split()))
            rows.append(cells)
        if rows and any(c for r in rows for c in r):
            return rows
    except Exception:  # noqa: BLE001
        pass
    return table.extract()


def extract_text_layer(page: fitz.Page) -> PageLayout:
    layout = PageLayout(page=page.number + 1, width=page.rect.width, height=page.rect.height)
    table_boxes: list[list[float]] = []
    try:
        tabs = page.find_tables()
        for t in tabs.tables:
            rows = _table_rows(page, t)
            if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
                continue
            md = table_to_markdown(rows)
            if md:
                bbox = list(t.bbox)
                table_boxes.append(bbox)
                layout.blocks.append(Block(text=md, bbox=bbox, kind="table"))
    except Exception:  # noqa: BLE001 - table detection is best effort
        pass

    d = page.get_text("dict", flags=fitz.TEXTFLAGS_TEXT)
    sizes = [s["size"] for b in d["blocks"] for line in b.get("lines", []) for s in line["spans"] if s["text"].strip()]
    body_size = sorted(sizes)[len(sizes) // 2] if sizes else 10
    for b in d["blocks"]:
        if b.get("type", 0) != 0:
            continue
        bbox = list(b["bbox"])
        if any(_overlap_frac(bbox, tb) > 0.5 for tb in table_boxes):
            continue
        lines = []
        max_size = 0.0
        bold = True
        for line in b.get("lines", []):
            txt = "".join(s["text"] for s in line["spans"])
            if txt.strip():
                lines.append(txt.strip())
                for s in line["spans"]:
                    if s["text"].strip():
                        max_size = max(max_size, s["size"])
                        bold = bold and bool(s["flags"] & 16)
        text = "\n".join(lines).strip()
        if not text:
            continue
        kind = "heading" if (max_size >= body_size * 1.2 or bold) and len(text) < 120 else "text"
        layout.blocks.append(Block(text=text, bbox=bbox, kind=kind))

    layout.blocks.sort(key=lambda bl: (round(bl.bbox[1] / 4), bl.bbox[0]))
    layout.words = [(w[4], [w[0], w[1], w[2], w[3]]) for w in page.get_text("words")]
    return layout
