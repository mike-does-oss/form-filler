# Local Form Filler

Fills forms from source documents, entirely on your own machine. OCR, text extraction,
embeddings and the LLM all run locally; no document content leaves the machine.

1. Upload a blank form: fillable PDF (AcroForm), Word form (.docx) or flat PDF.
2. Upload source documents: PDF, DOCX, images, scans, text.
3. The app proposes a value for every field with a source quote, page reference and confidence.
4. Review side by side with the highlighted source; accept, edit or pick between conflicting values.
5. Export the filled form (original format), a flattened PDF, JSON (values and evidence) or CSV.

Every correction is saved against the form's template and used as a few-shot example next
time the same form comes in.

## Quick start (uv + Ollama, macOS or Linux)

```bash
# 1. models (one time)
ollama pull qwen2.5:14b-instruct      # extraction + judgment (config: llm.model)
ollama pull bge-m3                    # embeddings
brew install tesseract                # or: apt install tesseract-ocr  (CPU OCR fallback)

# 2. app
uv sync                               # add `--extra docling` for the Docling OCR/layout engine
cd frontend && npm install && npm run build && cd ..
uv run form-filler health             # checks model server, models and OCR
uv run form-filler serve              # http://127.0.0.1:8080
```

To use a model you already have, override the config without editing it:
`FF__LLM__MODEL=gemma4:12b uv run form-filler serve`, or point `FORMFILLER_CONFIG` at another
YAML file (see `config.dev.yaml`).

### Docker (NVIDIA GPU)

```bash
docker compose up -d --build
docker compose --profile setup run --rm pull-models    # pulls LLM, embedding and vision models once
```

The app is published on `127.0.0.1:8080` only. Docling's model weights are downloaded at image
build time and the container runs with `HF_HUB_OFFLINE=1`, so there are no outbound calls at runtime.
On Apple Silicon, run Ollama natively (Metal) and the app with `uv` instead.

## Configuration

`config.yaml` holds the defaults from the spec. Any value can be overridden with an
environment variable `FF__SECTION__KEY`, e.g. `FF__LLM__BACKEND=vllm`,
`FF__LLM__BASE_URL=http://gpu-box:8000`, `FF__OCR__ENGINE=tesseract`, `FF__JUDGMENT__MODEL=qwen2.5:32b-instruct`.

| Key | Default | Notes |
|---|---|---|
| `llm.backend` | `ollama` | `ollama` or `vllm` (OpenAI-compatible server with guided JSON) |
| `llm.model` | `qwen2.5:14b-instruct` | extraction model |
| `llm.context_budget_tokens` | 24000 | sources that fit are passed whole; otherwise hybrid retrieval |
| `llm.trace` | false | append every LLM call to `data/llm_trace.jsonl` for debugging |
| `judgment.model` | null | null = `llm.model`; set a larger model for judgment fields only |
| `judgment.self_consistency_runs` | 5 | rubric decisions are majority votes at `judgment.temperature` |
| `ocr.engine` | `docling` | `docling`, `paddleocr`, `tesseract`; falls back to tesseract if the engine isn't installed (shown in `/api/health`) |
| `vision.enabled` | false | send low-confidence OCR pages to `vision.model` for transcription |
| `verification.fuzzy_threshold` | 90 | quote must match its chunk at this `partial_ratio` |
| `server.host` | `127.0.0.1` | LAN binding is an explicit change (there is no auth) |
| `server.worker_mode` | `process` | job worker pool: `process` or `thread` |

## How it works

```
sources ─> normalise to PDF ─> text layer or OCR ─> layout-block chunks (page + bbox) ─> embeddings + BM25
blank form ─> parser (AcroForm / Word / flat PDF) ─> field schema (labels, sections, instructions, kinds)
   └─> fingerprint ─> saved template? reuse schema, rules and past corrections
fields batched by section ─> hybrid retrieval (BM25 + vectors, RRF) or full context
   ─> constrained JSON extraction (value, exact quotes, candidates, derivation inputs)
   ─> quote verification (rapidfuzz) ─> value-in-quote check ─> validators ─> status + confidence
judgment fields ─> hidden fact fields (same pipeline) ─> code rule or rubric x N votes
review UI ─> accept / edit / pick candidate / answer missing facts ─> audit log + corrections
write-back ─> filled form, flattened PDF, JSON, CSV
```

- **Every source becomes a PDF first** (DOCX and text are laid out with PyMuPDF's HTML engine,
  images are wrapped), so page images, bboxes and highlights work the same for all inputs.
  Pages with a text layer use it; pages without one are OCR'd.
- **The model never does arithmetic.** For totals and ages it returns the inputs with their own
  quotes and an operation; the backend computes the result and checks every input quote.
