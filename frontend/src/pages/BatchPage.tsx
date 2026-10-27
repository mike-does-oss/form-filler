import { useEffect, useRef, useState } from "react";
import { api, errMsg, type Batch, type Form } from "../api";
import { DropZone } from "../components/DropZone";
import { HealthBanner } from "../components/HealthBanner";
import { ProgressBar } from "../components/JobProgress";
import { toast } from "../components/Toast";
import { fromDrop, fromInput, type Grouped } from "../folders";
import { navigate } from "../router";
import { Elapsed, FormSummary, FORM_TYPE_LABEL, JobStatusChip, Step, fmtDate } from "./UploadPage";

const UPLOAD_CHUNK = 10; // files per request

// ------------------------------------------------------------------ new batch + list
export function BatchPage() {
  const [form, setForm] = useState<Form | null>(null);
  const [parsing, setParsing] = useState<string | null>(null);
  const [forms, setForms] = useState<Omit<Form, "fields">[]>([]);
  const [grouped, setGrouped] = useState<Grouped | null>(null);
  const [name, setName] = useState("");
  const [upload, setUpload] = useState<{ done: number; total: number } | null>(null);
  const [batches, setBatches] = useState<Batch[] | null>(null);

  const loadBatches = () => api.listBatches().then(setBatches).catch((e) => toast(errMsg(e), "error"));
  useEffect(() => {
    loadBatches();
    api.listForms().then(setForms).catch(() => {});
  }, []);

  const uploadForm = async (files: File[]) => {
    setParsing(files[0].name);
    try {
      setForm(await api.uploadForm(files[0]));
      api.listForms().then(setForms).catch(() => {});
    } catch (e) {
      toast(`Could not read form: ${errMsg(e)}`, "error");
    } finally {
      setParsing(null);
    }
  };

  const takeFolder = (g: Grouped) => {
    setGrouped(g);
    if (g.root && !name) setName(g.root);
    if (!g.sets.length) toast("No sub-folders with supported documents found. Put each person's or case's documents in its own folder.", "error");
  };

  const removeSet = (setName: string) =>
    setGrouped((g) => (g ? { ...g, sets: g.sets.filter((s) => s.name !== setName) } : g));

  const start = async () => {
    if (!form || !grouped?.sets.length) return;
    const total = grouped.sets.reduce((n, s) => n + s.files.length, 0);
    setUpload({ done: 0, total });
    let batch: Batch | null = null;
    try {
      batch = await api.createBatch(form.id, name.trim(), grouped.sets.map((s) => s.name));
      let done = 0;
      for (const job of batch.jobs) {
        const set = grouped.sets.find((s) => s.name === job.label)!;
        for (let i = 0; i < set.files.length; i += UPLOAD_CHUNK) {
          const chunk = set.files.slice(i, i + UPLOAD_CHUNK).map((f) => new File([f.file], f.name, { type: f.file.type }));
          await api.uploadDocs(job.id, chunk);
          done += chunk.length;
          setUpload({ done, total });
        }
      }
      await api.runBatch(batch.id);
      navigate(`/batches/${batch.id}`);
    } catch (e) {
      toast(`Batch upload failed: ${errMsg(e)}`, "error");
      if (batch) api.deleteBatch(batch.id).catch(() => {});
      setUpload(null);
    }
  };

  const fileCount = grouped?.sets.reduce((n, s) => n + s.files.length, 0) ?? 0;
  const busy = upload !== null;

  return (
    <div className="page page-upload">
      <HealthBanner />
      <div className="upload-grid">
        <section className="panel steps" aria-label="New batch">
          <h1 className="h1">New batch</h1>
          <p className="muted small batch-intro">Fill the same form once per folder: put each person's or case's documents in its own sub-folder, then drop the parent folder.</p>

          <Step n={1} title="Blank form" done={!!form}>
            {parsing ? (
              <div className="busy">
                <span className="spinner" aria-hidden="true" />
                <strong>Reading {parsing}… <Elapsed /></strong>
              </div>
            ) : form ? (
              <FormSummary form={form} onChange={busy ? undefined : () => setForm(null)} />
            ) : (
              <>
                <DropZone accept=".pdf,.docx" onFiles={uploadForm} title="Drop the blank form" hint="fillable PDF, flat PDF or Word (.docx)" />
                {forms.length > 0 && (
                  <label className="reuse">
                    <span className="muted small">or reuse a form you already uploaded</span>
                    <select className="input" defaultValue="" onChange={(e) => e.target.value && api.getForm(Number(e.target.value)).then(setForm).catch((er) => toast(errMsg(er), "error"))}>
                      <option value="">Choose a form…</option>
                      {forms.map((f) => (
                        <option key={f.id} value={f.id}>#{f.id} · {f.filename} · {FORM_TYPE_LABEL[f.form_type] ?? f.form_type}</option>
                      ))}
                    </select>
                  </label>
                )}
              </>
            )}
          </Step>

          <Step n={2} title="Source folders" done={!!grouped?.sets.length} disabled={!form}>
            {!busy && <FolderDrop onFolder={takeFolder} compact={!!grouped?.sets.length} />}
            {grouped && grouped.sets.length > 0 && (
              <>
                <table className="table setlist">
                  <thead><tr><th>Folder → one filled form each</th><th>Documents</th><th><span className="sr-only">Remove</span></th></tr></thead>
                  <tbody>
                    {grouped.sets.map((s) => (
                      <tr key={s.name}>
                        <td>
                          <span className="folder-ico" aria-hidden="true">▸</span> <strong>{s.name}</strong>
                          <div className="muted small ellipsis" title={s.files.map((f) => f.name).join("\n")}>
                            {s.files.slice(0, 3).map((f) => f.name).join(", ")}{s.files.length > 3 ? `, +${s.files.length - 3} more` : ""}
                          </div>
                        </td>
                        <td className="mono">{s.files.length}</td>
                        <td className="row-actions">
                          {!busy && <button className="icon-btn" aria-label={`Leave out ${s.name}`} onClick={() => removeSet(s.name)}>×</button>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
            {grouped && grouped.skipped.length > 0 && (
              <details className="callout callout-warn skipped">
                <summary>{grouped.skipped.length} file{grouped.skipped.length === 1 ? "" : "s"} left out</summary>
                <ul>{grouped.skipped.slice(0, 50).map((s) => <li key={s.path}><span className="mono">{s.path}</span>: {s.reason}</li>)}</ul>
              </details>
            )}
            {!grouped && form && <FolderTree example />}
          </Step>

          <Step n={3} title="Fill" done={false} disabled={!grouped?.sets.length}>
            <label className="batch-name">
              <span className="muted small">Batch name (used for the download)</span>
              <input className="input" value={name} placeholder="e.g. October intake" onChange={(e) => setName(e.target.value)} disabled={busy} />
            </label>
            {upload ? (
              <div className="busy">
                <span className="spinner" aria-hidden="true" />
                <div className="grow">
                  <strong>Uploading {upload.done}/{upload.total} documents…</strong>
                  <ProgressBar value={upload.total ? upload.done / upload.total : 0} label="Upload progress" />
                </div>
              </div>
            ) : (
              <button className="btn btn-primary btn-lg" disabled={!grouped?.sets.length} onClick={start}>
                Fill {grouped?.sets.length ?? 0} form{grouped?.sets.length === 1 ? "" : "s"}
                <span className="muted-on-primary"> · {fileCount} document{fileCount === 1 ? "" : "s"}</span>
              </button>
            )}
            <p className="muted small">Sets are filled one or two at a time to keep memory use down. You can leave this page; progress is saved.</p>
          </Step>
        </section>

        <section className="panel recent" aria-label="Recent batches">
          <div className="panel-head">
            <h2 className="h2">Recent batches</h2>
            <button className="btn btn-sm btn-ghost" onClick={loadBatches}>Refresh</button>
          </div>
          {batches === null ? (
            <div className="muted">Loading…</div>
          ) : batches.length === 0 ? (
            <div className="empty">No batches yet.</div>
          ) : (
            <table className="table">
              <thead><tr><th>Batch</th><th>Form</th><th>Progress</th><th>Created</th></tr></thead>
              <tbody>
                {batches.map((b) => (
                  <tr key={b.id}>
                    <td className="ellipsis" title={b.name}><a href={`#/batches/${b.id}`}>{b.name}</a></td>
                    <td className="ellipsis muted" title={b.form?.filename}>{b.form?.filename}</td>
                    <td className="nowrap"><BatchProgress b={b} /></td>
                    <td className="muted nowrap">{fmtDate(b.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
    </div>
  );
}

function FolderTree({ example }: { example: boolean }) {
  if (!example) return null;
  return (
    <pre className="foldertree muted small" aria-label="Example folder layout">{`clients/            ← drop this folder
  alice/            ← one filled form
    payslip.pdf
    licence.jpg
  bob/              ← another filled form
    statement.pdf`}</pre>
  );
}

function FolderDrop({ onFolder, compact }: { onFolder: (g: Grouped) => void; compact?: boolean }) {
  const [over, setOver] = useState(false);
  const [reading, setReading] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    input.current?.setAttribute("webkitdirectory", "");
    input.current?.setAttribute("directory", "");
  }, []);
  return (
    <div
      className={`dropzone${over ? " over" : ""}${compact ? " compact" : ""}`}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={async (e) => {
        e.preventDefault();
        setOver(false);
        setReading(true);
        try { onFolder(await fromDrop(e.dataTransfer.items)); }
        catch (er) { toast(`Could not read the folder: ${errMsg(er)}`, "error"); }
        finally { setReading(false); }
      }}
    >
      <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6">
        <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z" />
      </svg>
      <div className="dz-text">
        <strong>{reading ? "Reading folder…" : compact ? "Drop a different folder" : "Drop the folder of folders"}</strong>
        <span>
          Drag &amp; drop or{" "}
          <button type="button" className="linklike" onClick={() => input.current?.click()}>choose a folder</button>
          {" "}· one sub-folder per form to fill
        </span>
      </div>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        onChange={(e) => { if (e.target.files?.length) onFolder(fromInput(e.target.files)); e.target.value = ""; }}
      />
    </div>
  );
}

function BatchProgress({ b }: { b: Batch }) {
  if (b.running || b.done + b.failed < b.total) {
    return <span className="badge tone-info">{b.done}/{b.total} filled</span>;
  }
  return (
    <span className="batch-progress">
      <span className={`badge tone-${b.failed ? "bad" : "ok"}`}>{b.done}/{b.total} filled{b.failed ? ` · ${b.failed} failed` : ""}</span>
      {b.needs_review > 0 && <span className="chip chip-warn">{b.needs_review} to check</span>}
    </span>
  );
}

// ------------------------------------------------------------------ one batch
export function BatchDetail({ batchId }: { batchId: string }) {
  const [b, setB] = useState<Batch | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const out = await api.getBatch(batchId);
        if (stop) return;
        setB(out);
        if (out.running > 0) timer = setTimeout(load, 1500);
      } catch (e) {
        if (!stop) setError(errMsg(e));
      }
    };
    load();
    const onFocus = () => load(); // pick up review progress when coming back from a set
    window.addEventListener("focus", onFocus);
    return () => { stop = true; clearTimeout(timer); window.removeEventListener("focus", onFocus); };
  }, [batchId]);

  if (error) return <div className="page"><div className="callout callout-bad">{error}</div></div>;
  if (!b) return <div className="page muted">Loading…</div>;

  const finished = b.done + b.failed;
  const del = async () => {
    if (!confirm(`Delete batch "${b.name}" with all ${b.total} sets, their documents, values and audit history? This cannot be undone.`)) return;
    try {
      await api.deleteBatch(b.id);
      toast("Batch deleted", "ok");
      navigate("/batches");
    } catch (e) {
      toast(errMsg(e), "error");
    }
  };
  const retry = async () => {
    try { setB(await api.runBatch(b.id)); } catch (e) { toast(errMsg(e), "error"); }
  };
  const nextToCheck = b.jobs.find((j) => j.status === "done" && j.needs_review > 0);

  return (
    <div className="page batch-detail">
      <div className="panel">
        <div className="panel-head">
          <div className="grow">
            <a href="#/batches" className="muted small">← Batches</a>
            <h1 className="h1">{b.name}</h1>
            <p className="muted small">
              {b.form?.filename} · {b.total} set{b.total === 1 ? "" : "s"} · created {fmtDate(b.created_at)}
              {b.forms_per_minute ? ` · ${b.forms_per_minute} forms/min` : ""}
            </p>
          </div>
          <div className="row-actions wrap">
            {nextToCheck && <a className="btn btn-primary btn-sm" href={`#/jobs/${nextToCheck.id}`}>Review next: {nextToCheck.label} →</a>}
            {b.done > 0 && (
              <>
                <a className="btn btn-sm" href={api.batchZipUrl(b.id, "filled")} download>Download all (.zip)</a>
                <a className="btn btn-sm btn-ghost" href={api.batchZipUrl(b.id, "flattened")} download title="Non-editable PDFs">Flattened</a>
              </>
            )}
            {b.failed > 0 && b.running === 0 && <button className="btn btn-sm" onClick={retry}>Retry failed</button>}
            <button className="btn btn-sm btn-ghost danger" onClick={del}>Delete</button>
          </div>
        </div>

        <div className="batch-summary">
          <ProgressBar value={b.total ? finished / b.total : 0} label="Sets finished" tone={b.failed ? "bad" : undefined} />
          <span className="small nowrap">
            <strong className="mono">{b.done}</strong>/<span className="mono">{b.total}</span> filled
            {b.failed > 0 && <> · <span className="err">{b.failed} failed</span></>}
            {b.needs_review > 0 && <> · <span className="warn-text">{b.needs_review} fields to check</span></>}
          </span>
        </div>
        <p className="muted small">
          The download has one folder per set (filled form, <span className="mono">values.json</span>, <span className="mono">values.csv</span>) and a{" "}
          <span className="mono">summary.csv</span> with every set's values side by side. Values you haven't reviewed are included as proposed;
          judgment answers are left blank until you accept them.
        </p>

        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>Set</th><th>Status</th><th>Docs</th><th>Fields filled</th><th>To check</th><th>Reviewed</th><th><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {b.jobs.map((j) => (
                <tr key={j.id}>
                  <td><span className="folder-ico" aria-hidden="true">▸</span> <strong>{j.label}</strong></td>
                  <td>
                    <JobStatusChip status={j.status} progress={j.progress} />
                    {j.status === "running" && j.stage && <span className="muted small"> {j.stage}</span>}
                    {j.error && <div className="err small ellipsis" title={j.error}>{j.error}</div>}
                  </td>
                  <td className="mono">{j.documents}</td>
                  <td className="mono">{j.status === "done" ? `${j.filled}/${j.total_fields}` : "–"}</td>
                  <td>{j.status === "done" ? (j.needs_review ? <span className="chip chip-warn">{j.needs_review}</span> : <span className="chip chip-ok">0</span>) : "–"}</td>
                  <td className="mono">{j.status === "done" ? `${j.reviewed}/${j.total_fields}` : "–"}</td>
                  <td className="row-actions">
                    <a className="btn btn-sm" href={`#/jobs/${j.id}`}>{j.status === "done" ? "Review" : "Open"}</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
