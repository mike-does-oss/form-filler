"""Domain models shared across the pipeline (Pydantic v2)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

FieldType = Literal[
    "text", "multiline", "number", "currency", "date", "checkbox", "choice", "email", "phone", "signature"
]
FIELD_TYPES: tuple[str, ...] = (
    "text", "multiline", "number", "currency", "date", "checkbox", "choice", "email", "phone", "signature",
)
FieldKind = Literal["lookup", "judgment"]
FormType = Literal["acroform", "docx", "flat_pdf"]
Status = Literal["verified", "unverified", "derived", "conflict", "not_found", "judged", "cannot_determine"]
Confidence = Literal["high", "medium", "low"]
CANNOT_DETERMINE = "cannot_determine"


class FieldDef(BaseModel):
    """A field on the blank form (or a hidden fact field)."""

    id: int | None = None
    key: str
    label: str
    type: str = "text"
    kind: FieldKind = "lookup"
    section: str | None = None
    required: bool = False
    options: list[str] = Field(default_factory=list)
    format_hint: str | None = None
    instructions: str | None = None
    page: int | None = None  # 1-based
    bbox: list[float] | None = None  # [x0, y0, x1, y1] in PDF points, top-left origin
    source_ref: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    order_index: int = 0

    @property
    def is_signature(self) -> bool:
        return self.type == "signature"


class FactDef(BaseModel):
    key: str
    label: str
    type: str = "text"
    question: str | None = None  # how to ask the user when missing
    required: bool = True


class Chunk(BaseModel):
    id: int | None = None
    document_id: int
    page: int
    bbox: list[float] | None = None
    text: str
    chunk_index: int = 0
    meta: dict[str, Any] = Field(default_factory=dict)


class Span(BaseModel):
    """A word/line-level text span on a page, used to map quotes to boxes."""

    text: str
    bbox: list[float]


class Evidence(BaseModel):
    document_id: int | None = None
    chunk_id: int | None = None
    page: int | None = None
    bbox: list[float] | None = None
    boxes: list[list[float]] = Field(default_factory=list)  # per-line boxes for highlights
    quote: str = ""
    verified: bool = False
    score: float = 0.0
    value_in_quote: bool = False
    source: str = "model"  # model | user


class Candidate(BaseModel):
    value: Any
    evidence: list[Evidence] = Field(default_factory=list)


class FieldResult(BaseModel):
    key: str
    value: Any = None
    status: Status = "not_found"
    confidence: Confidence = "low"
    evidence: list[Evidence] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    validator_errors: list[str] = Field(default_factory=list)
    derivation: dict[str, Any] | None = None
    reasoning: str | None = None
    facts_used: list[dict[str, Any]] = Field(default_factory=list)
    missing_facts: list[dict[str, Any]] = Field(default_factory=list)
    run_agreement: float | None = None
    rule: dict[str, Any] | None = None
    resolved_by_model: bool = False  # conflict resolved to a single candidate by the model
