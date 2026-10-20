"""Status and confidence rules (spec 6.5, 6.6, 6.10)."""

from __future__ import annotations

from app.db.models import FieldDef, FieldResult


def lookup_status(result: FieldResult) -> str:
    if result.value is None and not result.candidates:
        return "not_found"
    distinct = {str(c.value) for c in result.candidates}
    if len(distinct) > 1:
        return "conflict"
    if result.derivation:
        inputs = result.derivation.get("inputs", [])
        ok = inputs and all(i.get("verified") and i.get("value_in_quote") for i in inputs)
        return "derived" if ok else "unverified"
    if any(e.verified and e.value_in_quote for e in result.evidence):
        return "verified"
    return "unverified"


def lookup_confidence(field: FieldDef, result: FieldResult) -> str:
    s = result.status
    errors = [e for e in result.validator_errors if e != "required field is empty"]
    if s in ("verified", "derived"):
        return "high" if not errors else "medium"
    if s == "conflict":
        chosen_verified = result.value is not None and any(
            str(c.value) == str(result.value) and any(e.verified for e in c.evidence) for c in result.candidates
        )
        return "medium" if chosen_verified and result.resolved_by_model and not errors else "low"
    if s == "not_found":
        # an empty optional field is a confident "leave blank"; an empty required one needs a human
        return "low" if field.required else "medium"
    return "low"


def judgment_confidence(result: FieldResult, rule_type: str, facts_verified: bool) -> str:
    """Judgment fields are never high and never auto-accepted."""
    if result.status == "cannot_determine" or not facts_verified:
        return "low"
    if rule_type == "code":
        return "medium"
    if rule_type == "rubric" and result.run_agreement is not None and result.run_agreement >= 0.999:
        return "medium"
    return "low"
