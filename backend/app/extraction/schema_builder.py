"""JSON schemas for constrained decoding, generated from the field definitions."""

from __future__ import annotations

from typing import Any

from app.db.models import CANNOT_DETERMINE, FieldDef

DERIVATION_OPS = ["sum", "difference", "product", "ratio", "average", "min", "max", "count", "date_diff_days", "age_years"]

_EVIDENCE = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"chunk_id": {"type": "integer"}, "quote": {"type": "string"}},
        "required": ["chunk_id", "quote"],
    },
}


def value_schema(f: FieldDef) -> dict[str, Any]:
    if f.type == "choice" and f.options:
        return {"anyOf": [{"type": "string", "enum": list(f.options)}, {"type": "null"}]}
    if f.type == "checkbox":
        return {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
    return {"type": ["string", "null"]}


def field_schema(f: FieldDef) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "key": {"type": "string", "enum": [f.key]},
            "value": value_schema(f),
            "evidence": _EVIDENCE,
            "derived": {"type": "boolean"},
            "derivation": {
                "anyOf": [
                    {"type": "null"},
                    {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": DERIVATION_OPS},
                            "inputs": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "value": {"type": "string"},
                                        "chunk_id": {"type": "integer"},
                                        "quote": {"type": "string"},
                                    },
                                    "required": ["value", "chunk_id", "quote"],
                                },
                            },
                        },
                        "required": ["op", "inputs"],
                    },
                ]
            },
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "string"},
                        "chunk_id": {"type": "integer"},
                        "quote": {"type": "string"},
                    },
                    "required": ["value", "chunk_id", "quote"],
                },
            },
        },
        "required": ["key", "value", "evidence", "derived", "derivation", "candidates"],
    }


def batch_schema(fields: list[FieldDef]) -> dict[str, Any]:
    props = {f.key: field_schema(f) for f in fields}
    return {"type": "object", "properties": props, "required": [f.key for f in fields]}


def judgment_schema(options: list[str], fact_keys: list[str]) -> dict[str, Any]:
    answers = list(dict.fromkeys([*options, CANNOT_DETERMINE]))
    fact_key_schema: dict[str, Any] = {"type": "string"}
    if fact_keys:
        fact_key_schema = {"type": "string", "enum": fact_keys}
    # property order matters: facts and reasoning are generated before the answer
    return {
        "type": "object",
        "properties": {
            "facts_used": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"fact_key": fact_key_schema, "value": {"type": "string"}},
                    "required": ["fact_key", "value"],
                },
            },
            "reasoning": {"type": "string"},
            "answer": {"type": "string", "enum": answers},
            "missing_facts": {"type": "array", "items": fact_key_schema},
        },
        "required": ["facts_used", "reasoning", "answer", "missing_facts"],
    }
