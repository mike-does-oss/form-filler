import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, errMsg, type Bbox, type FieldValue, type Form, type Job, type ReviewAction } from "../api";
import { FieldRow, type Jumpable, type RowCtx } from "../components/FieldRow";
import { JobRunPanel, ProgressBar } from "../components/JobProgress";
import { PageStack, type Overlay, type ScrollTarget } from "../components/PageStack";
import { ExportMenu } from "../components/ExportMenu";
import { toast } from "../components/Toast";
import { isTypingTarget } from "../format";
import { JobStatusChip } from "./UploadPage";

type Filter = "all" | "low" | "required" | "conflict" | "judgment" | "pending";
const FILTERS: { id: Filter; label: string; title: string }[] = [
  { id: "all", label: "All", title: "Judgment fields first, then by section" },
  { id: "low", label: "Low confidence first", title: "Sorted low → high confidence" },
  { id: "required", label: "Required", title: "Required fields only" },
  { id: "conflict", label: "Conflicts", title: "Sources disagree" },
  { id: "judgment", label: "Judgment", title: "Fields decided by rules" },
  { id: "pending", label: "Not reviewed", title: "Still to review" },
];
const CONF_RANK: Record<string, number> = { low: 0, medium: 1, high: 2 };

interface Highlight { docId: number; page: number; boxes: Bbox[] }

