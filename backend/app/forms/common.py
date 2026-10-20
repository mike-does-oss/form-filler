"""Helpers shared by the form parsers: keys, type inference, sections, instructions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from app.db.models import FieldDef

MEANINGLESS_NAME = re.compile(
    r"^(text|text\s*field|check\s*box|checkbox|field|untitled|fill|dropdown|combo\s*box|radio(\s*button)?|group|button|"
    r"f\d|c\d|topmostsubform)[\s_\-.\[\]0-9]*$|\[\d+\]|^[A-Za-z]{0,3}[_\-.]?\d+$",
    re.I,
)
QNUM = re.compile(r"^\s*(?:Q(?:uestion)?\s*)?(\d{1,3}[a-z]?)[.):]\s+", re.I)
FORMAT_HINT = re.compile(r"\b(DD\s*/\s*MM\s*/\s*YYYY|MM\s*/\s*DD\s*/\s*YYYY|YYYY\s*-\s*MM\s*-\s*DD|DD\s*-\s*MM\s*-\s*YYYY|DD\.MM\.YYYY)\b", re.I)
YES_NO = {"yes", "no"}


def slugify(text: str, max_len: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    s = re.sub(r"^(q\d+[a-z]?_|\d+[a-z]?_)", "", s)
    return (s[:max_len].rstrip("_")) or "field"


def unique_key(base: str, used: set[str]) -> str:
    key = base
    i = 2
    while key in used:
        key = f"{base}_{i}"
        i += 1
    used.add(key)
    return key


def is_meaningless_name(name: str | None) -> bool:
    if not name:
        return True
    leaf = name.split(".")[-1]
    return bool(MEANINGLESS_NAME.search(leaf)) or len(leaf) <= 2


def clean_label(text: str) -> str:
    t = re.sub(r"[_.…]{3,}", " ", text)
    t = re.sub(r"\s+", " ", t).strip()
    t = t.rstrip(":*").strip()
    return t


def question_number(label: str) -> str | None:
    m = QNUM.match(label)
    return m.group(1).lower() if m else None


def strip_question_number(label: str) -> str:
    return QNUM.sub("", label).strip()


def is_required_label(label: str) -> bool:
    return label.rstrip().endswith("*") or "(required)" in label.lower()


def find_format_hint(*texts: str | None) -> str | None:
    for t in texts:
        if not t:
            continue
        m = FORMAT_HINT.search(t)
        if m:
            return re.sub(r"\s+", "", m.group(1)).upper()
    return None


def infer_text_type(label: str, instructions: str | None = None, multiline: bool = False) -> str:
    lab = label.lower()
    text = f"{lab} {(instructions or '').lower()}"
    if "signature" in lab or re.search(r"\bsign(ed)?\b", lab) and "date" not in lab:
        return "signature"
    if re.search(r"\b(e-?mail)\b", lab):
        return "email"
    if re.search(r"\b(phone|mobile|telephone|tel\.?|fax|contact number)\b", lab):
        return "phone"
    if re.search(r"\b(date|dob|d\.o\.b|born)\b", lab) or find_format_hint(text):
        return "date"
    if re.search(r"(\$|\bamount\b|\bincome\b|\bsalary\b|\btotal\b|\bfee\b|\bprice\b|\bcost\b|\bwages?\b|\bbalance\b|\bpayment\b|\baud\b|\busd\b)", lab):
        return "currency"
    if re.search(r"\b(number of|no\. of|how many|count|age|days|years|quantity|percentage|odometer|km|kilometres|%)\b", lab) and not re.search(
        r"\b(phone|abn|tfn|account|passport|licen[cs]e|policy|reference|id)\b", lab
    ):
        return "number"
    if multiline or re.search(r"\b(describe|details|explain|comments)\b", lab):
        return "multiline"
    return "text"


@dataclass
class TextLine:
    page: int
    text: str
    bbox: list[float]
    size: float = 10.0
    bold: bool = False


@dataclass
class Heading:
    page: int
    y: float
    text: str


@dataclass
class NoteBlock:
    page: int
    text: str
    bbox: list[float] | None = None
    qnum: str | None = None
    title: str | None = None


@dataclass
class InstructionContext:
    notes: list[NoteBlock] = field(default_factory=list)  # from instruction pages / notes sections
    footnotes: dict[str, str] = field(default_factory=dict)  # marker -> text


SECTION_PAT = re.compile(r"^(section|part|schedule)\s+[A-Z0-9]+\b", re.I)
NOTES_PAT = re.compile(r"^(?:notes?\s+(?:to|for|on)\s+)?(?:question|q)\s*(\d{1,3}[a-z]?)\b[.:)]?\s*(.*)$", re.I)


def is_heading(line: TextLine, body_size: float) -> bool:
    t = line.text.strip()
    if not t or len(t) > 90 or t.endswith(":") or "___" in t:
        return False
    if SECTION_PAT.match(t):
        return True
    if line.size >= body_size * 1.25:
        return True
    if line.bold and len(t) < 60 and not t.endswith("?") and not QNUM.match(t):
        return True
    return False


def assign_sections(fields: list[FieldDef], headings: list[Heading]) -> None:
    hs = sorted(headings, key=lambda h: (h.page, h.y))
    for f in fields:
        if f.page is None or f.bbox is None:
            continue
        current = None
        for h in hs:
            if (h.page, h.y) <= (f.page, f.bbox[1] + 1):
                current = h.text
            else:
                break
        if current and not f.section:
            f.section = current


def parse_notes_text(page: int, lines: list[str]) -> list[NoteBlock]:
    """Split instruction-page text into per-question note blocks."""
    notes: list[NoteBlock] = []
    cur: NoteBlock | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = NOTES_PAT.match(line)
        if m:
            if cur:
                notes.append(cur)
            cur = NoteBlock(page=page, text=m.group(2).strip(), qnum=m.group(1).lower(), title=line)
            continue
        if cur:
            cur.text = (cur.text + " " + line).strip()
    if cur:
        notes.append(cur)
    return notes


def link_instructions(fields: list[FieldDef], ctx: InstructionContext) -> None:
    """Attach notes (by question number, then label match) and footnotes to fields."""
    for f in fields:
        found: list[str] = []
        qn = f.meta.get("question_no")
        for n in ctx.notes:
            if qn and n.qnum and n.qnum == qn:
                found.append(n.text)
            elif not n.qnum and n.title and fuzz.partial_ratio(f.label.lower(), n.title.lower()) >= 90:
                found.append(n.text)
        marker = f.meta.get("footnote_marker")
        if marker and marker in ctx.footnotes:
            found.append(ctx.footnotes[marker])
        if found:
            existing = [f.instructions] if f.instructions else []
            merged = " ".join(existing + [t for t in found if t and t not in existing])
            f.instructions = merged.strip() or None
        hint = find_format_hint(f.label, f.instructions)
        if hint and not f.format_hint:
            f.format_hint = hint
            if f.type in ("text",):
                f.type = "date"


FOOTNOTE_MARK = re.compile(r"(\*{1,3}|[¹²³†‡])\s*$")
FOOTNOTE_LINE = re.compile(r"^(\*{1,3}|[¹²³†‡])\s*(.+)$")


def footnote_marker(label: str) -> str | None:
    m = FOOTNOTE_MARK.search(label.strip())
    if not m:
        return None
    mk = m.group(1)
    # a single trailing '*' usually means "required", not a footnote, unless there's a footnote line for it
    return mk


def heuristic_kind(f: FieldDef) -> str:
    """Fallback classifier when no LLM is available."""
    lab = strip_question_number(f.label).lower()
    opts = {o.lower() for o in f.options}
    judgment_start = re.match(
        r"^(are|is|do|does|did|have|has|were|was|will|would|should|can)\b.*\?", lab
    ) or re.search(r"\b(eligible|eligibility|qualif(y|ies)|resident for|apply to you|applies to you|entitled)\b", lab)
    if judgment_start and (f.type in ("checkbox", "choice") or opts <= YES_NO | {"n/a", "unsure"}):
        return "judgment"
    if f.instructions and re.search(r"\b(if you|you must|you are considered|criteria|threshold|at least|more than)\b", f.instructions.lower()) and f.type in ("checkbox", "choice"):
        return "judgment"
    return "lookup"
