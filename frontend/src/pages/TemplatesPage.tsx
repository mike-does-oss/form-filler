import { useEffect, useState } from "react";
import { api, errMsg, type TemplateSummary } from "../api";
import { fmtDate } from "./UploadPage";

const FT: Record<string, string> = { acroform: "Fillable PDF", docx: "Word", flat_pdf: "Flat PDF" };

export function TemplatesPage() {
  const [rows, setRows] = useState<TemplateSummary[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.listTemplates().then(setRows).catch((e) => setErr(errMsg(e))); }, []);
  return (
    <div className="page">
      <div className="page-head">
        <h1 className="h1">Templates</h1>
        <p className="muted">A template is created the first time a form is uploaded. Field edits, judgment rules and reviewer corrections are remembered and reused whenever the same form comes back.</p>
      </div>
      {err && <div className="callout callout-bad">{err}</div>}
      {rows === null ? <div className="muted">Loading…</div> : rows.length === 0 ? (
        <div className="empty">No templates yet. Upload a blank form on the <a href="#/">Jobs</a> page.</div>
      ) : (
        <div className="panel">
          <table className="table">
            <thead>
              <tr><th>Name</th><th>Type</th><th className="num">Forms</th><th className="num">Rules</th><th>Flags</th><th className="num">Corrections</th><th>Updated</th></tr>
            </thead>
            <tbody>
              {rows.map((t) => (
                <tr key={t.id}>
                  <td><a href={`#/templates/${t.id}`}><strong>{t.name}</strong></a></td>
                  <td>{FT[t.form_type] ?? t.form_type}</td>
                  <td className="num mono">{t.form_count}</td>
                  <td className="num mono">{t.rule_count}</td>
                  <td>{t.flagged_count > 0 ? <span className="badge tone-bad">⚑ {t.flagged_count} flagged</span> : <span className="muted small">—</span>}</td>
                  <td className="num mono">{t.correction_count}</td>
                  <td className="muted nowrap">{fmtDate(t.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
