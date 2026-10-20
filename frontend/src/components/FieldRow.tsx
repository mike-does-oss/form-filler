import { memo } from "react";
import type { Bbox, Evidence, FieldValue, ReviewAction } from "../api";
import { ConfidenceBadge, ReviewMark, StatusBadge, reviewLabel } from "./Badges";
import { ValueEditor } from "./ValueEditor";
import { effectiveValue, fmtValue, isEmpty } from "../format";
import { JudgmentDetail } from "./JudgmentDetail";

export interface Jumpable {
  document_id?: number | null;
  page?: number | null;
  boxes?: Bbox[] | null;
  bbox?: Bbox | null;
}
export type JumpFn = (ev: Jumpable, key: string) => void;

export interface RowCtx {
  docName: (id: number | null | undefined) => string;
  jump: JumpFn;
  activeKey: string | null;
  busy: boolean;
  act: (f: FieldValue, action: ReviewAction, extra?: { value?: unknown; candidate_index?: number; note?: string }) => void;
  answer: (f: FieldValue, answers: Record<string, unknown>) => Promise<void>;
  templateId: number | null;
}

interface Props {
  field: FieldValue;
  selected: boolean;
  editing: boolean;
  onSelect: (f: FieldValue) => void;
  setEditing: (id: number | null) => void;
  ctx: RowCtx;
}

export const FieldRow = memo(function FieldRow({ field: f, selected, editing, onSelect, setEditing, ctx }: Props) {
  const ev = effectiveValue(f);
  const sig = f.requires_manual_signing;
  const shown = fmtValue(ev, f.type);
  return (
    <li
      className={`frow${selected ? " selected" : ""} rs-${f.review_status}${f.kind === "judgment" ? " judgment" : ""}`}
      data-field-id={f.field_id}
      aria-selected={selected}
      role="option"
    >
      <button type="button" className="frow-head" onClick={() => onSelect(f)} aria-expanded={selected}>
        <ReviewMark status={f.review_status} />
        <span className="frow-label">
          <span className="frow-label-text">{f.label}</span>
          {f.required && <span className="req" title="Required" aria-label="required">*</span>}
          {f.kind === "judgment" && <span className="chip chip-judge" title="Judgment field: decided by a rule, always reviewed by a person">⚖ judgment</span>}
        </span>
        <span className={`frow-value mono${isEmpty(ev) ? " is-empty" : ""}${f.review_status === "not_applicable" ? " na" : ""}`}>
          {sig ? <span className="muted">✍ sign manually</span>
            : f.review_status === "not_applicable" ? "N/A"
            : isEmpty(ev) ? "—"
            : f.type === "checkbox" ? (ev === true || ev === "true" ? "☑ Yes" : "☐ No")
            : shown}
        </span>
        <span className="frow-badges">
          {!sig && <StatusBadge status={f.status} />}
          {!sig && <ConfidenceBadge confidence={f.confidence} />}
          {!sig && f.validator_errors.length > 0 && <span className="badge tone-warn" title={f.validator_errors.join("\n")}>⚠ {f.validator_errors.length}</span>}
        </span>
      </button>
      {selected && (
        <FieldDetail field={f} editing={editing} setEditing={setEditing} ctx={ctx} />
      )}
    </li>
  );
});