export function ReviewPage({ jobId }: { jobId: number }) {
  const [job, setJob] = useState<Job | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [values, setValues] = useState<FieldValue[] | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [selId, setSelId] = useState<number | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"sources" | "preview">("sources");
  const [docId, setDocId] = useState<number | null>(null);
  const [hl, setHl] = useState<Highlight | null>(null);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [target, setTarget] = useState<ScrollTarget | null>(null);
  const [previewTarget, setPreviewTarget] = useState<ScrollTarget | null>(null);
  const [previewVer, setPreviewVer] = useState(1);
  const [showKeys, setShowKeys] = useState(false);
  const listRef = useRef<HTMLUListElement>(null);
  const nonce = useRef(0);
  const selRef = useRef<number | null>(null);
  selRef.current = selId;

  const loadJob = useCallback(async () => {
    try {
      const j = await api.getJob(jobId);
      setJob(j);
      setDocId((d) => d ?? j.documents[0]?.id ?? null);
      if (!form || form.id !== j.form_id) api.getForm(j.form_id).then(setForm).catch(() => {});
      if (j.status === "done") {
        const v = await api.values(jobId);
        setValues(v.fields);
      }
    } catch (e) {
      setLoadErr(errMsg(e));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);
  useEffect(() => { loadJob(); }, [loadJob]);

  // ---------- ordering + filtering
  const visible = useMemo(() => {
    if (!values) return [];
    let xs = [...values];
    const byOrder = (a: FieldValue, b: FieldValue) =>
      (a.kind === "judgment" ? 0 : 1) - (b.kind === "judgment" ? 0 : 1) || a.order_index - b.order_index;
    switch (filter) {
      case "low":
        xs = xs.filter((f) => !f.requires_manual_signing);
        xs.sort((a, b) =>
          (a.review_status === "pending" ? 0 : 1) - (b.review_status === "pending" ? 0 : 1) ||
          (CONF_RANK[a.confidence ?? "low"] ?? 0) - (CONF_RANK[b.confidence ?? "low"] ?? 0) || byOrder(a, b));
        return xs;
      case "required": xs = xs.filter((f) => f.required); break;
      case "conflict": xs = xs.filter((f) => f.status === "conflict" || f.candidates.length > 1); break;
      case "judgment": xs = xs.filter((f) => f.kind === "judgment"); break;
      case "pending": xs = xs.filter((f) => f.review_status === "pending" && !f.requires_manual_signing); break;
    }
    return xs.sort(byOrder);
  }, [values, filter]);

  const counts = useMemo(() => {
    const v = values ?? [];
    return {
      all: v.length,
      low: v.filter((f) => f.confidence === "low" && !f.requires_manual_signing).length,
      required: v.filter((f) => f.required).length,
      conflict: v.filter((f) => f.status === "conflict" || f.candidates.length > 1).length,
      judgment: v.filter((f) => f.kind === "judgment").length,
      pending: v.filter((f) => f.review_status === "pending" && !f.requires_manual_signing).length,
    } as Record<Filter, number>;
  }, [values]);

  const reviewable = (values ?? []).filter((f) => !f.requires_manual_signing);
  const reviewed = reviewable.filter((f) => f.review_status !== "pending").length;
  const selected = values?.find((f) => f.field_id === selId) ?? null;

  // ---------- viewer jumps
  const docName = useCallback(
    (id: number | null | undefined) => job?.documents.find((d) => d.id === id)?.filename ?? (id != null ? `doc ${id}` : ""),
    [job],
  );
  const jump = useCallback((ev: Jumpable, key: string) => {
    if (ev.document_id == null || ev.page == null) return;
    const boxes = ev.boxes && ev.boxes.length ? ev.boxes : ev.bbox ? [ev.bbox] : [];
    setTab("sources");
    setDocId(ev.document_id);
    setHl({ docId: ev.document_id, page: ev.page, boxes });
    setActiveKey(key);
    setTarget({ page: ev.page, box: boxes[0] ?? null, nonce: ++nonce.current });
  }, []);

  const firstEvidence = (f: FieldValue): [Jumpable, string] | null => {
    const e = f.evidence.findIndex((x) => x.document_id != null && x.page != null);
    if (e >= 0) return [f.evidence[e], `e${f.field_id}-${e}`];
    for (let i = 0; i < f.candidates.length; i++) {
      const j = f.candidates[i].evidence.findIndex((x) => x.document_id != null && x.page != null);
      if (j >= 0) return [f.candidates[i].evidence[j], `c${f.field_id}-${i}-${j}`];
    }
    const d = f.derivation?.inputs.findIndex((x) => x.document_id != null && x.page != null) ?? -1;
    if (d >= 0) return [f.derivation!.inputs[d], `d${f.field_id}-${d}`];
    for (const fu of f.facts_used) {
      const j = fu.evidence.findIndex((x) => x.document_id != null && x.page != null);
      if (j >= 0) return [fu.evidence[j], `f${f.field_id}-${fu.fact_key}-${j}`];
    }
    return null;
  };

  const select = useCallback((f: FieldValue | null, opts: { scroll?: boolean } = {}) => {
    setEditing(null);
    if (!f) { setSelId(null); return; }
    setSelId(f.field_id);
    const fe = firstEvidence(f);
    if (fe) {
      const [ev, key] = fe;
      const boxes = ev.boxes && ev.boxes.length ? ev.boxes : ev.bbox ? [ev.bbox] : [];
      setDocId(ev.document_id!);
      setHl({ docId: ev.document_id!, page: ev.page!, boxes });
      setActiveKey(key);
      setTarget({ page: ev.page!, box: boxes[0] ?? null, nonce: ++nonce.current });
    } else {
      setHl(null);
      setActiveKey(null);
    }
    if (f.page && f.bbox) setPreviewTarget({ page: f.page, box: f.bbox, nonce: ++nonce.current });
    if (opts.scroll !== false) {
      requestAnimationFrame(() => {
        listRef.current?.querySelector(`[data-field-id="${f.field_id}"]`)?.scrollIntoView({ block: "nearest" });
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onRowSelect = useCallback((f: FieldValue) => select(f, { scroll: false }), [select]);

  const move = useCallback((d: number) => {
    if (!visible.length) return;
    const i = visible.findIndex((f) => f.field_id === selId);
    const n = i < 0 ? (d > 0 ? 0 : visible.length - 1) : Math.max(0, Math.min(visible.length - 1, i + d));
    select(visible[n]);
  }, [visible, selId, select]);

  // ---------- actions
  const replaceValue = (nv: FieldValue) => {
    setValues((vs) => vs?.map((x) => (x.field_id === nv.field_id ? nv : x)) ?? vs);
    setPreviewVer((v) => v + 1);
  };

  const act = useCallback(async (f: FieldValue, action: ReviewAction, extra: { value?: unknown; candidate_index?: number; note?: string } = {}) => {
    if (f.requires_manual_signing) return;
    // Advance to the next field after a decisive action (based on the list before the change).
    const i = visible.findIndex((x) => x.field_id === f.field_id);
    const next = action === "reset" ? null : visible[i + 1] ?? null;
    // "Accept" means "accept the model value": never let it silently overwrite a reviewer's edit.
    if (action === "accept" && f.review_status === "edited") {
      if (next) select(next);
      else toast("Field already edited — use Reset to go back to the model value", "info");
      return;
    }
    setBusy(true);
    try {
      const nv = await api.review(jobId, f.field_id, { action, ...extra });
      replaceValue(nv);
      // Only auto-advance if the reviewer hasn't moved on while the request was in flight.
      if (selRef.current === f.field_id) {
        setEditing(null);
        if (next) select(next);
      }
    } catch (e) {
      toast(`${action.replace("_", " ")} failed: ${errMsg(e)}`, "error");
    } finally {
      setBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, visible, select]);

  const answer = useCallback(async (f: FieldValue, answers: Record<string, unknown>) => {
    try {
      const nv = await api.answerFacts(jobId, f.field_id, answers);
      replaceValue(nv);
      toast(nv.status === "cannot_determine" ? "Still can't decide — more facts needed" : `Re-decided: ${String(nv.value ?? "—")}`, nv.status === "cannot_determine" ? "warn" : "ok");
    } catch (e) {
      toast(`Could not save answers: ${errMsg(e)}`, "error");
    }
  }, [jobId]);

  const ctx: RowCtx = useMemo(
    () => ({ docName, jump, activeKey, busy, act, answer, templateId: form?.template_id ?? job?.form?.template_id ?? null }),
    [docName, jump, activeKey, busy, act, answer, form, job],
  );

  // ---------- keyboard
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTypingTarget(e.target)) return;
      const inViewer = e.target instanceof HTMLElement && e.target.closest(".pages-scroll");
      const k = e.key;
      if (k === "j" || (k === "ArrowDown" && !inViewer)) { e.preventDefault(); move(1); return; }
      if (k === "k" || (k === "ArrowUp" && !inViewer)) { e.preventDefault(); move(-1); return; }
      if (k === "?") { setShowKeys((s) => !s); return; }
      if (k === "Escape") { if (editing) setEditing(null); else setShowKeys(false); return; }
      if (!selected || busy || selected.requires_manual_signing) return;
      if (k === "a") { e.preventDefault(); act(selected, "accept"); }
      else if (k === "e" || (k === "Enter" && !editing && !(e.target instanceof HTMLButtonElement))) { e.preventDefault(); setEditing(selected.field_id); }
      else if (k === "n") { e.preventDefault(); act(selected, "not_applicable"); }
      else if (k === "r" && selected.review_status !== "pending") { e.preventDefault(); act(selected, "reset"); }
      else if (/^[1-9]$/.test(k) && selected.candidates.length >= Number(k)) { e.preventDefault(); act(selected, "choose_candidate", { candidate_index: Number(k) - 1 }); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [move, selected, busy, editing, act]);

  // Select the first field once values arrive.
  useEffect(() => {
    if (values && selId == null && visible.length) select(visible[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [values]);

  // ---------- render
  if (loadErr) return <div className="page"><div className="callout callout-bad">Could not load job #{jobId}: {loadErr}. <a href="#/">Back to jobs</a></div></div>;
  if (!job) return <div className="page"><div className="busy"><span className="spinner" /> Loading job…</div></div>;

  if (job.status !== "done") {
    return (
      <div className="page page-narrow">
        <h1 className="h1">Job #{job.id} <JobStatusChip status={job.status} progress={job.progress} /></h1>
        <p className="muted">{job.form?.filename} · {job.documents.length} source document{job.documents.length === 1 ? "" : "s"}</p>
        {job.status === "running" || job.status === "queued" ? (
          <JobRunPanel jobId={job.id} onDone={loadJob} />
        ) : job.status === "failed" ? (
          <div className="callout callout-bad">
            <strong>The job failed.</strong> <span className="prewrap">{job.error}</span>
            <div><button className="btn btn-sm" onClick={async () => { try { setJob(await api.runJob(job.id)); } catch (e) { toast(errMsg(e), "error"); } }}>Run again</button></div>
          </div>
        ) : (
          <div className="callout callout-info">
            This job has not been run yet.{" "}
            {job.documents.length ? (
              <button className="btn btn-sm btn-primary" onClick={async () => { try { setJob(await api.runJob(job.id)); } catch (e) { toast(errMsg(e), "error"); } }}>Run now</button>
            ) : <a href="#/">Start a new job</a>}
          </div>
        )}
      </div>
    );
  }

  const groups = filter === "low" ? [{ title: null as string | null, items: visible }] : groupFields(visible);
  const doc = job.documents.find((d) => d.id === docId) ?? job.documents[0];
  const overlays: Overlay[] = [];
  if (selected && doc) {
    // All evidence of the selected field on this document (faint) + the active one (strong).
    const all = [
      ...selected.evidence,
      ...selected.candidates.flatMap((c) => c.evidence),
      ...(selected.derivation?.inputs ?? []),
      ...selected.facts_used.flatMap((f) => f.evidence),
    ];
    all.forEach((e, i) => {
      if (e.document_id === doc.id && e.page != null) {
        const boxes = e.boxes && e.boxes.length ? e.boxes : e.bbox ? [e.bbox] : [];
        if (boxes.length) overlays.push({ id: `a${i}`, page: e.page, boxes, tone: "alt" });
      }
    });
  }
  if (hl && doc && hl.docId === doc.id && hl.boxes.length) overlays.push({ id: "hit", page: hl.page, boxes: hl.boxes, tone: "hit" });

  const isWord = (form?.form_type ?? job.form?.form_type) === "docx";
  const pageCount = form?.page_count ?? 0;
  const previewOverlays: Overlay[] = (values ?? [])
    .filter((f) => f.page && f.bbox)
    .map((f) => ({
      id: f.field_id,
      page: f.page!,
      boxes: [f.bbox!],
      tone: f.field_id === selId ? "field-active" : "field",
      title: f.label,
      onClick: () => select(f),
    }));

  return (
    <div className="review">
      <div className="review-bar">
        <div className="rb-title">
          <a href="#/" className="muted small">← Jobs</a>
          <h1 className="h1 ellipsis" title={job.form?.filename}>{job.form?.filename ?? `Job ${job.id}`}</h1>
          <span className="muted small nowrap">Job #{job.id}</span>
        </div>
        <div className="rb-progress" title={`${reviewed} of ${reviewable.length} fields reviewed`}>
          <span className="small nowrap"><strong className="mono">{reviewed}</strong>/<span className="mono">{reviewable.length}</span> reviewed</span>
          <ProgressBar value={reviewable.length ? reviewed / reviewable.length : 0} label="Fields reviewed" />
        </div>
        <div className="rb-actions">
          <button className="btn btn-ghost btn-sm" onClick={() => setShowKeys((s) => !s)} aria-expanded={showKeys}>Shortcuts <kbd>?</kbd></button>
          <ExportMenu jobId={job.id} formType={form?.form_type} pending={reviewable.length - reviewed} />
        </div>
      </div>

      {showKeys && <ShortcutLegend onClose={() => setShowKeys(false)} />}

      <div className="review-split">
        <section className="fields-pane" aria-label="Form fields">
          <div className="filters" role="tablist" aria-label="Filter fields">
            {FILTERS.map((f) => (
              <button key={f.id} role="tab" aria-selected={filter === f.id} className={`filter${filter === f.id ? " on" : ""}`}
                title={f.title} onClick={() => setFilter(f.id)}>
                {f.label} <span className="count">{counts[f.id]}</span>
              </button>
            ))}
          </div>
          {values === null ? (
            <div className="busy"><span className="spinner" /> Loading values…</div>
          ) : visible.length === 0 ? (
            <div className="empty">{filter === "pending" ? "All fields reviewed. Export when ready." : "No fields match this filter."}</div>
          ) : (
            <ul className="flist" ref={listRef} role="listbox" aria-label="Fields">
              {groups.map((g) => (
                <li key={g.title ?? "_"} className="fgroup" role="presentation">
                  {g.title && <div className={`fgroup-h${g.title === JUDGMENT_GROUP ? " judge" : ""}`}>{g.title}<span className="muted"> · {g.items.length}</span></div>}
                  <ul role="presentation">
                    {g.items.map((f) => (
                      <FieldRow key={f.field_id} field={f} selected={f.field_id === selId} editing={editing === f.field_id}
                        onSelect={onRowSelect} setEditing={setEditing} ctx={ctx} />
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="viewer-pane" aria-label="Documents">
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={tab === "sources"} className={`tab${tab === "sources" ? " on" : ""}`} onClick={() => setTab("sources")}>Sources</button>
            <button role="tab" aria-selected={tab === "preview"} className={`tab${tab === "preview" ? " on" : ""}`} onClick={() => setTab("preview")}>Preview</button>
          </div>
          {tab === "sources" ? (
            doc ? (
              <PageStack
                key={doc.id}
                ariaLabel={`Source document ${doc.filename}`}
                pages={doc.pages.length ? doc.pages : Array.from({ length: doc.page_count ?? 1 }, (_, i) => ({ page: i + 1 }))}
                imgUrl={(p, dpi) => api.docPageUrl(doc.id, p, dpi)}
                overlays={overlays}
                target={hl && hl.docId === doc.id ? target : null}
                toolbarStart={
                  <select className="input input-sm doc-picker" value={doc.id} aria-label="Source document"
                    onChange={(e) => { setDocId(Number(e.target.value)); setHl(null); setActiveKey(null); }}>
                    {job.documents.map((d) => (
                      <option key={d.id} value={d.id}>{d.filename}{d.ocr_used ? " (OCR)" : ""} · {d.page_count ?? d.pages.length}p</option>
                    ))}
                  </select>
                }
              />
            ) : <div className="empty">No source documents.</div>
          ) : isWord ? (
            <div className="preview-html">
              <div className="viewer-toolbar">
                <span className="small muted">Word preview (approximate layout)</span>
                <button className="btn btn-sm btn-ghost" onClick={() => setPreviewVer((v) => v + 1)}>Refresh</button>
              </div>
              <iframe title="Filled form preview" sandbox="" src={api.previewHtmlUrl(job.id, previewVer)} />
            </div>
          ) : (
            <PageStack
              ariaLabel="Filled form preview"
              pages={Array.from({ length: pageCount }, (_, i) => ({ page: i + 1 }))}
              imgUrl={(p, dpi) => api.previewPageUrl(job.id, p, previewVer, dpi)}
              overlays={previewOverlays}
              target={previewTarget}
              toolbarStart={
                <>
                  <span className="small muted">Filled form</span>
                  <button className="btn btn-sm btn-ghost" onClick={() => setPreviewVer((v) => v + 1)}>Refresh</button>
                </>
              }
            />
          )}
        </section>
      </div>
    </div>
  );
}

const JUDGMENT_GROUP = "Judgment fields";

function groupFields(xs: FieldValue[]): { title: string | null; items: FieldValue[] }[] {
  const out: { title: string | null; items: FieldValue[] }[] = [];
  const judg = xs.filter((f) => f.kind === "judgment");
  if (judg.length) out.push({ title: JUDGMENT_GROUP, items: judg });
  for (const f of xs.filter((x) => x.kind !== "judgment")) {
    const t = f.section || "Fields";
    const last = out[out.length - 1];
    if (last && last.title === t) last.items.push(f);
    else out.push({ title: t, items: [f] });
  }
  return out;
}

function ShortcutLegend({ onClose }: { onClose: () => void }) {
  const rows: [string, string][] = [
    ["j / ↓", "Next field"], ["k / ↑", "Previous field"], ["a", "Accept"], ["e / Enter", "Edit"],
    ["Enter", "Save edit"], ["Esc", "Cancel edit"], ["n", "Not applicable"], ["r", "Reset to model value"],
    ["1–9", "Pick candidate"], ["?", "Toggle this legend"],
  ];
  return (
    <div className="legend" role="dialog" aria-label="Keyboard shortcuts">
      {rows.map(([k, d]) => <span key={k + d} className="legend-item"><kbd>{k}</kbd> {d}</span>)}
      <button className="icon-btn" onClick={onClose} aria-label="Close shortcuts">×</button>
    </div>
  );
}
