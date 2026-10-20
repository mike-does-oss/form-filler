import { useEffect, useRef, useState } from "react";
import { errMsg, exportJob, type ExportFormat } from "../api";
import { toast } from "./Toast";

export function ExportMenu({ jobId, formType, pending }: { jobId: number; formType?: string; pending: number }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);

  const isWord = formType === "docx";
  const items: { f: ExportFormat; label: string; sub: string }[] = [
    { f: "filled", label: isWord ? "Filled Word document" : "Filled PDF", sub: isWord ? ".docx, original layout" : "Fields stay editable" },
    { f: "flattened", label: "Flattened PDF", sub: isWord ? "Needs LibreOffice installed" : "Values burned in, not editable" },
    { f: "json", label: "JSON", sub: "Values with evidence & audit data" },
    { f: "csv", label: "CSV", sub: "One row per field" },
  ];
  const run = async (f: ExportFormat) => {
    setBusy(f);
    try {
      const { filename, warnings } = await exportJob(jobId, f);
      toast(`Exported ${filename}`, warnings.length ? "warn" : "ok", warnings);
      setOpen(false);
    } catch (e) {
      toast(`Export failed: ${errMsg(e)}`, "error");
    } finally {
      setBusy(null);
    }
  };
  return (
    <div className="menu" ref={root}>
      <button className="btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        Export <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="menu-pop" role="menu">
          {pending > 0 && (
            <div className="menu-note">
              {pending} field{pending === 1 ? "" : "s"} not reviewed. Unreviewed lookup values export as extracted; judgment fields stay blank until reviewed.
            </div>
          )}
          {items.map((it) => (
            <button key={it.f} role="menuitem" className="menu-item" disabled={busy !== null} onClick={() => run(it.f)}>
              <span>{it.label}{busy === it.f ? " …" : ""}</span>
              <span className="muted small">{it.sub}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
