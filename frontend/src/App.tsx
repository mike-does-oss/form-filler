import { match, useRoute } from "./router";
import { UploadPage } from "./pages/UploadPage";
import { ReviewPage } from "./pages/ReviewPage";
import { TemplatesPage } from "./pages/TemplatesPage";
import { TemplateEditor } from "./pages/TemplateEditor";
import { MapperPage } from "./pages/MapperPage";
import { BatchDetail, BatchPage } from "./pages/BatchPage";
import { SetupPage } from "./pages/SetupPage";
import { ToastHost } from "./components/Toast";
import { useEffect, useState } from "react";
import { api } from "./api";

const SKIP_KEY = "ff.setupSkipped";

/** null = still checking, true = models ready (or skipped), false = show the setup screen. */
function useSetupGate(): [boolean | null, () => void] {
  const [ready, setReady] = useState<boolean | null>(() => {
    try { return sessionStorage.getItem(SKIP_KEY) ? true : null; } catch { return null; }
  });
  useEffect(() => {
    if (ready !== null) return;
    api.setup().then((s) => setReady(s.ready)).catch(() => setReady(true)); // unreachable backend: the banner explains
  }, [ready]);
  const pass = () => { try { sessionStorage.setItem(SKIP_KEY, "1"); } catch { /* ignore */ } setReady(true); };
  return [ready, pass];
}

export function App() {
  const path = useRoute();
  const [ready, pass] = useSetupGate();
  let page: React.ReactNode;
  let m: Record<string, string> | null;
  let section = "jobs";
  if (match("/setup", path) || ready === false) {
    section = "setup";
    page = <SetupPage onReady={pass} />;
  } else if (ready === null) page = <div className="banner banner-muted">Checking this computer…</div>;
  else if ((m = match("/jobs/:id", path))) page = <ReviewPage key={m.id} jobId={Number(m.id)} />;
  else if ((m = match("/templates/:id", path))) {
    section = "templates";
    page = <TemplateEditor key={m.id} templateId={Number(m.id)} />;
  } else if (match("/templates", path)) {
    section = "templates";
    page = <TemplatesPage />;
  } else if ((m = match("/forms/:id/map", path))) {
    section = "templates";
    page = <MapperPage key={m.id} formId={Number(m.id)} />;
  } else if ((m = match("/batches/:id", path))) {
    section = "batches";
    page = <BatchDetail key={m.id} batchId={m.id} />;
  } else if (match("/batches", path)) {
    section = "batches";
    page = <BatchPage />;
  } else page = <UploadPage />;

  return (
    <div className="app">
      <header className="topbar">
        <a className="brand" href="#/">
          <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden="true">
            <rect x="2" y="1" width="12" height="14" rx="1.5" fill="currentColor" opacity="0.9" />
            <path d="M5 5h6M5 8h6M5 11h3" stroke="var(--bg)" strokeWidth="1.3" />
          </svg>
          Local Form Filler
        </a>
        <nav className="mainnav" aria-label="Main">
          <a href="#/" aria-current={section === "jobs" ? "page" : undefined}>Jobs</a>
          <a href="#/batches" aria-current={section === "batches" ? "page" : undefined}>Batches</a>
          <a href="#/templates" aria-current={section === "templates" ? "page" : undefined}>Templates</a>
          <a href="#/setup" aria-current={section === "setup" ? "page" : undefined}>Model</a>
        </nav>
        <span className="local-pill" title="All processing happens on this machine. Documents never leave it.">
          <span aria-hidden="true">●</span> On-device only
        </span>
      </header>
      <main className="main">{page}</main>
      <ToastHost />
    </div>
  );
}