function FieldDetail({ field: f, editing, setEditing, ctx }: { field: FieldValue; editing: boolean; setEditing: (id: number | null) => void; ctx: RowCtx }) {
  const sig = f.requires_manual_signing;
  const reviewed = f.review_status !== "pending";
  const modelShown = fmtValue(f.value, f.type);
  return (
    <div className="fdetail">
      <div className="fmeta">
        <span className="mono muted small">{f.key}</span>
        <span className="muted small">· {f.type}</span>
        {f.section && <span className="muted small">· {f.section}</span>}
        {f.page && <span className="muted small">· form p.{f.page}</span>}
        <span className="muted small">· {reviewLabel(f.review_status)}</span>
      </div>

      {f.format_hint && <div className="small"><span className="muted">Format:</span> {f.format_hint}</div>}
      {f.instructions && (
        <details className="instructions">
          <summary>Form instructions</summary>
          <p>{f.instructions}</p>
        </details>
      )}

      {!sig && f.validator_errors.length > 0 && (
        <ul className="verrs" aria-label="Validation problems">
          {f.validator_errors.map((e, i) => <li key={i}><span aria-hidden="true">⚠</span> {e}</li>)}
        </ul>
      )}

      {sig ? (
        <div className="callout callout-info">✍ <strong>Requires manual signing.</strong> Signature fields are never filled automatically.</div>
      ) : (
        <>
          {f.user_edited && (
            <div className="small model-was">
              Model value: <span className="mono">{isEmpty(f.value) ? "—" : modelShown}</span>
            </div>
          )}

          {editing ? (
            <ValueEditor
              field={f}
              busy={ctx.busy}
              onCancel={() => setEditing(null)}
              onSave={(value, note) => ctx.act(f, "edit", { value, note })}
            />
          ) : (
            <div className="actions" role="group" aria-label="Review actions">
              {f.review_status !== "edited" && (
                <button className="btn btn-sm btn-accept" disabled={ctx.busy} onClick={() => ctx.act(f, "accept")}
                  title={isEmpty(f.value) ? "Accept the empty value" : "Accept the model value"}>
                  ✓ Accept{f.review_status === "not_applicable" ? " model value" : ""} <kbd>A</kbd>
                </button>
              )}
              <button className="btn btn-sm" disabled={ctx.busy} onClick={() => setEditing(f.field_id)}>✎ Edit <kbd>E</kbd></button>
              <button className="btn btn-sm" disabled={ctx.busy} onClick={() => ctx.act(f, "not_applicable")}>N/A <kbd>N</kbd></button>
              {reviewed && (
                <button className="btn btn-sm btn-ghost" disabled={ctx.busy} onClick={() => ctx.act(f, "reset")}>↺ Reset <kbd>R</kbd></button>
              )}
            </div>
          )}

          {f.candidates.length > 0 && (
            <section className="dsec">
              <h4 className="dsec-h">{f.status === "conflict" ? "Conflicting candidates — pick one" : "Candidates"}</h4>
              <ol className="cands">
                {f.candidates.map((c, i) => {
                  const chosen = reviewed && fmtValue(f.final_value, f.type) === fmtValue(c.value, f.type);
                  return (
                    <li key={i} className={chosen ? "chosen" : ""}>
                      <div className="cand-row">
                        <span className="mono cand-val">{fmtValue(c.value, f.type) || "—"}</span>
                        <button className="btn btn-sm" disabled={ctx.busy} onClick={() => ctx.act(f, "choose_candidate", { candidate_index: i })}>
                          {chosen ? "✓ Chosen" : "Use this"} {i < 9 && <kbd>{i + 1}</kbd>}
                        </button>
                      </div>
                      <EvidenceList items={c.evidence} prefix={`c${f.field_id}-${i}`} ctx={ctx} />
                    </li>
                  );
                })}
              </ol>
            </section>
          )}

          {f.derivation && (
            <section className="dsec">
              <h4 className="dsec-h">Derived: <span className="mono">{f.derivation.op}</span></h4>
              <ul className="deriv">
                {f.derivation.inputs.map((inp, i) => {
                  const k = `d${f.field_id}-${i}`;
                  const can = inp.document_id != null;
                  return (
                    <li key={i}>
                      <button className={`evlink${ctx.activeKey === k ? " active" : ""}`} disabled={!can} onClick={() => ctx.jump(inp, k)}>
                        <span className="mono">{fmtValue(inp.value) || "—"}</span>
                        {inp.quote && <q>{inp.quote}</q>}
                        {can && <span className="muted small">{ctx.docName(inp.document_id)} p.{inp.page}</span>}
                        {inp.verified === false && <span className="badge tone-warn">unverified</span>}
                      </button>
                    </li>
                  );
                })}
              </ul>
              <div className="small">= <span className="mono">{fmtValue(f.derivation.result) || "—"}</span> <span className="muted">(computed by the app, not the model)</span></div>
            </section>
          )}

          {f.kind === "judgment" ? (
            <JudgmentDetail field={f} ctx={ctx} />
          ) : (
            f.evidence.length > 0 && (
              <section className="dsec">
                <h4 className="dsec-h">Evidence</h4>
                <EvidenceList items={f.evidence} prefix={`e${f.field_id}`} ctx={ctx} />
              </section>
            )
          )}
          {f.kind !== "judgment" && f.evidence.length === 0 && f.candidates.length === 0 && !f.derivation && (
            <div className="muted small">No supporting evidence in the sources.</div>
          )}
          {f.reasoning && f.kind !== "judgment" && (
            <div className="small"><span className="muted">Note:</span> {f.reasoning}</div>
          )}
        </>
      )}
    </div>
  );
}

export function EvidenceList({ items, prefix, ctx }: { items: Evidence[]; prefix: string; ctx: RowCtx }) {
  if (!items.length) return null;
  return (
    <ul className="evlist">
      {items.map((e, i) => {
        const k = `${prefix}-${i}`;
        const can = e.document_id != null && e.page != null;
        return (
          <li key={i}>
            <button className={`evlink${ctx.activeKey === k ? " active" : ""}`} disabled={!can} onClick={() => ctx.jump(e, k)}
              title={can ? "Show in source" : undefined}>
              {e.quote ? <q>{e.quote}</q> : <span className="muted">(no quote)</span>}
              <span className="evmeta">
                {can && <span className="muted small">{ctx.docName(e.document_id)} · p.{e.page}</span>}
                {e.source === "user" ? <span className="badge tone-info">user</span>
                  : e.verified ? <span className="badge tone-ok" title="Quote found in the source text">✓ quote verified</span>
                  : <span className="badge tone-warn" title="Quote could not be found in the source text">? quote not matched</span>}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
