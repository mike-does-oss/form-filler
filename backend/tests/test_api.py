"""End-to-end through the HTTP API with a scripted fake LLM: upload, run, review, correct, export, delete."""

from __future__ import annotations

import json
import re
import time

import pymupdf as fitz
from fastapi.testclient import TestClient

from app import db
from app.main import create_app

from .conftest import FIXTURES, find_quote

SET = FIXTURES / "01_acroform_basic"

PATTERNS = {
    "full_name": r"Account holder: Priya Natarajan",
    "date_of_birth_dd_mm_yyyy": r"Date of birth \| 14 March 1990",
    "email_address": r"priya\.natarajan@example\.com",
    "annual_salary": r"\$92,500\.00",
    "occupation": r"Senior\s+Logistics\s+Coordinator",
}
VALUES = {
    "full_name": "Priya Natarajan",
    "date_of_birth_dd_mm_yyyy": "14 March 1990",
    "email_address": "priya.natarajan@example.com",
    "annual_salary": "$92,500.00",
    "occupation": "Logistics Coordinator",  # deliberately wrong-ish -> user edits it
}


def responder(messages, schema, **kw):
    props = schema.get("properties", {})
    if props and all("kind" in v.get("properties", {}) for v in props.values()):
        return {}  # label enrichment: keep parser labels and heuristic kinds
    if "answer" in props:
        return {"facts_used": [], "reasoning": "241 days is at least 183.", "answer": "Yes", "missing_facts": []}
    if "facts" in props and "rubric" in props:
        return {"facts": [{"key": "days_present", "label": "Days present in Australia in 2023-24", "type": "number",
                           "question": "How many days were you in Australia between 1 July 2023 and 30 June 2024?"}],
                "rubric": "Yes if days_present >= 183, otherwise No.", "code_rule": None}
    if not props or "evidence" not in next(iter(props.values())).get("properties", {}):
        return None  # enrichment etc. -> empty default
    out = {}
    for key in props:
        entry = {"key": key, "value": None, "evidence": [], "derived": False, "derivation": None, "candidates": []}
        if key.endswith("__days_present"):
            q = find_quote(messages, r"present\s+in Australia for 241 days")
            if q:
                entry.update(value="241", evidence=[{"chunk_id": q[0], "quote": q[1]}])
        elif key in PATTERNS:
            q = find_quote(messages, PATTERNS[key])
            if q:
                entry.update(value=VALUES[key], evidence=[{"chunk_id": q[0], "quote": q[1]}])
        out[key] = entry
    return out


