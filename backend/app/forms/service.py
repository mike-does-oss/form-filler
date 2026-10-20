"""Form upload -> parse -> enrich -> persist, with template reuse by fingerprint."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pymupdf as fitz

from app import db
from app.config import get_config
from app.db.models import FIELD_TYPES, FieldDef
from app.forms.acroform import is_acroform, parse_acroform
from app.forms.common import heuristic_kind
from app.forms.docx_form import parse_docx_form
from app.forms.fingerprint import fingerprint
from app.forms.flat_pdf import parse_flat_pdf

log = logging.getLogger(__name__)


def detect_form_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return "docx"
    if ext == ".pdf":
        return "acroform" if is_acroform(path) else "flat_pdf"
    raise ValueError(f"Unsupported form type: {ext} (expected .pdf or .docx)")


def parse_form_file(path: Path, form_type: str) -> list[FieldDef]:
    if form_type == "acroform":
        return parse_acroform(path)
    if form_type == "docx":
        return parse_docx_form(path)
    return parse_flat_pdf(path)


def form_text(path: Path, form_type: str, limit: int = 6000) -> str:
    if form_type == "docx":
        from app.forms.docx_form import docx_first_text

        return docx_first_text(path)[:limit]
    doc = fitz.open(path)
    return "\n".join(p.get_text("text") for p in doc)[:limit]


# ------------------------------------------------------------------ LLM enrichment
ENRICH_SYSTEM = """You review fields parsed from a blank form. For each field:
- label: a short, clear human label (fix truncation, drop leading question numbers only if they add nothing). Keep the meaning; do not invent.
- kind: "lookup" if the answer is a fact stated in a person's documents (names, dates, numbers, addresses, IDs, amounts).
  "judgment" if answering requires applying criteria to facts (eligibility, yes/no "does X apply to you", residency status, category selection based on rules).
