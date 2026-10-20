"""Evidence verification: quotes must exist in the cited chunk, values must be in the quote.
Verified quotes are mapped back to page boxes for highlighting."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from rapidfuzz import fuzz

from app import db
from app.db.models import Evidence, FieldDef
from app.verify.normalize import dayfirst_for, parse_date, parse_decimal

_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-").replace("|", " ")
    return _WS.sub(" ", t).strip().lower()


def quote_score(quote: str, chunk_text: str) -> float:
    q, c = norm(quote), norm(chunk_text)
    if not q:
        return 0.0
    if q in c:
        return 100.0
    if len(q) > len(c):
        return float(fuzz.ratio(q, c))
    return float(fuzz.partial_ratio(q, c))


def value_in_quote(field: FieldDef, value: Any, quote: str, threshold: int) -> bool:
    if value is None or not quote:
        return False
    q = norm(quote)
    t = field.type
    if t == "date":
        target = str(value)
        df = dayfirst_for(field)
        # try every date-ish substring in the quote
        pats = re.findall(
            r"\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}|\d{1,2}(?:st|nd|rd|th)?\s+[a-z]{3,9}\.?,?\s+\d{2,4}|[a-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}",
            q,
        )
        for p in pats:
            d = parse_date(p, df)
            if d and d.isoformat() == target:
                return True
        return False
    if t in ("number", "currency"):
        target = parse_decimal(value)
        if target is None:
            return False
        for m in re.finditer(r"\(?-?\$?\s?\d[\d,]*(?:\.\d+)?\s*(?:k|m|million|thousand)?\)?", q):
            v = parse_decimal(m.group(0))
            if v is not None and abs(v - target) < Decimal("0.005"):
                return True
        return False
    if t == "checkbox":
        return True  # a boolean can't be string-matched; the verified quote is the support
    if t == "phone":
        digits = re.sub(r"\D", "", str(value))
        return len(digits) >= 6 and digits in re.sub(r"\D", "", q)
    v = norm(str(value))
    if not v:
        return False
    if v in q:
        return True
    if t == "choice":
        return fuzz.partial_ratio(v, q) >= 80
    if len(v) <= 3:
        return bool(re.search(rf"\b{re.escape(v)}\b", q))
    return fuzz.partial_ratio(v, q) >= threshold or fuzz.token_set_ratio(v, q) >= 95


def verify_evidence(
    field: FieldDef, value: Any, evidence: list[Evidence], chunks: dict[int, dict], threshold: int
) -> list[Evidence]:
    out = []
    for ev in evidence:
        ch = chunks.get(ev.chunk_id) if ev.chunk_id is not None else None
        if ch is None and ev.quote:
            # model cited a wrong chunk id: find the best chunk for the quote
            best = max(chunks.values(), key=lambda c: quote_score(ev.quote, c["text"]), default=None)
            if best is not None and quote_score(ev.quote, best["text"]) >= threshold:
                ch = best
                ev.chunk_id = best["id"]
        if ch is None:
            ev.verified = False
            out.append(ev)
            continue
        ev.document_id = ch["document_id"]
        ev.page = ch["page"]
        ev.score = quote_score(ev.quote, ch["text"])
        if ev.score < threshold:
            # the quote may live in a different chunk than the one cited
            for other in chunks.values():
                if other["id"] == ch["id"]:
                    continue
                s = quote_score(ev.quote, other["text"])
                if s >= threshold and s > ev.score:
                    ch, ev.score, ev.chunk_id = other, s, other["id"]
                    ev.document_id, ev.page = other["document_id"], other["page"]
        ev.verified = ev.score >= threshold
        ev.value_in_quote = value_in_quote(field, value, ev.quote, threshold) if value is not None else False
        boxes = locate_quote(ev.document_id, ev.page, ev.quote) if ev.verified else []
        ev.boxes = boxes
        ev.bbox = _union(boxes) or db.loads(ch.get("bbox_json"))
        out.append(ev)
    return out


def _union(boxes: list[list[float]]) -> list[float] | None:
    if not boxes:
        return None
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def locate_quote(document_id: int | None, page: int | None, quote: str) -> list[list[float]]:
    """Find the quote among the page's word spans; return one box per text line."""
    if document_id is None or page is None or not quote.strip():
        return []
    row = db.q1("SELECT spans_json FROM page_spans WHERE document_id = ? AND page = ?", (document_id, page))
    if not row:
        return []
    words: list[tuple[str, list[float]]] = [tuple(w) for w in db.loads(row["spans_json"], [])]  # type: ignore[misc]
    if not words:
        return []
    # concatenated normalised page text with char -> word index map
    parts: list[str] = []
    owner: list[int] = []
    for i, (w, _) in enumerate(words):
        nw = norm(w)
        if not nw:
            continue
        if parts:
            parts.append(" ")
            owner.append(-1)
        parts.append(nw)
        owner.extend([i] * len(nw))
    page_text = "".join(parts)
    q = norm(quote)
    start = page_text.find(q)
    if start >= 0:
        end = start + len(q)
    else:
        al = fuzz.partial_ratio_alignment(q, page_text)
        if al is None or al.score < 80:
            return []
        start, end = al.dest_start, al.dest_end
    idx = sorted({owner[k] for k in range(start, min(end, len(owner))) if owner[k] >= 0})
    boxes = [words[i][1] for i in idx]
    # merge per line
    lines: list[list[float]] = []
    for b in sorted(boxes, key=lambda b: (round(b[1]), b[0])):
        if lines and abs(lines[-1][1] - b[1]) < 3 and b[0] - lines[-1][2] < 30:
            lines[-1] = [min(lines[-1][0], b[0]), min(lines[-1][1], b[1]), max(lines[-1][2], b[2]), max(lines[-1][3], b[3])]
        else:
            lines.append(list(b))
    return lines
