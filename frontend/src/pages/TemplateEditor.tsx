import { useEffect, useMemo, useState } from "react";
import {
  api, errMsg, FIELD_TYPES, type FactDefT, type FieldDef, type Form, type RegistryEntry, type Rule, type Template,
} from "../api";
import { toast } from "../components/Toast";
import { fmtDate } from "./UploadPage";

const FACT_TYPES = ["text", "number", "date", "currency", "checkbox", "choice"];

export function TemplateEditor({ templateId }: { templateId: number }) {
  const [t, setT] = useState<Template | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [registry, setRegistry] = useState<RegistryEntry[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [name, setName] = useState("");

  const load = async () => {
    try {
      const tt = await api.getTemplate(templateId);
      setT(tt);
      setName(tt.name);
      if (tt.latest_form_id) setForm(await api.getForm(tt.latest_form_id));
    } catch (e) {
      setErr(errMsg(e));
    }
  };
  useEffect(() => {
    load();
    api.registry().then(setRegistry).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [templateId]);

  if (err) return <div className="page"><div className="callout callout-bad">{err}</div></div>;
  if (!t) return <div className="page"><div className="busy"><span className="spinner" /> Loading template…</div></div>;

  const saveName = async () => {
    if (name.trim() && name !== t.name) {
      try { setT(await api.patchTemplate(t.id, { name: name.trim() })); toast("Template renamed", "ok"); }
      catch (e) { toast(errMsg(e), "error"); }
    }
  };

  const judgmentKeys = new Set((form?.fields ?? t.fields).filter((f) => f.kind === "judgment" && f.type !== "signature").map((f) => f.key));
  const rules = t.rules.filter((r) => judgmentKeys.has(r.field_key));
  const flagged = rules.filter((r) => r.flagged);

  return (
    <div className="page">
      <div className="page-head">
        <a href="#/templates" className="muted small">← Templates</a>
        <div className="tpl-title">
          <input className="input input-title" value={name} aria-label="Template name" onChange={(e) => setName(e.target.value)}
            onBlur={saveName} onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()} />
          <span className="chip">{t.form_type}</span>
          {form?.form_type === "flat_pdf" && <a className="btn btn-sm" href={`#/forms/${form.id}/map`}>Edit field mapping</a>}
        </div>
        <p className="muted small">Edits here apply to every future job for this form. Latest form #{t.latest_form_id ?? "—"} · fingerprint <span className="mono">{t.fingerprint?.slice(0, 12)}</span></p>
      </div>

      {flagged.length > 0 && (
        <div className="callout callout-bad" role="alert">
          <strong>⚑ {flagged.length} rule{flagged.length > 1 ? "s" : ""} flagged for review.</strong>
          <ul>{flagged.map((r) => <li key={r.id}><a href={`#rule-${r.field_key}`} onClick={(e) => { e.preventDefault(); document.getElementById(`rule-${r.field_key}`)?.scrollIntoView({ behavior: "smooth" }); }}>{labelFor(form, t, r.field_key)}</a>: {r.flag_reason}</li>)}</ul>
        </div>
      )}

      {form ? <FieldsEditor form={form} onSaved={(f) => { setForm(f); load(); }} /> : <div className="callout callout-warn">No form is linked to this template.</div>}

      <section className="panel">
        <div className="panel-head">
          <h2 className="h2">Judgment rules</h2>
          <span className="muted small">{rules.length} rule{rules.length === 1 ? "" : "s"} · {rules.filter((r) => r.status === "draft").length} draft</span>
        </div>
        {rules.length === 0 ? (
          <div className="empty">No judgment fields. Mark a field as <em>judgment</em> above to give it a rule.</div>
        ) : (
          rules.map((r) => (
            <RuleEditor key={r.id + ":" + r.updated_at} rule={r} label={labelFor(form, t, r.field_key)} field={form?.fields.find((f) => f.key === r.field_key)}
              registry={registry} templateId={t.id} onSaved={(nr) => setT((tt) => tt && { ...tt, rules: tt.rules.map((x) => (x.field_key === nr.field_key ? nr : x)) })} />
          ))
        )}
      </section>

      <section className="panel">
        <div className="panel-head"><h2 className="h2">Recent corrections</h2><span className="muted small">{t.corrections.length}</span></div>
        {t.corrections.length === 0 ? <div className="empty">No reviewer corrections yet.</div> : (
          <table className="table">
            <thead><tr><th>Field</th><th>Model</th><th>Reviewer</th><th>Note</th><th>When</th></tr></thead>
            <tbody>
              {t.corrections.slice(0, 50).map((c) => (
                <tr key={c.id}>
                  <td>{c.field_label ?? c.field_key}</td>
                  <td className="mono"><s className="muted">{c.model_value ?? "—"}</s></td>
                  <td className="mono">{c.user_value ?? "—"}</td>
                  <td className="small">{c.user_note ?? ""}</td>
                  <td className="muted nowrap">{fmtDate(c.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function labelFor(form: Form | null, t: Template, key: string): string {
  return form?.fields.find((f) => f.key === key)?.label ?? t.fields.find((f) => f.key === key)?.label ?? key;
}

// ------------------------------------------------------------------ fields
type Draft = Pick<FieldDef, "key" | "label" | "type" | "kind" | "required" | "section" | "options" | "format_hint" | "instructions">;
const pick = (f: FieldDef): Draft => ({
  key: f.key, label: f.label, type: f.type, kind: f.kind, required: f.required, section: f.section ?? "",
  options: f.options ?? [], format_hint: f.format_hint ?? "", instructions: f.instructions ?? "",
});

function FieldsEditor({ form, onSaved }: { form: Form; onSaved: (f: Form) => void }) {
  const [drafts, setDrafts] = useState<Draft[]>(() => form.fields.map(pick));
  const [open, setOpen] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [q, setQ] = useState("");
  useEffect(() => setDrafts(form.fields.map(pick)), [form]);
  const orig = useMemo(() => Object.fromEntries(form.fields.map((f) => [f.key, pick(f)])), [form]);
  const changed = drafts.filter((d) => JSON.stringify(d) !== JSON.stringify(orig[d.key]));
  const upd = (key: string, patch: Partial<Draft>) => setDrafts((ds) => ds.map((d) => (d.key === key ? { ...d, ...patch } : d)));

  const save = async () => {
    setSaving(true);
    try {
      const body = changed.map((d) => {
        const o = orig[d.key];
        const out: Partial<FieldDef> = { key: d.key };
        (Object.keys(d) as (keyof Draft)[]).forEach((k) => {
          if (JSON.stringify(d[k]) !== JSON.stringify(o[k])) (out as Record<string, unknown>)[k] = d[k] === "" && k !== "label" ? null : d[k];
        });
        return out;
      });
      const nf = await api.putFields(form.id, body);
      toast(`Saved ${body.length} field${body.length === 1 ? "" : "s"}`, "ok");
      onSaved(nf);
    } catch (e) {
      toast(`Save failed: ${errMsg(e)}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const shown = drafts.filter((d) => !q || (d.label + " " + d.key + " " + (d.section ?? "")).toLowerCase().includes(q.toLowerCase()));
  const newJudgment = changed.some((d) => d.kind === "judgment" && orig[d.key].kind !== "judgment");
  return (
    <section className="panel">
      <div className="panel-head">
        <h2 className="h2">Fields <span className="muted small">{drafts.length}</span></h2>
        <input className="input input-sm" placeholder="Filter fields…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter fields" />
        <div className="spacer" />
        {changed.length > 0 && <span className="small">{changed.length} unsaved change{changed.length === 1 ? "" : "s"}</span>}
        <button className="btn btn-sm btn-ghost" disabled={!changed.length || saving} onClick={() => setDrafts(form.fields.map(pick))}>Discard</button>
        <button className="btn btn-sm btn-primary" disabled={!changed.length || saving} onClick={save}>
          {saving ? (newJudgment ? "Saving & drafting rules…" : "Saving…") : "Save fields"}
        </button>
      </div>
      <div className="table-wrap">
        <table className="table table-edit">
          <thead>
            <tr><th>Label</th><th>Type</th><th>Kind</th><th title="Required">Req.</th><th>Section</th><th><span className="sr-only">More</span></th></tr>
          </thead>
          <tbody>
            {shown.map((d) => {
              const dirty = JSON.stringify(d) !== JSON.stringify(orig[d.key]);
              return (
                <FieldEditRow key={d.key} d={d} dirty={dirty} open={open === d.key} toggle={() => setOpen((o) => (o === d.key ? null : d.key))} upd={upd} />
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function FieldEditRow({ d, dirty, open, toggle, upd }: { d: Draft; dirty: boolean; open: boolean; toggle: () => void; upd: (k: string, p: Partial<Draft>) => void }) {
  return (
    <>
      <tr className={dirty ? "dirty" : ""}>
        <td>
          <input className="input input-sm" value={d.label} onChange={(e) => upd(d.key, { label: e.target.value })} aria-label={`Label for ${d.key}`} />
          <div className="mono muted tiny">{d.key}</div>
        </td>
        <td>
          <select className="input input-sm" value={d.type} onChange={(e) => upd(d.key, { type: e.target.value })} aria-label="Type">
            {FIELD_TYPES.map((x) => <option key={x}>{x}</option>)}
          </select>
        </td>
        <td>
          <div className="seg" role="radiogroup" aria-label="Kind">
            {(["lookup", "judgment"] as const).map((k) => (
              <button key={k} role="radio" aria-checked={d.kind === k} className={`seg-btn${d.kind === k ? " on" : ""}${k === "judgment" ? " judge" : ""}`}
                onClick={() => upd(d.key, { kind: k })}>{k}</button>
            ))}
          </div>
        </td>
        <td><input type="checkbox" checked={d.required} onChange={(e) => upd(d.key, { required: e.target.checked })} aria-label="Required" /></td>
        <td><input className="input input-sm" value={d.section ?? ""} onChange={(e) => upd(d.key, { section: e.target.value })} aria-label="Section" /></td>
        <td><button className="btn btn-sm btn-ghost" onClick={toggle} aria-expanded={open}>{open ? "Less" : "More"}</button></td>
      </tr>
      {open && (
        <tr className="subrow">
          <td colSpan={6}>
            <div className="subgrid">
              <label>Options (one per line{d.type !== "choice" ? ", for choice fields" : ""})
                <textarea className="input" rows={3} value={d.options.join("\n")} onChange={(e) => upd(d.key, { options: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })} />
              </label>
              <label>Format hint
                <input className="input" value={d.format_hint ?? ""} onChange={(e) => upd(d.key, { format_hint: e.target.value })} />
              </label>
              <label className="span2">Instructions
                <textarea className="input" rows={3} value={d.instructions ?? ""} onChange={(e) => upd(d.key, { instructions: e.target.value })} />
              </label>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ------------------------------------------------------------------ rules
function RuleEditor({ rule, label, field, registry, templateId, onSaved }: {
  rule: Rule; label: string; field?: FieldDef; registry: RegistryEntry[]; templateId: number; onSaved: (r: Rule) => void;
}) {
  const suggested = (rule.rule_params?.suggested ?? null) as { rule_code?: string; params?: Record<string, unknown> } | null;
  const ownParams = Object.fromEntries(Object.entries(rule.rule_params ?? {}).filter(([k]) => k !== "suggested"));
  const [facts, setFacts] = useState<FactDefT[]>(rule.facts.map((f) => ({ ...f, question: f.question ?? "", required: f.required ?? true })));
  const [type, setType] = useState<"code" | "rubric">(rule.rule_type);
  const [code, setCode] = useState<string>(rule.rule_code ?? suggested?.rule_code ?? "");
  const [params, setParams] = useState<string>(() =>
    JSON.stringify(Object.keys(ownParams).length ? ownParams : suggested?.params ?? {}, null, 2));
  const [rubric, setRubric] = useState(rule.rubric_text ?? "");
  const [busy, setBusy] = useState<"save" | "propose" | null>(null);
  const [open, setOpen] = useState(rule.flagged || rule.status === "draft");

  let paramsErr: string | null = null;
  let parsed: Record<string, unknown> = {};
  try {
    const p = JSON.parse(params || "{}");
    if (typeof p !== "object" || Array.isArray(p) || p === null) paramsErr = "Params must be a JSON object";
    else parsed = p;
  } catch (e) {
    paramsErr = errMsg(e);
  }
  const reg = registry.find((r) => r.name === code);
  const keys = facts.map((f) => f.key);
  const dupKey = keys.find((k, i) => keys.indexOf(k) !== i);

  const save = async () => {
    if (type === "code" && (paramsErr || !code)) return;
    setBusy("save");
    try {
      const body = {
        facts: facts.map((f) => ({ ...f, question: f.question || null })),
        rule_type: type,
        rubric_text: rubric,
        status: "saved" as const,
        ...(type === "code" ? { rule_code: code, rule_params: suggested ? { ...parsed, suggested } : parsed } : {}),
      };
      onSaved(await api.putRule(templateId, rule.field_key, body));
      toast(`Rule for “${label}” saved`, "ok");
    } catch (e) {
      toast(`Save failed: ${errMsg(e)}`, "error");
    } finally {
      setBusy(null);
    }
  };
  const regenerate = async () => {
    if (!confirm("Replace this rule with a newly drafted one from the model? Unsaved edits are lost.")) return;
    setBusy("propose");
    try {
      onSaved(await api.proposeRule(templateId, rule.field_key));
      toast("New draft generated", "ok");
    } catch (e) {
      toast(`Regenerate failed: ${errMsg(e)}`, "error");
    } finally {
      setBusy(null);
    }
  };
  const updFact = (i: number, p: Partial<FactDefT>) => setFacts((fs) => fs.map((f, j) => (j === i ? { ...f, ...p } : f)));

  return (
    <div className={`rule${rule.flagged ? " flagged" : ""}`} id={`rule-${rule.field_key}`}>
      <button className="rule-head" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        <span className="rule-caret" aria-hidden="true">{open ? "▾" : "▸"}</span>
        <strong className="rule-label">{label}</strong>
        <span className={`chip ${rule.status === "draft" ? "chip-warn" : "chip-ok"}`}>{rule.status === "draft" ? "draft" : "saved"}</span>
        <span className="chip">{rule.rule_type === "code" ? `code: ${rule.rule_code}` : "rubric"}</span>
        <span className="muted small">{rule.facts.length} fact{rule.facts.length === 1 ? "" : "s"}</span>
        {rule.flagged && <span className="badge tone-bad">⚑ flagged</span>}
      </button>
      {open && (
        <div className="rule-body">
          {rule.flagged && <div className="callout callout-bad"><strong>Flagged:</strong> {rule.flag_reason}</div>}
          {field?.options && field.options.length > 0 && <div className="small"><span className="muted">Answer options:</span> {field.options.join(" · ")}</div>}
          {field?.instructions && <details className="instructions"><summary>Form instructions</summary><p>{field.instructions}</p></details>}

          <h4 className="dsec-h">Facts the decision depends on</h4>
          <div className="table-wrap">
            <table className="table table-edit facts-table">
              <thead><tr><th>Key</th><th>Label</th><th>Type</th><th>Question to ask if missing</th><th title="Required">Req.</th><th /></tr></thead>
              <tbody>
                {facts.map((f, i) => (
                  <tr key={i}>
                    <td><input className="input input-sm mono" value={f.key} onChange={(e) => updFact(i, { key: e.target.value.replace(/[^a-z0-9_]/gi, "_").toLowerCase() })} aria-label="Fact key" /></td>
                    <td><input className="input input-sm" value={f.label} onChange={(e) => updFact(i, { label: e.target.value })} aria-label="Fact label" /></td>
                    <td>
                      <select className="input input-sm" value={f.type} onChange={(e) => updFact(i, { type: e.target.value })} aria-label="Fact type">
                        {[...new Set([...FACT_TYPES, f.type])].map((x) => <option key={x}>{x}</option>)}
                      </select>
                    </td>
                    <td><input className="input input-sm" value={f.question ?? ""} onChange={(e) => updFact(i, { question: e.target.value })} aria-label="Question" /></td>
                    <td><input type="checkbox" checked={f.required} onChange={(e) => updFact(i, { required: e.target.checked })} aria-label="Required fact" /></td>
                    <td><button className="icon-btn" aria-label={`Remove fact ${f.key}`} onClick={() => setFacts((fs) => fs.filter((_, j) => j !== i))}>×</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button className="btn btn-sm" onClick={() => setFacts((fs) => [...fs, { key: `fact_${fs.length + 1}`, label: "", type: "text", question: "", required: true }])}>+ Add fact</button>
          {dupKey && <div className="err small">Duplicate fact key “{dupKey}”.</div>}

          <h4 className="dsec-h">Rule</h4>
          <div className="seg" role="radiogroup" aria-label="Rule type">
            <button role="radio" aria-checked={type === "rubric"} className={`seg-btn${type === "rubric" ? " on" : ""}`} onClick={() => setType("rubric")}>Rubric (model decides, 5 votes)</button>
            <button role="radio" aria-checked={type === "code"} className={`seg-btn${type === "code" ? " on" : ""}`} onClick={() => setType("code")}>Code rule (deterministic)</button>
          </div>
          {type === "rubric" ? (
            <label className="block">Rubric text
              <textarea className="input" rows={6} value={rubric} onChange={(e) => setRubric(e.target.value)} />
            </label>
          ) : (
            <div className="code-rule">
              {suggested?.rule_code && (
                <div className="callout callout-info small">
                  Suggested by the model: <code>{suggested.rule_code}</code>{" "}
                  <button className="btn btn-sm" onClick={() => { setCode(suggested.rule_code!); setParams(JSON.stringify(suggested.params ?? {}, null, 2)); }}>Use suggestion</button>
                </div>
              )}
              <label className="block">Code rule
                <select className="input" value={code} onChange={(e) => setCode(e.target.value)}>
                  <option value="">Choose a registered rule…</option>
                  {registry.map((r) => <option key={r.name} value={r.name}>{r.name}</option>)}
                </select>
              </label>
              {reg && (
                <div className="small">
                  <p className="muted">{reg.description}</p>
                  <details><summary>Example params</summary><pre className="code-block">{JSON.stringify(reg.params_example, null, 2)}</pre>
                    <button className="btn btn-sm" onClick={() => setParams(JSON.stringify(reg.params_example, null, 2))}>Use example</button>
                  </details>
                </div>
              )}
              <label className="block">Params (JSON)
                <textarea className={`input mono${paramsErr ? " invalid" : ""}`} rows={8} value={params} onChange={(e) => setParams(e.target.value)} spellCheck={false} />
              </label>
              {paramsErr && <div className="err small">{paramsErr}</div>}
              <label className="block">Rubric text (kept as a fallback / documentation)
                <textarea className="input" rows={3} value={rubric} onChange={(e) => setRubric(e.target.value)} />
              </label>
            </div>
          )}
          <div className="editor-actions">
            <button className="btn btn-primary btn-sm" onClick={save} disabled={busy !== null || !!dupKey || (type === "code" && (!!paramsErr || !code))}>
              {busy === "save" ? "Saving…" : "Save rule"}
            </button>
            <button className="btn btn-sm" onClick={regenerate} disabled={busy !== null}>{busy === "propose" ? "Drafting with the model…" : "Regenerate draft"}</button>
            <span className="muted small">Last updated {fmtDate(rule.updated_at)}</span>
          </div>
        </div>
      )}
    </div>
  );
}
