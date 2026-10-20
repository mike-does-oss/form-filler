"""Layout-block chunking: merge blocks in reading order up to ~N tokens, never across pages."""

from __future__ import annotations

from app.db.models import Chunk
from app.ingest.layout import PageLayout, union_bbox


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def chunk_page(layout: PageLayout, document_id: int, max_tokens: int, start_index: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf_text: list[str] = []
    buf_boxes: list[list[float]] = []
    buf_tokens = 0
    heading: str | None = None

    def flush() -> None:
        nonlocal buf_text, buf_boxes, buf_tokens
        if buf_text:
            chunks.append(
                Chunk(
                    document_id=document_id,
                    page=layout.page,
                    bbox=union_bbox(buf_boxes),
                    text="\n\n".join(buf_text),
                    chunk_index=start_index + len(chunks),
                    meta={"heading": heading, "ocr": layout.ocr_used},
                )
            )
        buf_text, buf_boxes, buf_tokens = [], [], 0

    for block in layout.blocks:
        t = approx_tokens(block.text)
        if block.kind == "heading":
            # a heading starts a new chunk so it stays with the content that follows
            flush()
            heading = block.text.strip()
        if buf_tokens + t > max_tokens and buf_text:
            flush()
        if t > max_tokens:
            # very long block (e.g. a big table): split by lines, repeating a table header
            flush()
            lines = block.text.split("\n")
            header = lines[:2] if block.kind == "table" and len(lines) > 2 else []
            body = lines[len(header):]
            part: list[str] = []
            for line in body:
                if part and approx_tokens("\n".join(header + part + [line])) > max_tokens:
                    buf_text, buf_boxes = ["\n".join(header + part)], [block.bbox]
                    flush()
                    part = []
                part.append(line)
            if part:
                buf_text, buf_boxes = ["\n".join(header + part)], [block.bbox]
                buf_tokens = approx_tokens(buf_text[0])
            continue
        buf_text.append(block.text)
        buf_boxes.append(block.bbox)
        buf_tokens += t
    flush()
    return chunks
