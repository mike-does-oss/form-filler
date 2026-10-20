import { useState } from "react";
import type { FieldValue } from "../api";
import { agreementText } from "../format";
import { EvidenceList, type RowCtx } from "./FieldRow";

export function JudgmentDetail({ field: f, ctx }: { field: FieldValue; ctx: RowCtx }) {
  const agree = agreementText(f);
  const rule = f.rule;
  const votes = rule?.votes ? Object.entries(rule.votes).sort((a, b) => b[1] - a[1]) : [];
  const unanimous = f.run_agreement != null && f.run_agreement >= 0.999;
  return (
    <div className="judg">
      {rule?.status === "draft" && (
        <div className="callout callout-warn">
          <strong>Draft rule.</strong> This rule was proposed by the model and has not been saved by a person.
          {ctx.templateId && <> <a href={`#/templates/${ctx.templateId}`}>Review rule →</a></>}
        </div>
      )}

      {f.missing_facts.length > 0 && <MissingFacts field={f} ctx={ctx} />}

      <section className="dsec">
        <h4 className="dsec-h">Decision</h4>
        {f.reasoning ? <p className="reasoning">{f.reasoning}</p> : <p className="muted small">No reasoning recorded.</p>}
        <div className="judg-meta">
          {agree && (
            <span className={`badge ${unanimous ? "tone-ok" : (f.run_agreement ?? 0) >= 0.6 ? "tone-warn" : "tone-bad"}`}
              title="Self-consistency: how many independent model runs reached the same answer">
              {unanimous ? "✓" : "≈"} {agree}
            </span>
          )}
          {votes.length > 1 && (
            <span className="small muted">
              Votes: {votes.map(([k, n]) => <span key={k} className="vote"><span className="mono">{k}</span> ×{n}</span>)}
            </span>
          )}
        </div>
      </section>

      {rule && (
        <section className="dsec">
          <h4 className="dsec-h">
            Rule applied: {rule.rule_type === "code" ? <>code rule <code>{rule.rule_code}</code></> : "rubric"}
            {rule.status && <span className={`chip ${rule.status === "draft" ? "chip-warn" : "chip-ok"}`}>{rule.status}</span>}
          </h4>
          {rule.rule_type === "code" ? (
            rule.rule_params && Object.keys(rule.rule_params).filter((k) => k !== "suggested").length > 0 && (
              <pre className="code-block">{JSON.stringify(Object.fromEntries(Object.entries(rule.rule_params).filter(([k]) => k !== "suggested")), null, 2)}</pre>
            )
          ) : (
            rule.rubric_text && (
              <details className="rubric">
                <summary>Show rubric</summary>
                <p className="prewrap">{rule.rubric_text}</p>
              </details>
            )
          )}
        </section>
      )}

      {f.facts_used.length > 0 && (
        <section className="dsec">
          <h4 className="dsec-h">Facts used</h4>
          <ul className="facts">
            {f.facts_used.map((fu) => (
              <li key={fu.fact_key} className={fu.used === false ? "unused" : ""}>
                <div className="fact-row">
                  <span className="fact-label">{fu.label}</span>
                  <span className={`mono fact-val${fu.value == null || fu.value === "" ? " is-empty" : ""}`}>{fu.value ?? "—"}</span>
                  <span className={`badge ${fu.source === "user" ? "tone-info" : fu.status === "verified" ? "tone-ok" : fu.status === "not_found" ? "tone-muted" : "tone-warn"}`}>
                    {fu.source === "user" ? "answered by you" : fu.status.replace("_", " ")}
                  </span>
                  {fu.used === false && <span className="muted small">(not cited)</span>}
                </div>
                {fu.source !== "user" && <EvidenceList items={fu.evidence} prefix={`f${f.field_id}-${fu.fact_key}`} ctx={ctx} />}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function MissingFacts({ field: f, ctx }: { field: FieldValue; ctx: RowCtx }) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const typeOf = (k: string) => f.facts.find((x) => x.fact_key === k)?.type ?? "text";
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(answers)) if (v.trim() !== "") out[k] = v.trim();
    if (!Object.keys(out).length) return;
    setBusy(true);
    try {
      await ctx.answer(f, out);
      setAnswers({});
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="missing" onSubmit={submit}>
      <h4 className="dsec-h">{f.status === "cannot_determine" ? "Can't decide yet — please answer" : "Missing facts"}</h4>
      {f.missing_facts.map((m) => {
        const id = `mf-${f.field_id}-${m.fact_key}`;
        const t = typeOf(m.fact_key);
        const val = answers[m.fact_key] ?? "";
        const set = (v: string) => setAnswers((a) => ({ ...a, [m.fact_key]: v }));
        return (
          <div key={m.fact_key} className="mf">
            <label htmlFor={id}>{m.question || `What is the ${m.label.toLowerCase()}?`}</label>
            {t === "boolean" || t === "checkbox" || t === "bool" ? (
              <select id={id} className="input" value={val} onChange={(e) => set(e.target.value)}>
                <option value="">—</option>
                <option value="yes">Yes</option>
                <option value="no">No</option>
              </select>
            ) : (
              <input
                id={id}
                className="input"
                type={t === "date" ? "date" : "text"}
                inputMode={t === "number" || t === "currency" ? "decimal" : undefined}
                value={val}
                onChange={(e) => set(e.target.value)}
                onKeyDown={(e) => e.key === "Escape" && (e.currentTarget as HTMLInputElement).blur()}
              />
            )}
          </div>
        );
      })}
      <div className="editor-actions">
        <button className="btn btn-primary btn-sm" type="submit" disabled={busy || !Object.values(answers).some((v) => v.trim())}>
          {busy ? "Re-deciding…" : "Submit answers & re-decide"}
        </button>
        <span className="muted small">Answers are saved as facts for this job.</span>
      </div>
    </form>
  );
}
