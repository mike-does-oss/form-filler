"""Prompt text for extraction and judgment calls."""

from __future__ import annotations

from app import db
from app.db.models import FieldDef

EXTRACTION_SYSTEM = """You extract values for form fields from source documents. Follow these rules strictly:

1. Use ONLY the provided source text. If the information is not there, return "value": null. Never guess, infer from typical values, or fill placeholders.
2. Every non-null value needs evidence: the chunk_id and a quote COPIED EXACTLY, character for character, from that chunk. Keep quotes short (the line or phrase that contains the value).
3. If sources disagree (different values for the same field), list every distinct value in "candidates", each with its own chunk_id and exact quote. Set "value" to the best-supported one only if one is clearly more authoritative or more recent, otherwise null.
4. If a value must be calculated (e.g. a total of several amounts, an age from a date of birth), set "derived": true, give the operation in "derivation.op" and every input value with its own chunk_id and exact quote in "derivation.inputs". Do NOT compute the result yourself; set "value" to null. The system does the arithmetic.
5. Otherwise "derived": false and "derivation": null.
6. Return values as they appear in the source; the system normalises formats. For checkbox fields return true/false only when the source states it.
7. A field's instructions (from the form itself) explain what it means; follow them.
8. Do not fill a field from information about a different person or entity than the one the field asks about."""


def describe_field(f: FieldDef) -> str:
    parts = [f"- key: {f.key}", f"  label: {f.label}", f"  type: {f.type}"]
    if f.section:
        parts.append(f"  section: {f.section}")
    if f.options:
        parts.append(f"  options: {f.options}")
    if f.format_hint:
        parts.append(f"  format: {f.format_hint}")
    if f.instructions:
        parts.append(f"  instructions: {f.instructions[:600]}")
    return "\n".join(parts)


def render_chunks(chunks: list[dict], filenames: dict[int, str]) -> str:
    out = []
    for c in chunks:
        out.append(
            f"[chunk_id={c['id']}] (document: {filenames.get(c['document_id'], c['document_id'])}, page {c['page']})\n{c['text']}"
        )
    return "\n\n---\n\n".join(out)


def render_lookup_fewshot(corrections: list[dict]) -> str:
    if not corrections:
        return ""
    lines = ["Past corrections made by a reviewer on this same form (learn from them):"]
    for c in corrections:
        ev = db.loads(c.get("evidence_json"), []) or []
        quote = ev[0].get("quote") if ev and isinstance(ev[0], dict) else None
        line = f"- field {c['field_key']} ({c.get('field_label') or ''}): model said {c['model_value']!r}, correct value is {c['user_value']!r}"
        if quote:
            line += f" (supported by: {quote!r})"
        if c.get("user_note"):
            line += f". Reviewer note: {c['user_note']}"
        lines.append(line)
    return "\n".join(lines)


def extraction_messages(fields: list[FieldDef], chunks: list[dict], filenames: dict[int, str], fewshot: list[dict]) -> list[dict]:
    user = []
    fs = render_lookup_fewshot(fewshot)
    if fs:
        user.append(fs)
    user.append("FIELDS TO FILL:\n" + "\n".join(describe_field(f) for f in fields))
    user.append("SOURCE TEXT:\n" + (render_chunks(chunks, filenames) if chunks else "(no source text)"))
    user.append("Return a JSON object with one entry per field key.")
    return [{"role": "system", "content": EXTRACTION_SYSTEM}, {"role": "user", "content": "\n\n".join(user)}]


JUDGMENT_SYSTEM = """You decide the answer to one form question by applying the stated rule (rubric) to the extracted facts.
- Use only the facts given. Do not assume facts that are missing.
- First list the facts you used, then reason step by step in 1-4 sentences, then give the answer.
- If a fact needed by the rubric is missing or unclear, answer "cannot_determine" and list the missing fact keys.
- The answer must be one of the allowed options or "cannot_determine"."""


def judgment_messages(field: FieldDef, rubric: str, facts: list[dict], fewshot: list[dict]) -> list[dict]:
    lines = [f"QUESTION: {field.label}"]
    if field.options:
        lines.append(f"ALLOWED ANSWERS: {field.options + ['cannot_determine']}")
    if field.instructions:
        lines.append(f"FORM INSTRUCTIONS: {field.instructions}")
    lines.append(f"RULE / RUBRIC:\n{rubric}")
    lines.append("FACTS:")
    for f in facts:
        val = f.get("value")
        status = f.get("status")
        q = ""
        ev = f.get("evidence") or []
        if ev:
            q = f" (source quote: {ev[0].get('quote', '')!r})"
        if val in (None, ""):
            lines.append(f"- {f['fact_key']} ({f['label']}): NOT FOUND")
        else:
            lines.append(f"- {f['fact_key']} ({f['label']}): {val}{q}" + (f" [status: {status}]" if status else ""))
    if fewshot:
        lines.append("\nPAST REVIEWER DECISIONS FOR THIS QUESTION (facts -> correct answer):")
        for c in fewshot:
            facts_json = db.loads(c.get("facts_json"), {}) or {}
            fact_str = ", ".join(f"{k}={v}" for k, v in facts_json.items())
            line = f"- facts: {fact_str} -> correct answer: {c['user_value']}"
            if c.get("model_value") and c["model_value"] != c["user_value"]:
                line += f" (model had said {c['model_value']})"
            if c.get("user_note"):
                line += f". Why: {c['user_note']}"
            lines.append(line)
    return [{"role": "system", "content": JUDGMENT_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]
