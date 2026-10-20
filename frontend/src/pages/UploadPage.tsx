import { useEffect, useMemo, useState } from "react";
import { api, errMsg, type Doc, type Form, type JobSummary } from "../api";
import { DropZone } from "../components/DropZone";
import { HealthBanner } from "../components/HealthBanner";
import { JobRunPanel } from "../components/JobProgress";
import { toast } from "../components/Toast";
import { navigate } from "../router";

const FORM_TYPE_LABEL: Record<string, string> = { acroform: "Fillable PDF", docx: "Word document", flat_pdf: "Flat PDF" };
const SOURCE_EXT = ".pdf,.docx,.png,.jpg,.jpeg,.tif,.tiff,.txt";

export function UploadPage() {
  const [form, setForm] = useState<Form | null>(null);
  const [parsing, setParsing] = useState<string | null>(null);
  const [jobId, setJobId] = useState<number | null>(null);
  const [docs, setDocs] = useState<Doc[]>([]);
  const [uploading, setUploading] = useState(0);
  const [running, setRunning] = useState(false);
  const [jobs, setJobs] = useState<JobSummary[] | null>(null);
  const [forms, setForms] = useState<Omit<Form, "fields">[]>([]);

  const loadJobs = () => api.listJobs().then(setJobs).catch((e) => toast(errMsg(e), "error"));
  useEffect(() => {
    loadJobs();
    api.listForms().then(setForms).catch(() => {});
  }, []);

  const uploadForm = async (files: File[]) => {
    const f = files[0];
    setParsing(f.name);
    try {
      const out = await api.uploadForm(f);
      setForm(out);
      setJobId(null);
      setDocs([]);
      api.listForms().then(setForms).catch(() => {});
    } catch (e) {
      toast(`Could not read form: ${errMsg(e)}`, "error");
    } finally {
      setParsing(null);
    }
  };

  const pickExisting = async (id: number) => {
    try {
      setForm(await api.getForm(id));
      setJobId(null);
      setDocs([]);
    } catch (e) {
      toast(errMsg(e), "error");
    }
  };

  const ensureJob = async (): Promise<number> => {
    if (jobId) return jobId;
    const j = await api.createJob(form!.id);
    setJobId(j.id);
    return j.id;
  };

  const uploadDocs = async (files: File[]) => {
    setUploading((n) => n + files.length);
    try {
      const id = await ensureJob();
      const out = await api.uploadDocs(id, files);
      setDocs((d) => [...d, ...out]);
    } catch (e) {
      toast(`Upload failed: ${errMsg(e)}`, "error");
    } finally {
      setUploading((n) => n - files.length);
    }
  };

  const removeDoc = async (d: Doc) => {
    if (!jobId) return;
    try {
      await api.deleteDoc(jobId, d.id);
      setDocs((xs) => xs.filter((x) => x.id !== d.id));
    } catch (e) {
      toast(errMsg(e), "error");
    }
  };

  const run = async () => {
    if (!jobId) return;
    try {
      await api.runJob(jobId);
      setRunning(true);
      loadJobs();
    } catch (e) {
      toast(`Could not start: ${errMsg(e)}`, "error");
    }
  };

  const reset = () => {
    setForm(null);
    setJobId(null);
    setDocs([]);
    setRunning(false);
  };

  const deleteJob = async (j: JobSummary) => {
    if (!confirm(`Delete job #${j.id} (${j.form_filename}) and all its documents, values and audit history? This cannot be undone.`)) return;
    try {
      await api.deleteJob(j.id);
      setJobs((xs) => xs?.filter((x) => x.id !== j.id) ?? null);
      if (j.id === jobId) reset();
      toast(`Job #${j.id} deleted`, "ok");
    } catch (e) {
      toast(errMsg(e), "error");
    }
  };

  return (
    <div className="page page-upload">
      <HealthBanner />
      <div className="upload-grid">
        <section className="panel steps" aria-label="New job">
          <h1 className="h1">New job</h1>

          <Step n={1} title="Blank form" done={!!form}>
            {parsing ? (
              <div className="busy">
                <span className="spinner" aria-hidden="true" />
                <div>
                  <strong>Reading {parsing}… <Elapsed /></strong>
                  <div className="muted small">Detecting fields, cleaning labels and proposing judgment rules with the local model. This usually takes under a minute, longer on a busy model server.</div>
                </div>
              </div>
            ) : form ? (
              <FormSummary form={form} onChange={running ? undefined : reset} />
            ) : (
              <>
                <DropZone accept=".pdf,.docx" onFiles={uploadForm} title="Drop the blank form" hint="fillable PDF, flat PDF or Word (.docx)" />
                {forms.length > 0 && (
                  <label className="reuse">
                    <span className="muted small">or reuse a form you already uploaded</span>
                    <select className="input" defaultValue="" onChange={(e) => e.target.value && pickExisting(Number(e.target.value))}>
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

          <Step n={2} title="Source documents" done={docs.length > 0} disabled={!form}>
            {form && !running && (
              <DropZone
                accept={SOURCE_EXT}
                multiple
                onFiles={uploadDocs}
                title="Drop source documents"
                hint="PDF, Word, images (PNG/JPG/TIFF) or text"
                compact={docs.length > 0}
              />
            )}
            {(docs.length > 0 || uploading > 0) && (
              <ul className="doclist">
                {docs.map((d) => (
                  <li key={d.id}>
                    <FileIcon name={d.filename} />
                    <span className="doc-name">{d.filename}</span>
                    {!running && (
                      <button className="icon-btn" aria-label={`Remove ${d.filename}`} onClick={() => removeDoc(d)}>×</button>
                    )}
                  </li>
                ))}
                {uploading > 0 && <li className="muted"><span className="spinner sm" aria-hidden="true" /> Uploading {uploading} file{uploading > 1 ? "s" : ""}…</li>}
              </ul>
            )}
          </Step>

          <Step n={3} title="Fill" done={false} disabled={!docs.length}>
            {running && jobId ? (
              <JobRunPanel jobId={jobId} onDone={() => navigate(`/jobs/${jobId}`)} />
            ) : (
              <button className="btn btn-primary btn-lg" disabled={!docs.length || uploading > 0} onClick={run}>
                Run extraction
                <span className="muted-on-primary"> · {docs.length} document{docs.length === 1 ? "" : "s"}</span>
              </button>
            )}
          </Step>
        </section>

        <section className="panel recent" aria-label="Recent jobs">
          <div className="panel-head">
            <h2 className="h2">Recent jobs</h2>
            <button className="btn btn-sm btn-ghost" onClick={loadJobs}>Refresh</button>
          </div>
          {jobs === null ? (
            <div className="muted">Loading…</div>
          ) : jobs.length === 0 ? (
            <div className="empty">No jobs yet.</div>
          ) : (
            <table className="table">
              <thead>
                <tr><th>#</th><th>Form</th><th>Status</th><th>Created</th><th><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.id}>
                    <td className="mono">{j.id}</td>
                    <td className="ellipsis" title={j.form_filename}>
                      <a href={`#/jobs/${j.id}`}>{j.form_filename}</a>
                    </td>
                    <td><JobStatusChip status={j.status} progress={j.progress} /></td>
                    <td className="muted nowrap">{fmtDate(j.created_at)}</td>
                    <td className="row-actions">
                      <a className="btn btn-sm" href={`#/jobs/${j.id}`}>{j.status === "done" ? "Review" : "Open"}</a>
                      <button className="btn btn-sm btn-ghost danger" onClick={() => deleteJob(j)} aria-label={`Delete job ${j.id}`}>Delete</button>
                    </td>
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

function Elapsed() {
  const [t0] = useState(() => Date.now());
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);
  const s = Math.round((Date.now() - t0) / 1000);
  return <span className="mono muted small">{Math.floor(s / 60)}:{String(s % 60).padStart(2, "0")}</span>;
}

function Step({ n, title, done, disabled, children }: { n: number; title: string; done: boolean; disabled?: boolean; children: React.ReactNode }) {
  return (
    <div className={`step${disabled ? " step-disabled" : ""}${done ? " step-done" : ""}`}>
      <div className="step-num" aria-hidden="true">{done ? "✓" : n}</div>
      <div className="step-body">
        <h3 className="h3">{title}</h3>
        {!disabled && children}
      </div>
    </div>
  );
}

function FormSummary({ form, onChange }: { form: Form; onChange?: () => void }) {
  const s = useMemo(() => {
    const f = form.fields;
    const sections = new Set(f.map((x) => x.section).filter(Boolean));
    return {
      total: f.length,
      judgment: f.filter((x) => x.kind === "judgment").length,
      required: f.filter((x) => x.required).length,
      signature: f.filter((x) => x.type === "signature").length,
      sections: sections.size,
    };
  }, [form]);
  return (
    <div className="formsum">
      <div className="formsum-head">
        <FileIcon name={form.filename} />
        <strong className="ellipsis">{form.filename}</strong>
        <span className="chip">{FORM_TYPE_LABEL[form.form_type] ?? form.form_type}</span>
        <span className="muted small">{form.page_count} page{form.page_count === 1 ? "" : "s"}</span>
        {onChange && <button className="btn btn-sm btn-ghost" onClick={onChange}>Change</button>}
      </div>
      {form.template_matched && (
        <div className="callout callout-ok">
          <span aria-hidden="true">★</span> Matched saved template <strong>{form.template_name}</strong> — your earlier field edits and judgment rules apply.
        </div>
      )}
      <dl className="stats">
        <div><dt>Fields</dt><dd>{s.total}</dd></div>
        <div><dt>Lookup</dt><dd>{s.total - s.judgment}</dd></div>
        <div><dt>Judgment</dt><dd>{s.judgment}</dd></div>
        <div><dt>Required</dt><dd>{s.required}</dd></div>
        {s.signature > 0 && <div><dt>Signatures</dt><dd>{s.signature}</dd></div>}
        {s.sections > 0 && <div><dt>Sections</dt><dd>{s.sections}</dd></div>}
      </dl>
      {s.total === 0 && <div className="callout callout-warn">No fields detected. {form.form_type === "flat_pdf" ? "Draw them in the field mapper." : ""}</div>}
      <div className="formsum-links">
        {form.form_type === "flat_pdf" && (
          <a className="btn btn-sm" href={`#/forms/${form.id}/map`}>Review field mapping →</a>
        )}
        {form.template_id && <a className="btn btn-sm btn-ghost" href={`#/templates/${form.template_id}`}>Edit template &amp; rules</a>}
      </div>
      <details className="fieldpeek">
        <summary>Show detected fields</summary>
        <ul>
          {form.fields.map((f) => (
            <li key={f.key}>
              <span>{f.label}</span>
              <span className="muted small mono">{f.type}</span>
              {f.kind === "judgment" && <span className="chip chip-judge">judgment</span>}
              {f.required && <span className="req" title="Required">*</span>}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

export function JobStatusChip({ status, progress }: { status: string; progress?: number }) {
  const map: Record<string, string> = { done: "ok", failed: "bad", running: "info", queued: "info", created: "muted" };
  return (
    <span className={`badge tone-${map[status] ?? "muted"}`}>
      {status === "running" && progress != null ? `running ${Math.round(progress * 100)}%` : status}
    </span>
  );
}

function FileIcon({ name }: { name: string }) {
  const ext = name.split(".").pop()?.toUpperCase().slice(0, 4) ?? "";
  return <span className="fileicon" aria-hidden="true">{ext}</span>;
}

export function fmtDate(s?: string | null): string {
  if (!s) return "";
  const d = new Date(s.replace(" ", "T") + (s.includes("Z") || s.includes("+") ? "" : "Z"));
  if (isNaN(d.getTime())) return s;
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