- format_hint: the expected format if the form states or clearly implies one (e.g. "DD/MM/YYYY"), else null.
- type: the best field type.
Return every field key exactly once."""


def enrich_fields(fields: list[FieldDef], text: str) -> None:
    """Optional LLM pass: clean labels, infer format hints, classify lookup vs judgment."""
    from app.extraction.llm_adapter import get_llm

    for f in fields:
        f.kind = heuristic_kind(f)  # fallback if the LLM is unavailable
    candidates = [f for f in fields if f.type != "signature"]
    if not candidates:
        return
    for start in range(0, len(candidates), 30):
        batch = candidates[start : start + 30]
        props = {
            f.key: {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "kind": {"type": "string", "enum": ["lookup", "judgment"]},
                    "format_hint": {"type": ["string", "null"]},
                    "type": {"type": "string", "enum": list(FIELD_TYPES)},
                },
                "required": ["label", "kind", "format_hint", "type"],
            }
            for f in batch
        }
        schema = {"type": "object", "properties": props, "required": list(props)}
        listing = "\n".join(
            f"- key={f.key} | label={f.label!r} | type={f.type} | options={f.options} | section={f.section!r}"
            + (f" | instructions={f.instructions[:300]!r}" if f.instructions else "")
            for f in batch
        )
        msgs = [
            {"role": "system", "content": ENRICH_SYSTEM},
            {"role": "user", "content": f"Form text (excerpt):\n{text[:5000]}\n\nFields:\n{listing}"},
        ]
        try:
            out = get_llm().chat_json(msgs, schema)
        except Exception as e:  # noqa: BLE001
            log.warning("field enrichment skipped (LLM unavailable): %s", e)
            return
        for f in batch:
            r = out.get(f.key) if isinstance(out, dict) else None
            if not isinstance(r, dict):
                continue
            lab = (r.get("label") or "").strip()
            if lab and len(lab) <= 200:
                f.meta.setdefault("label_parsed", f.label)
                f.label = lab
            if r.get("kind") in ("lookup", "judgment"):
                f.kind = r["kind"]
            if r.get("format_hint") and not f.format_hint:
                f.format_hint = str(r["format_hint"])[:40]
            t = r.get("type")
            # Only let the model refine free-text types; widget types are authoritative.
            if t in FIELD_TYPES and f.type in ("text", "multiline") and t not in ("checkbox", "choice", "signature"):
                f.type = t
        for f in batch:
            if f.type in ("number", "currency", "email", "phone", "date") and f.kind == "judgment":
                # stated values; the user can still switch the kind in the template editor
                f.kind = "lookup"


# ------------------------------------------------------------------ persistence
def field_row(form_id: int, f: FieldDef) -> dict:
    return {
        "form_id": form_id,
        "key": f.key,
        "label": f.label,
        "type": f.type,
        "kind": f.kind,
        "section": f.section,
        "required": int(f.required),
        "options_json": db.dumps(f.options),
        "format_hint": f.format_hint,
        "instructions": f.instructions,
        "page": f.page,
        "bbox_json": db.dumps(f.bbox) if f.bbox else None,
        "source_ref": f.source_ref,
        "meta_json": db.dumps(f.meta),
        "order_index": f.order_index,
    }


def row_to_field(r: dict) -> FieldDef:
    return FieldDef(
        id=r.get("id"),
        key=r["key"],
        label=r["label"],
        type=r["type"],
        kind=r["kind"],
        section=r.get("section"),
        required=bool(r.get("required")),
        options=db.loads(r.get("options_json"), []),
        format_hint=r.get("format_hint"),
        instructions=r.get("instructions"),
        page=r.get("page"),
        bbox=db.loads(r.get("bbox_json")),
        source_ref=r.get("source_ref"),
        meta=db.loads(r.get("meta_json"), {}),
        order_index=r.get("order_index") or 0,
    )


def get_fields(form_id: int) -> list[FieldDef]:
    return [row_to_field(r) for r in db.q("SELECT * FROM fields WHERE form_id = ? ORDER BY order_index, id", (form_id,))]


def schema_from_fields(fields: list[FieldDef]) -> list[dict]:
    return [f.model_dump(exclude={"id"}) for f in fields]


def create_form(upload_path: Path, filename: str, *, enrich: bool = True, setup_judgment: bool = True) -> int:
    cfg = get_config()
    form_type = detect_form_type(upload_path)
    fp = fingerprint(form_type, upload_path)
    tpl = db.q1("SELECT * FROM form_templates WHERE fingerprint = ?", (fp,))
    page_count = 1
    if form_type != "docx":
        page_count = len(fitz.open(upload_path))

    if tpl:
        fields = [FieldDef.model_validate(x) for x in db.loads(tpl["field_schema_json"], [])]
        template_id = tpl["id"]
        matched = True
    else:
        fields = parse_form_file(upload_path, form_type)
        if enrich:
            enrich_fields(fields, form_text(upload_path, form_type))
        else:
            for f in fields:
                f.kind = heuristic_kind(f)
        template_id = db.insert(
            "form_templates",
            {
                "fingerprint": fp,
                "name": Path(filename).stem,
                "form_type": form_type,
                "field_schema_json": db.dumps(schema_from_fields(fields)),
                "field_mappings_json": db.dumps(
                    {f.key: {"page": f.page, "bbox": f.bbox} for f in fields if form_type == "flat_pdf"}
                ),
            },
        )
        matched = False

    form_id = db.insert(
        "forms",
        {
            "filename": filename,
            "stored_path": "",
            "form_type": form_type,
            "fingerprint": fp,
            "template_id": template_id,
            "page_count": page_count,
        },
    )
    dest_dir = cfg.data_path / "forms" / str(form_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("form" + upload_path.suffix.lower())
    shutil.copyfile(upload_path, dest)
    db.ex("UPDATE forms SET stored_path = ? WHERE id = ?", (str(dest), form_id))
    with db.transaction():
        for f in fields:
            db.insert("fields", field_row(form_id, f))

    if setup_judgment and not matched:
        from app.judgment.setup import ensure_rules_for_template

        try:
            ensure_rules_for_template(template_id, get_fields(form_id))
        except Exception as e:  # noqa: BLE001
            log.warning("judgment rule proposal failed: %s", e)
    return form_id


def update_fields(form_id: int, updates: list[dict], replace: bool = False) -> None:
    """Edit labels/types/kinds/mappings. Changes are written back to the template too.

    With replace=True the list is the complete field set (manual mapper: add/delete).
    """
    form = db.q1("SELECT * FROM forms WHERE id = ?", (form_id,))
    if not form:
        raise KeyError(form_id)
    current = {f.key: f for f in get_fields(form_id)}
    new_fields: list[FieldDef] = []
    seen: set[str] = set()
    for i, u in enumerate(updates):
        key = u.get("key")
        base = current.get(key) if key else None
        data = base.model_dump() if base else {}
        data.update({k: v for k, v in u.items() if k != "id"})
        if not data.get("key"):
            from app.forms.common import slugify, unique_key

            data["key"] = unique_key(slugify(data.get("label") or "field"), set(current) | seen)
        if form["form_type"] == "flat_pdf" and data.get("bbox") and not data.get("source_ref"):
            data["source_ref"] = f"bbox:{data.get('page') or 1}:" + ",".join(str(round(v, 1)) for v in data["bbox"])
            data.setdefault("meta", {})["element"] = "overlay"
        data.setdefault("order_index", i)
        fd = FieldDef.model_validate(data)
        seen.add(fd.key)
        new_fields.append(fd)
    if not replace:
        merged = dict(current)
        for f in new_fields:
            merged[f.key] = f
        new_fields = sorted(merged.values(), key=lambda f: f.order_index)
    with db.transaction() as conn:
        existing_ids = {r["key"]: r["id"] for r in db.q("SELECT id, key FROM fields WHERE form_id = ?", (form_id,))}
        keep = {f.key for f in new_fields}
        for key, fid in existing_ids.items():
            if key not in keep:
                conn.execute("DELETE FROM fields WHERE id = ?", (fid,))
        for f in new_fields:
            row = field_row(form_id, f)
            if f.key in existing_ids:
                db.update("fields", existing_ids[f.key], row)
            else:
                db.insert("fields", row)
        if form["template_id"]:
            fields = get_fields(form_id)
            conn.execute(
                "UPDATE form_templates SET field_schema_json = ?, field_mappings_json = ?, updated_at = datetime('now') WHERE id = ?",
                (
                    db.dumps(schema_from_fields(fields)),
                    db.dumps({f.key: {"page": f.page, "bbox": f.bbox} for f in fields if form["form_type"] == "flat_pdf"}),
                    form["template_id"],
                ),
            )
    if form["template_id"]:
        from app.judgment.setup import ensure_rules_for_template

        try:
            ensure_rules_for_template(form["template_id"], get_fields(form_id))
        except Exception as e:  # noqa: BLE001
            log.warning("judgment rule proposal failed: %s", e)


def render_form_page(form_id: int, page: int, dpi: int = 110) -> bytes:
    form = db.q1("SELECT * FROM forms WHERE id = ?", (form_id,))
    if not form or form["form_type"] == "docx":
        raise FileNotFoundError(form_id)
    doc = fitz.open(form["stored_path"])
    if page < 1 or page > len(doc):
        raise FileNotFoundError(page)
    return doc[page - 1].get_pixmap(dpi=dpi).tobytes("png")
