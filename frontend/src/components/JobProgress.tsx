import { useEffect, useRef, useState } from "react";
import { api, type JobStatus } from "../api";

interface Snapshot { status: JobStatus; progress: number; stage: string | null; error: string | null }

/** Follows a job via SSE (falls back to polling) and reports each snapshot. */
export function useJobEvents(jobId: number | null, active: boolean, onUpdate: (s: Snapshot) => void) {
  const cb = useRef(onUpdate);
  cb.current = onUpdate;
  useEffect(() => {
    if (!jobId || !active) return;
    let closed = false;
    let poll: ReturnType<typeof setInterval> | null = null;
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    const startPolling = () => {
      if (poll || closed) return;
      poll = setInterval(async () => {
        try {
          const j = await api.getJob(jobId);
          cb.current(j);
          if (j.status === "done" || j.status === "failed") stop();
        } catch { /* keep trying */ }
      }, 1500);
    };
    const stop = () => {
      closed = true;
      es.close();
      if (poll) clearInterval(poll);
    };
    es.onmessage = (e) => {
      const s = JSON.parse(e.data) as Snapshot;
      cb.current(s);
      if (s.status === "done" || s.status === "failed") stop();
    };
    es.onerror = () => {
      // Stream ended or broke: confirm the final state by polling.
      es.close();
      startPolling();
    };
    return stop;
  }, [jobId, active]);
}

export function ProgressBar({ value, label, tone }: { value: number; label?: string; tone?: "bad" }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className={`pbar${tone ? " pbar-" + tone : ""}`} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <div className="pbar-fill" style={{ width: pct + "%" }} />
    </div>
  );
}

export function JobRunPanel({ jobId, onDone }: { jobId: number; onDone: () => void }) {
  const [s, setS] = useState<Snapshot | null>(null);
  const [startedAt] = useState(() => Date.now());
  const [, tick] = useState(0);
  useEffect(() => {
    const t = setInterval(() => tick((x) => x + 1), 1000);
    return () => clearInterval(t);
  }, []);
  useJobEvents(jobId, true, (x) => {
    setS(x);
    if (x.status === "done") onDone();
  });
  const secs = Math.round((Date.now() - startedAt) / 1000);
  return (
    <div className="runpanel">
      <div className="runpanel-head">
        <strong>{s?.status === "failed" ? "Job failed" : s?.status === "queued" ? "Queued…" : "Filling form…"}</strong>
        <span className="mono muted">{Math.round((s?.progress ?? 0) * 100)}% · {Math.floor(secs / 60)}:{String(secs % 60).padStart(2, "0")}</span>
      </div>
      <ProgressBar value={s?.progress ?? 0} label="Job progress" tone={s?.status === "failed" ? "bad" : undefined} />
      <div className="runpanel-stage">{s?.error ? <span className="err">{s.error}</span> : s?.stage ?? "Starting…"}</div>
      <p className="muted small">A job usually takes 1–5 minutes on a local model. You can leave this page; the job keeps running.</p>
    </div>
  );
}