def wait_done(client, job_id, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "failed"):
            return j
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_full_flow(app_env):
    app_env.responder = responder
    with TestClient(create_app()) as client:
        assert client.get("/api/health").json()["llm"]["reachable"]
        with open(SET / "form.pdf", "rb") as fh:
            form = client.post("/api/forms", files={"file": ("form.pdf", fh, "application/pdf")}).json()
        assert form["form_type"] == "acroform" and len(form["fields"]) == 16
        assert not form["template_matched"]
        resident = next(f for f in form["fields"] if f["key"].startswith("were_you"))
        assert resident["kind"] == "judgment"

        job = client.post("/api/jobs", json={"form_id": form["id"]}).json()
        files = [("files", (p.name, p.read_bytes(), "application/pdf")) for p in sorted((SET / "sources").iterdir())]
        docs = client.post(f"/api/jobs/{job['id']}/documents", files=files).json()
        assert len(docs) == 3
        assert client.post(f"/api/jobs/{job['id']}/run").status_code == 200
        j = wait_done(client, job["id"])
        assert j["status"] == "done", j["error"]

        vals = {v["key"]: v for v in client.get(f"/api/jobs/{job['id']}/values").json()["fields"]}
        assert vals["full_name"]["status"] == "verified" and vals["full_name"]["confidence"] == "high"
        ev = vals["full_name"]["evidence"][0]
        assert ev["verified"] and ev["boxes"] and ev["page"] == 1
        assert vals["date_of_birth_dd_mm_yyyy"]["value"] == "1990-03-14"
        assert vals["annual_salary"]["value"] == "92500.00"
        assert vals["previous_surname_if_any"]["status"] == "not_found"
        assert vals["signature_of_applicant"]["requires_manual_signing"]
        r = vals[resident["key"]]
        assert r["status"] == "judged" and r["value"] == "Yes" and r["run_agreement"] == 1.0 and r["confidence"] == "medium"
        assert r["facts"][0]["value"] == "241"

        # review: accept, edit (-> correction), not applicable
        fid = vals["full_name"]["field_id"]
        assert client.patch(f"/api/jobs/{job['id']}/values/{fid}", json={"action": "accept"}).json()["review_status"] == "accepted"
        occ = vals["occupation"]["field_id"]
        edited = client.patch(f"/api/jobs/{job['id']}/values/{occ}", json={"action": "edit", "value": "Senior Logistics Coordinator"}).json()
        assert edited["final_value"] == "Senior Logistics Coordinator" and edited["user_edited"]
        client.patch(f"/api/jobs/{job['id']}/values/{r['field_id']}", json={"action": "accept"})
        assert db.q1("SELECT COUNT(*) AS n FROM corrections")["n"] == 1
        audit = client.get(f"/api/jobs/{job['id']}/audit").json()
        assert {"model_fill", "accept", "edit"} <= {a["action"] for a in audit}

        # exports
        res = client.post(f"/api/jobs/{job['id']}/export", json={"format": "filled"})
        assert res.status_code == 200 and res.content[:4] == b"%PDF"
        doc = fitz.open(stream=res.content, filetype="pdf")
        got = {w.field_name: w.field_value for p in doc for w in p.widgets()}
        name_ref = next(f["source_ref"] for f in form["fields"] if f["key"] == "full_name")
        res_ref = resident["source_ref"]
        assert got[name_ref] == "Priya Natarajan"
        radio = [(w.on_state(), w.field_value) for p in doc for w in p.widgets() if w.field_name == res_ref]
        assert ("Yes", "Yes") in radio  # accepted judgment answer written
        assert client.post(f"/api/jobs/{job['id']}/export", json={"format": "flattened"}).content[:4] == b"%PDF"
        js = json.loads(client.post(f"/api/jobs/{job['id']}/export", json={"format": "json"}).content)
        occ_out = next(x for x in js["fields"] if x["key"] == "occupation")
        assert occ_out["value"] == "Senior Logistics Coordinator" and occ_out["model_value"] == "Logistics Coordinator"
        csv = client.post(f"/api/jobs/{job['id']}/export", json={"format": "csv"}).text
        assert "14/03/1990" in csv
        assert client.get(f"/api/jobs/{job['id']}/preview/pages/1").content[:4] == b"\x89PNG"
        assert client.get(f"/api/documents/{docs[0]['id']}/pages/1").content[:4] == b"\x89PNG"

        # P6: same form again -> template reused, correction injected as a few-shot example
        with open(SET / "form.pdf", "rb") as fh:
            form2 = client.post("/api/forms", files={"file": ("form.pdf", fh, "application/pdf")}).json()
        assert form2["template_matched"] and form2["template_id"] == form["template_id"]
        job2 = client.post("/api/jobs", json={"form_id": form2["id"]}).json()
        client.post(f"/api/jobs/{job2['id']}/documents", files=files)
        app_env.calls.clear()
        client.post(f"/api/jobs/{job2['id']}/run")
        assert wait_done(client, job2["id"])["status"] == "done"
        prompts = [c["messages"][-1]["content"] for c in app_env.calls]
        assert any("Past corrections" in p and "Senior Logistics Coordinator" in p for p in prompts)

        # delete removes files, chunks and values
        assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
        assert db.q1("SELECT COUNT(*) AS n FROM chunks c JOIN documents d ON d.id = c.document_id WHERE d.job_id = ?", (job["id"],))["n"] == 0
        assert db.q1("SELECT COUNT(*) AS n FROM field_values WHERE job_id = ?", (job["id"],))["n"] == 0
        assert client.get(f"/api/jobs/{job['id']}").status_code == 404


def test_cannot_determine_flow(app_env):
    """Missing fact -> cannot_determine with a question -> user answers -> decision re-runs."""

    def resp(messages, schema, **kw):
        props = schema.get("properties", {})
        if "answer" in props:
            text = messages[-1]["content"]
            m = re.search(r"days_present \(.*?\): (\d+)", text)
            ans = "Yes" if m and int(m.group(1)) >= 183 else "cannot_determine"
            return {"facts_used": [], "reasoning": "r", "answer": ans, "missing_facts": [] if m else ["days_present"]}
        return responder(messages, schema)

    app_env.responder = lambda m, s, **k: resp(m, s) if "answer" in s.get("properties", {}) else (
        {k2: {"key": k2, "value": None, "evidence": [], "derived": False, "derivation": None, "candidates": []} for k2 in s["properties"]}
        if s.get("properties") and "evidence" in next(iter(s["properties"].values())).get("properties", {}) else responder(m, s))
    with TestClient(create_app()) as client:
        with open(FIXTURES / "06_judgment" / "form.pdf", "rb") as fh:
            form = client.post("/api/forms", files={"file": ("form.pdf", fh, "application/pdf")}).json()
        job = client.post("/api/jobs", json={"form_id": form["id"]}).json()
        files = [("files", (p.name, p.read_bytes(), "application/pdf")) for p in sorted((FIXTURES / "06_judgment" / "sources").iterdir())]
        client.post(f"/api/jobs/{job['id']}/documents", files=files)
        client.post(f"/api/jobs/{job['id']}/run")
        assert wait_done(client, job["id"])["status"] == "done"
        vals = [v for v in client.get(f"/api/jobs/{job['id']}/values").json()["fields"] if v["kind"] == "judgment"]
        assert vals and all(v["status"] == "cannot_determine" for v in vals)
        v = vals[0]
        assert v["missing_facts"][0]["question"].startswith("How many days")
        out = client.post(f"/api/jobs/{job['id']}/values/{v['field_id']}/facts", json={"answers": {"days_present": "200"}}).json()
        assert out["status"] == "judged" and out["value"] == "Yes"
        assert out["facts"][0]["source"] == "user"
