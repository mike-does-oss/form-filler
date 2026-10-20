import { match, useRoute } from "./router";
import { UploadPage } from "./pages/UploadPage";
import { ReviewPage } from "./pages/ReviewPage";
import { TemplatesPage } from "./pages/TemplatesPage";
import { TemplateEditor } from "./pages/TemplateEditor";
import { MapperPage } from "./pages/MapperPage";
import { ToastHost } from "./components/Toast";

export function App() {
  const path = useRoute();
  let page: React.ReactNode;
  let m: Record<string, string> | null;
  let section = "jobs";
  if ((m = match("/jobs/:id", path))) page = <ReviewPage key={m.id} jobId={Number(m.id)} />;
  else if ((m = match("/templates/:id", path))) {
    section = "templates";
    page = <TemplateEditor key={m.id} templateId={Number(m.id)} />;
  } else if (match("/templates", path)) {
    section = "templates";
    page = <TemplatesPage />;
  } else if ((m = match("/forms/:id/map", path))) {
    section = "templates";
    page = <MapperPage key={m.id} formId={Number(m.id)} />;
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
          <a href="#/templates" aria-current={section === "templates" ? "page" : undefined}>Templates</a>
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
