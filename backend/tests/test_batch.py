"""Batch fill: one job per folder in, one folder per set out (zip via API, directory via CLI)."""

from __future__ import annotations

import csv
import io
import shutil
import time
import zipfile

from fastapi.testclient import TestClient

from app import db
from app.cli import main as cli_main
from app.jobs.batch import group_files, inner_name
from app.main import create_app

from .test_api import SET, responder


def test_group_files_by_first_folder():
    groups, skipped = group_files(
        [
            "alice/payslip.pdf",
            "alice/2024/licence.PNG",
            "bob/letter.docx",
            "set 10/a.pdf",
            "set 9/a.pdf",
            "loose.pdf",
            "bob/notes.exe",
            "bob/.DS_Store",
            ".hidden/x.pdf",
            "../escape/x.pdf",
        ]
    )
    assert list(groups) == ["alice", "bob", "set 9", "set 10"]  # natural order
    assert groups["alice"] == [0, 1] and groups["bob"] == [2]
    reasons = {s["path"]: s["reason"] for s in skipped}
    assert reasons == {
        "loose.pdf": "not inside a sub-folder",
        "bob/notes.exe": "unsupported file type",
        "../escape/x.pdf": "invalid path",
    }
    assert inner_name("alice/2024/licence.png") == "2024/licence.png"


def _wait(client, batch_id, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        b = client.get(f"/api/batches/{batch_id}").json()
        if b["running"] == 0 and b["done"] + b["failed"] == b["total"]:
            return b
        time.sleep(0.2)
    raise AssertionError("batch did not finish")


def test_batch_api_upload_run_export_delete(app_env):
    app_env.responder = responder
    sources = sorted((SET / "sources").iterdir())
    with TestClient(create_app()) as client:
        with open(SET / "form.pdf", "rb") as fh:
            form = client.post("/api/forms", files={"file": ("form.pdf", fh, "application/pdf")}).json()
        assert client.post("/api/batches", json={"form_id": form["id"], "sets": ["a", "a"]}).status_code == 400

        b = client.post("/api/batches", json={"form_id": form["id"], "name": "October intake", "sets": ["alice", "bob"]}).json()
        assert [j["label"] for j in b["jobs"]] == ["alice", "bob"]
        alice, bob = b["jobs"]
        client.post(f"/api/jobs/{alice['id']}/documents", files=[("files", (p.name, p.read_bytes(), "application/pdf")) for p in sources])
        bank = SET / "sources" / "bank_statement.pdf"
        client.post(f"/api/jobs/{bob['id']}/documents", files=[("files", (bank.name, bank.read_bytes(), "application/pdf"))])
        assert client.get(f"/api/batches/{b['id']}/export.zip").status_code == 409  # nothing finished yet

        client.post(f"/api/batches/{b['id']}/run")
        done = _wait(client, b["id"])
        assert done["done"] == 2, done
        by = {j["label"]: j for j in done["jobs"]}
        assert by["alice"]["filled"] > by["bob"]["filled"] > 0
        assert by["alice"]["needs_review"] >= 1  # the judgment field is never auto-accepted
        assert client.get(f"/api/jobs/{alice['id']}").json()["label"] == "alice"
        assert [x["id"] for x in client.get("/api/batches").json()] == [b["id"]]

        res = client.get(f"/api/batches/{b['id']}/export.zip")
        assert res.status_code == 200 and "October%20intake.zip" in res.headers["content-disposition"]
        z = zipfile.ZipFile(io.BytesIO(res.content))
        names = set(z.namelist())
        for s in ("alice", "bob"):
            assert {f"October intake/{s}/form_filled.pdf", f"October intake/{s}/values.json", f"October intake/{s}/values.csv"} <= names
        rows = list(csv.DictReader(io.StringIO(z.read("October intake/summary.csv").decode("utf-8-sig"))))
        assert [r["set"] for r in rows] == ["alice", "bob"]
        assert rows[0]["1. Full name"] == "Priya Natarajan" and rows[0]["status"] == "done"

        assert client.delete(f"/api/batches/{b['id']}").status_code == 200
        assert client.get(f"/api/batches/{b['id']}").status_code == 404
        assert db.q1("SELECT COUNT(*) AS n FROM documents")["n"] == 0


def test_batch_api_count_only_still_works(app_env):
    with TestClient(create_app()) as client:
        with open(SET / "form.pdf", "rb") as fh:
            form = client.post("/api/forms", files={"file": ("form.pdf", fh, "application/pdf")}).json()
        b = client.post("/api/batches", json={"form_id": form["id"], "count": 2}).json()
        assert b["batch_id"] == b["id"] and [j["label"] for j in b["jobs"]] == ["set 1", "set 2"]


def test_batch_cli_folders_in_folders_out(app_env, tmp_path, capsys):
    app_env.responder = responder
    sets = tmp_path / "clients"
    for name in ("alice", "bob"):
        (sets / name / "older").mkdir(parents=True)
    for p in (SET / "sources").iterdir():
        shutil.copy(p, sets / "alice" / p.name)
    shutil.copy(SET / "sources" / "employment_letter.pdf", sets / "bob" / "older" / "letter.pdf")
    shutil.copy(SET / "sources" / "bank_statement.pdf", sets / "stray.pdf")
    (sets / "alice" / ".DS_Store").write_bytes(b"x")
    out = tmp_path / "out"

    code = cli_main(["batch", "--form", str(SET / "form.pdf"), "--sets", str(sets), "--out", str(out)])
    assert code == 0
    err = capsys.readouterr().err
    assert "skipped stray.pdf: not inside a sub-folder" in err
    assert sorted(p.name for p in out.iterdir()) == ["alice", "bob", "summary.csv"]
    assert (out / "alice" / "form_filled.pdf").exists() and (out / "bob" / "values.json").exists()
    rows = list(csv.DictReader((out / "summary.csv").open(encoding="utf-8-sig")))
    assert [r["set"] for r in rows] == ["alice", "bob"]
    docs = {r["filename"] for r in db.q("SELECT filename FROM documents")}
    assert "older/letter.pdf" in docs  # nested folder kept in the document name