- **Statuses:** `verified`, `derived`, `conflict`, `not_found`, `unverified`, plus `judged` /
  `cannot_determine` for judgment fields. Confidence is `high` only for verified (or derived)
  values that pass every validator. Judgment fields are never `high` and never auto-accepted.
- **Judgment fields** (eligibility, "does X apply to you"): when a template is first seen, the
  LLM proposes the facts each decision depends on and a rubric drafted from the form's own notes.
  Facts are extracted with evidence like any field. Missing facts give `cannot_determine` and
  are shown to the reviewer as direct questions; answering re-runs the decision. Rules can be
  switched to deterministic code rules (`backend/app/judgment/rules.py`) in the template editor.
  Rules that reviewers keep contradicting are flagged for review.
- **Form instructions** (notes beside fields, footnotes, "Notes to question 7" pages) are linked
  to their fields and passed to the model.

## API

See `backend/app/api/routes.py` or http://127.0.0.1:8080/docs. Main endpoints:

```
POST   /api/forms                         upload blank form -> parse -> field schema
PUT    /api/forms/{id}/fields             edit labels/types/kinds; full mapping for flat PDFs (replace=true)
POST   /api/jobs                          {form_id}
POST   /api/jobs/{id}/documents           upload source documents
POST   /api/jobs/{id}/run                 queue ingest + extraction (progress: GET /api/jobs/{id}/events, SSE)
GET    /api/jobs/{id}/values              values with evidence, candidates, facts, reasoning
PATCH  /api/jobs/{id}/values/{fid}        accept | edit | choose_candidate | not_applicable | reset
POST   /api/jobs/{id}/values/{fid}/facts  answer missing facts for a cannot_determine field
POST   /api/jobs/{id}/export              {format: filled | flattened | json | csv}
DELETE /api/jobs/{id}                     removes files, chunks, embeddings, values and audit entries
GET    /api/templates[/{id}]              templates, judgment rules, corrections
PUT    /api/templates/{id}/rules/{key}    edit facts / rule type / rubric / code rule
POST   /api/batches, /api/batches/{id}/run, GET /api/batches/{id}    batch mode with throughput
GET    /api/health
```

CLI: `form-filler serve | health | delete-job <id> | batch --form F --sets DIR --out DIR`.

## Evaluation

`eval/fixtures/` holds 7 synthetic sets (no real personal data), built by
`eval/fixtures/build_fixtures.py`: fillable PDF with a derived total and a judgment question,
Word form (content controls, label/value table, blank lines, checkbox glyphs), scanned sources,
conflicting sources, mostly-absent fields, judgment fields including a `cannot_determine` case,
and a flat PDF.

```bash
uv run python eval/run_eval.py                          # all sets with the configured models
uv run python eval/run_eval.py --model qwen2.5:7b-instruct --ocr tesseract --label qwen7b
uv run python eval/run_eval.py --with-corrections      # pass 2 re-runs with reviewer corrections
uv run python eval/run_eval.py --compare               # Markdown table across saved runs
```

Reports go to `eval/results/` and include field accuracy, not-found precision, **false-fill rate**,
evidence verification rate, judgment accuracy, `cannot_determine` rate, missing-facts precision and recall,
accuracy after answering the questions, calibration by run agreement, and latency.

## Tests

```bash
uv run pytest            # uses a scripted fake LLM; needs tesseract for the OCR test
```

## Layout

```
backend/app/
  api/          FastAPI routes
  ingest/       normalise-to-PDF, text layer, OCR engines, chunking, embeddings + BM25
  forms/        acroform.py, docx_form.py, flat_pdf.py, fingerprint.py, service.py (templates)
  extraction/   llm_adapter.py (Ollama, vLLM), retrieval.py, prompts.py, schema_builder.py, extract.py
  verify/       evidence.py, validators.py, normalize.py, confidence.py
  judgment/     rules.py (code rules), setup.py (fact/rubric proposals), runtime.py
  writeback/    acroform.py, docx_form.py, flat_pdf.py, export.py
  jobs/         queue.py, worker.py, pipeline.py, service.py (review, corrections, audit)
  db/           SQLite access, migrations, domain models
frontend/       React + Vite + TypeScript review UI (built to frontend/dist, served by FastAPI)
eval/           fixtures + run_eval.py
```

## Notes and limits

- Flattened PDF export of **Word** forms needs LibreOffice (`soffice`) installed locally; Word
  forms otherwise export as .docx with formatting preserved.
- The document viewer uses server-rendered page images with bbox overlays rather than pdf.js,
  so DOCX, image and PDF sources all display the same way.
- Out of scope for v1: web forms, handwriting beyond what the vision model handles, digital
  signatures (signature fields are listed as "requires manual signing"), multi-user auth.
