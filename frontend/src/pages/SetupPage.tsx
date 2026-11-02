import { useEffect, useRef, useState } from "react";
import { api, errMsg, type Setup, type SetupProgress } from "../api";
import { navigate } from "../router";

// Approximate download size of the portable Ollama build per release asset (GB).
const OLLAMA_GB: Record<string, number> = {
  "ollama-darwin.tgz": 0.2,
  "ollama-windows-amd64.zip": 1.6,
  "ollama-windows-arm64.zip": 0.2,
  "ollama-linux-amd64.tgz": 1.7,
  "ollama-linux-arm64.tgz": 0.6,
};
const EMBEDDING_GB = 1.2; // bge-m3

const STEP_LABEL: Record<string, string> = {
  starting: "Starting",
  install: "Downloading the local model server (Ollama)",
  start: "Starting the model server",
  pull: "Downloading models",
  done: "Ready",
};

/**
 * First-run screen. Everything the app needs is downloaded once, here, with the user's say-so;
 * after that it never goes online. Also reachable later at #/setup to switch models.
 */
export function SetupPage({ onReady }: { onReady?: () => void }) {
  const [s, setS] = useState<Setup | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [progress, setProgress] = useState<SetupProgress | null>(null);
  const timer = useRef<number | null>(null);

  const load = () =>
    api.setup().then((x) => {
      setS(x);
      setErr(null);
      // Keep the current model if it's already downloaded; otherwise start from the recommendation.
      const haveCurrent = x.catalog.some((c) => c.name === x.models.current) && !x.models.missing.includes(x.models.current);
      setModel((m) => m ?? (haveCurrent ? x.models.current : x.catalog.find((c) => c.recommended)?.name ?? x.models.current));
      if (x.progress.running) setProgress(x.progress);
    }).catch((e) => setErr(errMsg(e)));

  useEffect(() => { load(); }, []);

  // Poll while a run is in progress; refresh status when it finishes.
  useEffect(() => {
    if (!progress?.running) return;
    timer.current = window.setInterval(() => {
      api.setupProgress().then((p) => {
        setProgress(p);
        if (!p.running) { if (timer.current) window.clearInterval(timer.current); load(); }
      }).catch(() => {});
    }, 1000);
    return () => { if (timer.current) window.clearInterval(timer.current); };
  }, [progress?.running]);

  const start = async () => {
    setErr(null);
    try {
      const changed = model && s && model !== s.models.current;
      const x = await api.setupStart(changed ? model : null);
      setS(x);
      setProgress({ ...x.progress, running: true });
    } catch (e) { setErr(errMsg(e)); }
  };

  if (err && !s) return <div className="banner banner-bad" role="alert">Backend unreachable: {err} <button className="btn btn-sm" onClick={load}>Retry</button></div>;
  if (!s) return <div className="banner banner-muted">Checking this computer…</div>;

  const running = !!progress?.running;
  const chosen = s.catalog.find((c) => c.name === model);
  const needOllama = !s.ollama.running && !s.ollama.installed && !s.ollama.remote;
  const needEmbedding = s.models.missing.includes(s.models.required[s.models.required.length - 1]) || s.models.missing.some((m) => m.startsWith("bge"));
  const modelMissing = !!chosen && (chosen.name !== s.models.current || s.models.missing.includes(chosen.name));
  const totalGb = (needOllama && s.portable_asset ? OLLAMA_GB[s.portable_asset] ?? 0 : 0) + (modelMissing ? chosen?.download_gb ?? 0 : 0) + (needEmbedding ? EMBEDDING_GB : 0);
  const ready = s.ready && !(chosen && chosen.name !== s.models.current);
  const remoteBlocked = s.ollama.remote && !s.ollama.running;

  return (
    <div className="setup">
      <div>
        <h1>{s.ready ? "Model settings" : "Set up on this computer"}</h1>
        <p className="lede">
          Forms are filled by a language model running on this machine. The model server and the model itself are
          downloaded once, here. After that the app works offline and nothing you upload leaves this computer.
        </p>
      </div>

      <div className="setup-status" aria-label="Status">
        <div className="row"><span className="k">Model server</span>
          <span>{s.ollama.running ? "Running" : s.ollama.installed ? "Installed, not running" : s.ollama.remote ? `Not reachable at ${s.ollama.base_url}` : "Not installed (will be downloaded into the app's folder; no admin rights needed)"}</span></div>
        <div className="row"><span className="k">Reading scans</span>
          <span>{s.ocr.active_engine === "rapidocr" ? "Built in" : <>Using <code>{s.ocr.active_engine}</code></>}</span></div>
        {s.ram_gb != null && <div className="row"><span className="k">Memory</span><span>{s.ram_gb} GB</span></div>}
      </div>

      {s.backend === "ollama" && !s.ollama.remote && (
        <fieldset className="model-list" disabled={running} style={{ border: 0, padding: 0, margin: 0 }}>
          <legend className="muted" style={{ marginBottom: 6, fontSize: 13 }}>Which model?</legend>
          {s.catalog.map((c) => {
            const tight = s.ram_gb != null && s.ram_gb < c.min_ram_gb;
            return (
              <label key={c.name} className="model-opt">
                <input type="radio" name="model" checked={model === c.name} onChange={() => setModel(c.name)} />
                <span>
                  <span className="name">{c.label} <code>{c.name}</code>
                    {c.recommended && <span className="rec">Recommended for this computer</span>}
                    {tight && <span className="tight">Needs {c.min_ram_gb} GB; may be slow</span>}
                  </span>
                  <div className="blurb">{c.blurb}</div>
                </span>
                <span className="size">{c.download_gb} GB download</span>
              </label>
            );
          })}
        </fieldset>
      )}

      {err && <div className="banner banner-bad" role="alert">{err}</div>}

      {progress && (progress.running || progress.error || progress.done) && (
        <div className="setup-progress" aria-live="polite">
          <strong>{progress.error ? "Setup stopped" : STEP_LABEL[progress.step ?? "starting"]}</strong>
          {progress.running && (
            <div className={`bar ${progress.fraction == null ? "indeterminate" : ""}`}>
              <div style={progress.fraction != null ? { width: `${Math.round(progress.fraction * 100)}%` } : undefined} />
            </div>
          )}
          <div className="detail">{progress.detail}</div>
        </div>
      )}

      {ready && !running ? (
        <div className="setup-done">
          <span>Everything is in place. Model: <code>{s.models.current}</code>.</span>
          <button className="btn btn-primary" onClick={() => { onReady?.(); navigate("/"); }}>Start filling forms</button>
        </div>
      ) : (
        <div className="setup-actions">
          <button className="btn btn-primary" onClick={start} disabled={running || remoteBlocked}>
            {running ? "Working…" : totalGb > 0 ? `Download and set up (about ${totalGb.toFixed(1)} GB)` : "Set up"}
          </button>
          {!s.ready && !running && <a href="#/" className="muted" onClick={() => onReady?.()}>Skip for now</a>}
          {remoteBlocked && <span className="muted">Start the model server on that machine, then re-check.</span>}
          {!running && <button className="btn btn-sm btn-ghost" onClick={load}>Re-check</button>}
        </div>
      )}
    </div>
  );
}
