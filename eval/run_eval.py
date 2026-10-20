"""Evaluation harness.

Runs every fixture set end to end (parse form -> ingest sources -> extract -> verify ->
judge) against the configured local models and reports:

  - field accuracy (exact after normalisation), lookup fields
  - not-found precision (fields left empty that should be empty)
  - false-fill rate (value filled when gold is empty)  <- the most important metric
  - evidence verification rate
  - judgment accuracy (separately), cannot_determine rate, missing-facts precision/recall,
    accuracy after answering the missing-fact questions
  - calibration: judgment accuracy when run_agreement = 1.0 vs < 1.0
  - latency per form and OCR time per page

Usage:
  uv run python eval/run_eval.py                       # all sets, config from FORMFILLER_CONFIG / config.yaml
  uv run python eval/run_eval.py --model gemma4:12b --ocr tesseract --label gemma-tess
  uv run python eval/run_eval.py --with-corrections    # pass 2 re-runs with reviewer corrections as few-shot
  uv run python eval/run_eval.py --compare             # table comparing all saved results
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import time
import warnings
from decimal import Decimal
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
FIXTURES = Path(__file__).parent / "fixtures"
RESULTS = Path(__file__).parent / "results"


# ------------------------------------------------------------------ matching + normalisation
def _strip_qnum(s: str) -> str:
    return re.sub(r"^\s*(q(uestion)?\s*)?\d{1,3}[a-z]?[.):]\s*", "", s, flags=re.I).strip()


def match_fields(gold: list[dict], fields: list[dict]) -> dict[int, dict]:
    """Greedy best-label matching of gold entries to parsed fields."""
    from rapidfuzz import fuzz

    pairs = []
    for gi, g in enumerate(gold):
        gl = _strip_qnum(g["label"]).lower()
        for f in fields:
            names = {f["label"], f.get("meta", {}).get("label_parsed") or "", f.get("meta", {}).get("label_raw") or "", f["key"].replace("_", " ")}
            score = max(
                max(fuzz.ratio(gl, _strip_qnum(n).lower()), fuzz.token_set_ratio(gl, _strip_qnum(n).lower()) - 3)
                for n in names if n
            )
            pairs.append((score, gi, f["key"]))
    pairs.sort(reverse=True)
    used_g, used_f, out = set(), set(), {}
    by_key = {f["key"]: f for f in fields}
    for score, gi, key in pairs:
        if score < 70 or gi in used_g or key in used_f:
            continue
        used_g.add(gi)
        used_f.add(key)
        out[gi] = by_key[key]
    return out


def canon(ftype: str, v: Any) -> Any:
    from app.verify.normalize import parse_date, parse_decimal

    if ftype == "checkbox":
        if v in (None, "", False, "false", "False", "no", "No"):
            return False
        return True
    if v is None or v == "":
        return None
    if ftype == "date":
        d = parse_date(str(v))
        return d.isoformat() if d else str(v)
    if ftype in ("currency", "number"):
        d = parse_decimal(v)
        return format(d.normalize(), "f") if isinstance(d, Decimal) else str(v)
    if ftype == "phone":
        return re.sub(r"\D", "", str(v))
    return re.sub(r"[^a-z0-9@.]+", " ", str(v).lower()).strip()


def is_empty(ftype: str, v: Any) -> bool:
    return canon(ftype, v) in (None, False) if ftype == "checkbox" else canon(ftype, v) is None


# ------------------------------------------------------------------ one set
def run_set(set_dir: Path, *, corrections_from: dict | None = None) -> dict:
    from app import db
    from app.forms.service import create_form
    from app.ingest.normalize import guess_mime
    from app.jobs.pipeline import run_job
    from app.jobs.service import apply_review, job_dir, list_values

    gold = json.loads((set_dir / "gold.json").read_text())
    form_path = set_dir / gold["form"]
    t0 = time.monotonic()
    form_id = create_form(form_path, form_path.name)
    t_form = time.monotonic() - t0
    job_id = db.insert("jobs", {"form_id": form_id, "status": "created"})
    for f in sorted((set_dir / "sources").iterdir()):
        doc_id = db.insert("documents", {"job_id": job_id, "filename": f.name, "stored_path": "", "mime": guess_mime(f.name)})
        dest = job_dir(job_id) / "docs" / f"{doc_id}{f.suffix.lower()}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(f, dest)
        db.ex("UPDATE documents SET stored_path = ? WHERE id = ?", (str(dest), doc_id))
    stats = run_job(job_id)
    values = list_values(job_id)
    form_fields = db.q("SELECT key, label, meta_json FROM fields WHERE form_id = ?", (form_id,))
    meta = {r["key"]: json.loads(r["meta_json"] or "{}") for r in form_fields}
    for v in values:
        v["meta"] = meta.get(v["key"], {})
    matches = match_fields(gold["fields"], values)

    rows = []
    for gi, g in enumerate(gold["fields"]):
        if g.get("skip_eval"):
            continue
        v = matches.get(gi)
        row: dict[str, Any] = {"label": g["label"], "matched": v is not None}
        if v is None:
            row.update(correct=False, kind=g.get("kind", "lookup"), note="no matching field parsed")
            rows.append(row)
            continue
        ftype = v["type"]
        kind = "judgment" if (g.get("kind") == "judgment" or v["kind"] == "judgment") else "lookup"
        pred = v["value"]
        row.update(key=v["key"], kind=kind, parsed_kind=v["kind"], type=ftype, pred=pred, status=v["status"],
                   confidence=v["confidence"], run_agreement=v["run_agreement"], field_id=v["field_id"])
        if "conflict" in g:
            from rapidfuzz import fuzz

            cand_vals = [canon(ftype, c["value"]) for c in v["candidates"]] + ([canon(ftype, pred)] if pred else [])
            covered = all(any(c and fuzz.ratio(canon(ftype, gv), c) >= 90 for c in cand_vals) for gv in g["conflict"])
            row.update(gold={"conflict": g["conflict"]}, correct=v["status"] == "conflict" and covered, gold_empty=False)
        elif g.get("value") == "cannot_determine":
            row.update(gold="cannot_determine", gold_empty=False, correct=v["status"] == "cannot_determine")
            row["missing_facts"] = _missing_fact_scores(g, v)
            if g.get("user_answers"):
                row["after_answers"] = _answer_and_score(job_id, g, v, ftype)
        else:
            gv = g.get("value")
            gold_empty = is_empty(ftype, gv)
            pred_empty = is_empty(ftype, pred)
            row.update(gold=gv, gold_empty=gold_empty, pred_empty=pred_empty)
            if gold_empty:
                row["correct"] = pred_empty
            else:
                row["correct"] = (not pred_empty) and canon(ftype, gv) == canon(ftype, pred)
        rows.append(row)

    if corrections_from is not None:
        # simulate a reviewer fixing every wrong field (used by --with-corrections)
        for r in rows:
            if r.get("matched") and not r["correct"] and r.get("gold") not in (None, "cannot_determine") and "conflict" not in str(r.get("gold")):
                try:
                    apply_review(job_id, r["field_id"], "edit", r["gold"], note="eval: corrected to gold")
                except Exception as e:  # noqa: BLE001
                    print(f"  could not apply correction for {r['label']}: {e}")
    return {
        "set": set_dir.name,
        "job_id": job_id,
        "rows": rows,
        "stats": stats,
        "form_parse_s": round(t_form, 2),
        "parsed_fields": len(values),
    }


def _missing_fact_scores(g: dict, v: dict) -> dict:
    from rapidfuzz import fuzz

    predicted = [" ".join(str(m.get(k) or "") for k in ("fact_key", "label", "question")).replace("_", " ").lower() for m in v["missing_facts"]]
    gold_items = [x.lower() for x in g.get("missing_facts", [])]
    hit_gold = [any(fuzz.token_set_ratio(gm, p) >= 55 or fuzz.partial_ratio(gm, p) >= 70 for p in predicted) for gm in gold_items]
    hit_pred = [any(fuzz.token_set_ratio(gm, p) >= 55 or fuzz.partial_ratio(gm, p) >= 70 for gm in gold_items) for p in predicted]
    return {
        "predicted": [m.get("label") for m in v["missing_facts"]],
        "questions": [m.get("question") for m in v["missing_facts"]],
        "recall": sum(hit_gold) / len(gold_items) if gold_items else None,
        "precision": sum(hit_pred) / len(predicted) if predicted else None,
    }


def _answer_and_score(job_id: int, g: dict, v: dict, ftype: str) -> dict:
    from app.jobs.service import answer_facts

    answers = {}
    for m in v["missing_facts"]:
        text = " ".join(str(m.get(k) or "") for k in ("fact_key", "label", "question")).replace("_", " ").lower()
        for ua in g["user_answers"]:
            if any(kw.lower() in text for kw in ua["match"]):
                answers[m["fact_key"]] = ua["value"]
                break
    if not answers:
        return {"answered": {}, "correct": False, "note": "no gold answer matched the missing-fact questions"}
    out = answer_facts(job_id, v["field_id"], answers)
    correct = canon(ftype, out["value"]) == canon(ftype, g["value_after_answers"])
    return {"answered": answers, "value": out["value"], "status": out["status"], "correct": correct}


# ------------------------------------------------------------------ metrics + report
def metrics(results: list[dict]) -> dict:
    rows = [r for res in results for r in res["rows"]]
    lookup = [r for r in rows if r.get("kind") == "lookup"]
    judg = [r for r in rows if r.get("kind") == "judgment"]

    def frac(n, d):
        return round(n / d, 3) if d else None

    gold_empty = [r for r in lookup if r.get("gold_empty")]
    pred_empty = [r for r in lookup if r.get("pred_empty")]
    filled = [r for r in lookup if r.get("matched") and not r.get("pred_empty") and r.get("status") not in ("not_found",)]
    cd = [r for r in judg if r.get("status") == "cannot_determine"]
    gold_cd = [r for r in judg if r.get("gold") == "cannot_determine"]
    mf = [r["missing_facts"] for r in judg if r.get("missing_facts")]
    after = [r["after_answers"] for r in judg if r.get("after_answers")]
    agree1 = [r for r in judg if r.get("run_agreement") is not None and r["run_agreement"] >= 0.999]
    agree_lt = [r for r in judg if r.get("run_agreement") is not None and r["run_agreement"] < 0.999]
    lat = [res["stats"].get("total_s") for res in results if res["stats"].get("total_s") is not None]
    ocr = [res["stats"].get("ocr_s_per_page") for res in results if res["stats"].get("ocr_s_per_page")]
    return {
        "lookup_fields": len(lookup),
        "field_accuracy": frac(sum(r["correct"] for r in lookup), len(lookup)),
        "not_found_precision": frac(sum(1 for r in pred_empty if r.get("gold_empty")), len(pred_empty)),
        "false_fill_rate": frac(sum(1 for r in gold_empty if not r.get("pred_empty")), len(gold_empty)),
        "evidence_verification_rate": frac(sum(1 for r in filled if r.get("status") in ("verified", "derived")), len(filled)),
        "judgment_fields": len(judg),
        "judgment_accuracy": frac(sum(r["correct"] for r in judg), len(judg)),
        "cannot_determine_rate": frac(len(cd), len(judg)),
        "cannot_determine_correct": frac(sum(1 for r in gold_cd if r.get("status") == "cannot_determine"), len(gold_cd)),
        "missing_facts_recall": frac(sum(m["recall"] or 0 for m in mf), len(mf)),
        "missing_facts_precision": frac(sum(m["precision"] or 0 for m in mf), len(mf)),
        "accuracy_after_answers": frac(sum(a["correct"] for a in after), len(after)),
        "judgment_acc_agreement_1": frac(sum(r["correct"] for r in agree1), len(agree1)),
        "judgment_n_agreement_1": len(agree1),
        "judgment_acc_agreement_lt1": frac(sum(r["correct"] for r in agree_lt), len(agree_lt)),
        "judgment_n_agreement_lt1": len(agree_lt),
        "latency_per_form_s": round(sum(lat) / len(lat), 1) if lat else None,
        "ocr_s_per_page": round(sum(ocr) / len(ocr), 2) if ocr else None,
    }


METRIC_LABELS = [
    ("field_accuracy", "Field accuracy (lookup)"),
    ("false_fill_rate", "False-fill rate (lower is better)"),
    ("not_found_precision", "Not-found precision"),
    ("evidence_verification_rate", "Evidence verification rate"),
    ("judgment_accuracy", "Judgment accuracy"),
    ("cannot_determine_rate", "Judgment cannot_determine rate"),
    ("cannot_determine_correct", "cannot_determine when gold is cannot_determine"),
    ("missing_facts_recall", "Missing-facts recall"),
    ("missing_facts_precision", "Missing-facts precision"),
    ("accuracy_after_answers", "Judgment accuracy after answering questions"),
    ("judgment_acc_agreement_1", "Judgment accuracy, run_agreement = 1.0"),
    ("judgment_acc_agreement_lt1", "Judgment accuracy, run_agreement < 1.0"),
    ("latency_per_form_s", "Latency per form (s)"),
    ("ocr_s_per_page", "OCR time per page (s)"),
]


def fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float) and v <= 1.0 and not isinstance(v, bool):
        return f"{v:.1%}"
    return str(v)


def report(run: dict) -> str:
    lines = [f"# Eval: {run['label']}", "", f"- date: {run['date']}", f"- model: `{run['model']}`, judgment model: `{run['judgment_model']}`",
             f"- embeddings: `{run['embeddings']}`, OCR: `{run['ocr']}`, backend: `{run['backend']}`", ""]
    passes = run["passes"]
    header = "| Metric | " + " | ".join(p["name"] for p in passes) + " |"
    lines += [header, "|---|" + "---|" * len(passes)]
    for k, lab in METRIC_LABELS:
        lines.append(f"| {lab} | " + " | ".join(fmt(p["metrics"].get(k)) for p in passes) + " |")
    m = passes[0]["metrics"]
    lines += ["", f"Counts: {m['lookup_fields']} lookup fields, {m['judgment_fields']} judgment fields "
              f"({m['judgment_n_agreement_1']} with full agreement, {m['judgment_n_agreement_lt1']} below).", ""]
    for p in passes:
        lines += [f"## Per-field results: {p['name']}", ""]
        for res in p["results"]:
            ok = sum(r["correct"] for r in res["rows"])
            lines.append(f"### {res['set']}  ({ok}/{len(res['rows'])} correct, {res['stats'].get('total_s')}s)")
            lines.append("")
            lines.append("| Field | Kind | Gold | Predicted | Status | Conf | OK |")
            lines.append("|---|---|---|---|---|---|---|")
            for r in res["rows"]:
                extra = ""
                if r.get("missing_facts"):
                    mf = r["missing_facts"]
                    extra = f" missing={mf['predicted']}"
                if r.get("after_answers"):
                    extra += f" after_answers={r['after_answers'].get('value')!r}"
                if r.get("run_agreement") is not None:
                    extra += f" agree={r['run_agreement']}"
                gold = r.get("gold")
                lines.append(
                    f"| {r['label'][:48]} | {r.get('kind', '')} | {str(gold)[:40]} | {str(r.get('pred'))[:40]}{extra[:120]} | "
                    f"{r.get('status', '')} | {r.get('confidence', '')} | {'✅' if r['correct'] else '❌'} |"
                )
            lines.append("")
    return "\n".join(lines)


def compare() -> str:
    runs = [json.loads(p.read_text()) for p in sorted(RESULTS.glob("*.json"))]
    if not runs:
        return "no saved results"
    cols = []
    for r in runs:
        for p in r["passes"]:
            cols.append((f"{r['label']}<br>{r['model']}<br>OCR {r['ocr']}" + (f"<br>{p['name']}" if len(r['passes']) > 1 else ""), p["metrics"]))
    lines = ["| Metric | " + " | ".join(c[0] for c in cols) + " |", "|---|" + "---|" * len(cols)]
    for k, lab in METRIC_LABELS:
        lines.append(f"| {lab} | " + " | ".join(fmt(c[1].get(k)) for c in cols) + " |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sets", nargs="*", help="set directory names (default: all)")
    ap.add_argument("--model", help="override llm.model")
    ap.add_argument("--judgment-model", help="override judgment.model")
    ap.add_argument("--ocr", choices=["docling", "paddleocr", "tesseract"], help="override ocr.engine")
    ap.add_argument("--backend", choices=["ollama", "vllm"])
    ap.add_argument("--base-url")
    ap.add_argument("--runs", type=int, help="override judgment.self_consistency_runs")
    ap.add_argument("--label", default=None)
    ap.add_argument("--with-corrections", action="store_true", help="pass 2: re-run with reviewer corrections as few-shot")
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--keep-data", action="store_true")
    args = ap.parse_args()
    if args.compare:
        print(compare())
        return 0

    data_dir = Path(tempfile.mkdtemp(prefix="ff_eval_"))
    os.environ["FF__STORAGE__DATA_DIR"] = str(data_dir)
    if args.model:
        os.environ["FF__LLM__MODEL"] = args.model
    if args.judgment_model:
        os.environ["FF__JUDGMENT__MODEL"] = args.judgment_model
    if args.ocr:
        os.environ["FF__OCR__ENGINE"] = args.ocr
    if args.backend:
        os.environ["FF__LLM__BACKEND"] = args.backend
    if args.base_url:
        os.environ["FF__LLM__BASE_URL"] = args.base_url
    if args.runs:
        os.environ["FF__JUDGMENT__SELF_CONSISTENCY_RUNS"] = str(args.runs)

    from app.config import get_config
    from app.ingest.ocr import resolve_engine

    cfg = get_config()
    sets = sorted(p for p in FIXTURES.iterdir() if p.is_dir() and (p / "gold.json").exists())
    if args.sets:
        sets = [s for s in sets if s.name in args.sets or any(s.name.startswith(x) for x in args.sets)]
    label = args.label or f"{cfg.llm.model}-{resolve_engine()}"
    run: dict[str, Any] = {
        "label": label,
        "date": dt.datetime.now().isoformat(timespec="seconds"),
        "model": cfg.llm.model,
        "judgment_model": cfg.judgment_model,
        "embeddings": cfg.embeddings.model,
        "ocr": resolve_engine(),
        "backend": cfg.llm.backend,
        "passes": [],
    }
    passes = [("pass 1", None)] + ([("pass 2 (with corrections)", None)] if args.with_corrections else [])
    for pi, (name, _) in enumerate(passes):
        results = []
        for s in sets:
            print(f"[{name}] {s.name} ...", flush=True)
            try:
                res = run_set(s, corrections_from={} if (args.with_corrections and pi == 0) else None)
            except Exception as e:  # noqa: BLE001
                import traceback

                traceback.print_exc()
                res = {"set": s.name, "rows": [], "stats": {}, "error": str(e)}
            ok = sum(r["correct"] for r in res["rows"])
            print(f"   {ok}/{len(res['rows'])} correct in {res['stats'].get('total_s')}s", flush=True)
            results.append(res)
        run["passes"].append({"name": name, "metrics": metrics(results), "results": results})

    RESULTS.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
    md = report(run)
    (RESULTS / f"{stamp}_{safe}.md").write_text(md)
    (RESULTS / f"{stamp}_{safe}.json").write_text(json.dumps(run, indent=1, default=str))
    print()
    print("\n".join(md.split("\n## Per-field")[0].splitlines()))
    print(f"\nFull report: {RESULTS / f'{stamp}_{safe}.md'}")
    if not args.keep_data:
        shutil.rmtree(data_dir, ignore_errors=True)
    else:
        print(f"data kept in {data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
