"""P3: schema builder, retrieval, batched extraction -> verification -> statuses."""

from __future__ import annotations

from app.db.models import FieldDef
from app.extraction.extract import build_result, make_batches
from app.extraction.retrieval import rrf
from app.extraction.schema_builder import batch_schema, judgment_schema


def chunks():
    return {
        1: {"id": 1, "document_id": 10, "page": 1, "bbox_json": "[0,0,100,100]", "text": "Name: Jane Citizen\nDate of birth: 14 March 1990"},
        2: {"id": 2, "document_id": 11, "page": 2, "bbox_json": "[0,0,100,100]", "text": "Address: 8 Banksia Street, Fitzroy"},
        3: {"id": 3, "document_id": 12, "page": 1, "bbox_json": "[0,0,100,100]", "text": "Address: 15 Lygon Street, Brunswick\nSalary $50,000. Bonus $2,500."},
    }


def test_schema_shapes():
    s = batch_schema([FieldDef(key="a", label="A", type="choice", options=["X", "Y"]), FieldDef(key="b", label="B", type="checkbox")])
    assert s["required"] == ["a", "b"]
    assert s["properties"]["a"]["properties"]["value"]["anyOf"][0]["enum"] == ["X", "Y"]
    j = judgment_schema(["Yes", "No"], ["days"])
    assert list(j["properties"]) == ["facts_used", "reasoning", "answer", "missing_facts"]  # reasoning before answer
    assert j["properties"]["answer"]["enum"] == ["Yes", "No", "cannot_determine"]


def test_batches_by_section():
    fs = [FieldDef(key=f"f{i}", label=f"F{i}", section="A" if i < 3 else "B") for i in range(20)]
    batches = make_batches(fs, 5, 15)
    assert all(len(b) <= 15 for _, b in batches)
    assert sum(len(b) for _, b in batches) == 20


def test_rrf():
    assert rrf([[1, 2, 3], [3, 1]])[0] == 1


def test_verified_and_hallucinated_quote():
    f = FieldDef(key="dob", label="Date of birth", type="date")
    r = build_result(f, {"value": "14/03/1990", "evidence": [{"chunk_id": 1, "quote": "Date of birth: 14 March 1990"}]}, chunks(), 90)
    assert r.status == "verified" and r.value == "1990-03-14" and r.confidence == "high"
    assert r.evidence[0].document_id == 10 and r.evidence[0].page == 1
    bad = build_result(f, {"value": "1990-03-14", "evidence": [{"chunk_id": 1, "quote": "DOB 1990-03-14"}]}, chunks(), 90)
    assert bad.status == "unverified" and bad.confidence == "low"
    # quote is real but the value isn't in it
    wrong = build_result(f, {"value": "1991-01-01", "evidence": [{"chunk_id": 1, "quote": "Date of birth: 14 March 1990"}]}, chunks(), 90)
    assert wrong.status == "unverified"


def test_wrong_chunk_id_is_recovered():
    f = FieldDef(key="addr", label="Address")
    r = build_result(f, {"value": "8 Banksia Street, Fitzroy", "evidence": [{"chunk_id": 3, "quote": "Address: 8 Banksia Street, Fitzroy"}]}, chunks(), 90)
    assert r.status == "verified" and r.evidence[0].chunk_id == 2


def test_conflict():
    f = FieldDef(key="addr", label="Address")
    raw = {"value": None, "evidence": [], "candidates": [
        {"value": "8 Banksia Street, Fitzroy", "chunk_id": 2, "quote": "Address: 8 Banksia Street, Fitzroy"},
        {"value": "15 Lygon Street, Brunswick", "chunk_id": 3, "quote": "Address: 15 Lygon Street, Brunswick"},
    ]}
    r = build_result(f, raw, chunks(), 90)
    assert r.status == "conflict" and len(r.candidates) == 2 and r.confidence == "low"
    raw["value"] = "15 Lygon Street, Brunswick"
    r2 = build_result(f, raw, chunks(), 90)
    assert r2.status == "conflict" and r2.confidence == "medium"  # resolved to one verified candidate by the model


def test_derived_sum_done_in_code():
    f = FieldDef(key="total", label="Total income", type="currency")
    raw = {"value": "99999", "derived": True, "evidence": [], "derivation": {"op": "sum", "inputs": [
        {"value": "$50,000", "chunk_id": 3, "quote": "Salary $50,000."},
        {"value": "$2,500", "chunk_id": 3, "quote": "Bonus $2,500."},
    ]}}
    r = build_result(f, raw, chunks(), 90)
    assert r.value == "52500.00" and r.status == "derived" and r.confidence == "high"
    raw["derivation"]["inputs"][1]["quote"] = "Bonus $9,999."
    assert build_result(f, raw, chunks(), 90).status == "unverified"


def test_not_found_and_single_candidate():
    f = FieldDef(key="email", label="Email", type="email", required=True)
    r = build_result(f, {"value": None, "evidence": [], "candidates": []}, chunks(), 90)
    assert r.status == "not_found" and r.confidence == "low" and r.value is None
    g = FieldDef(key="name", label="Name")
    r2 = build_result(g, {"value": None, "evidence": [], "candidates": [{"value": "Jane Citizen", "chunk_id": 1, "quote": "Name: Jane Citizen"}]}, chunks(), 90)
    assert r2.status == "verified" and r2.value == "Jane Citizen"
