import { useEffect, useState } from "react";
import { api, errMsg, type Health } from "../api";

export function HealthBanner() {
  const [h, setH] = useState<Health | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = () => api.health().then((x) => { setH(x); setErr(null); }).catch((e) => setErr(errMsg(e)));
  useEffect(() => { load(); }, []);

  if (err) return (
    <div className="banner banner-bad" role="alert">
      <strong>Backend unreachable.</strong> {err} <button className="btn btn-sm" onClick={load}>Retry</button>
    </div>
  );
  if (!h) return <div className="banner banner-muted">Checking local services…</div>;

  const models = Object.entries(h.llm.models ?? {});
  const missing = models.filter(([, m]) => !m.available);
  const ocrOk = h.ocr.active_engine !== "tesseract" || h.ocr.tesseract !== false;
  const ok = h.status === "ok";
  return (
    <div className={`banner ${ok ? "banner-ok" : "banner-warn"}`} role={ok ? undefined : "alert"}>
      <div className="health-items">
        <span className={`hitem ${h.llm.reachable ? "ok" : "bad"}`}>
          <span aria-hidden="true">{h.llm.reachable ? "✓" : "✕"}</span>
          Model server {h.llm.reachable ? "reachable" : "unreachable"}
          {h.llm.backend ? <span className="muted"> ({h.llm.backend})</span> : null}
        </span>
        {models.map(([role, m]) => (
          <span key={role} className={`hitem ${m.available ? "ok" : "bad"}`}>
            <span aria-hidden="true">{m.available ? "✓" : "✕"}</span>
            {role}: <code>{m.name}</code>
          </span>
        ))}
        <span className={`hitem ${ocrOk ? "ok" : "bad"}`}>
          <span aria-hidden="true">{ocrOk ? "✓" : "✕"}</span>
          OCR: <code>{h.ocr.active_engine}</code>
          {h.ocr.configured_engine && h.ocr.configured_engine !== h.ocr.active_engine ? (
            <span className="muted"> (configured {h.ocr.configured_engine}, not installed)</span>
          ) : null}
        </span>
        <button className="btn btn-sm btn-ghost" onClick={load} title="Re-check">Re-check</button>
      </div>
      {!h.llm.reachable && (
        <div className="hint">
          Start the local model server{h.llm.base_url ? <> at <code>{h.llm.base_url}</code></> : null} (e.g. <code>ollama serve</code>).
        </div>
      )}
      {h.llm.reachable && missing.length > 0 && (
        <div className="hint">
          Missing model{missing.length > 1 ? "s" : ""}: run{" "}
          {[...new Set(missing.map(([, m]) => m.name))].map((n) => <code key={n} className="cmd">ollama pull {n}</code>)}
        </div>
      )}
      {!ocrOk && <div className="hint">Install Tesseract (<code>brew install tesseract</code>) for scanned documents.</div>}
    </div>
  );
}
