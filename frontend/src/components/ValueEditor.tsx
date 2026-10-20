import { useEffect, useRef, useState } from "react";
import type { FieldValue } from "../api";
import { effectiveValue, isEmpty } from "../format";

interface Props {
  field: FieldValue;
  busy?: boolean;
  onSave: (value: unknown, note?: string) => void;
  onCancel: () => void;
}

/** Type-appropriate editor. Enter saves (Shift+Enter = newline in multi-line), Esc cancels. */
export function ValueEditor({ field, busy, onSave, onCancel }: Props) {
  const initial = effectiveValue(field) ?? (field.candidates[0]?.value ?? null);
  const [v, setV] = useState<unknown>(() => {
    if (field.type === "checkbox") return initial === true || initial === "true";
    if (field.type === "date") return typeof initial === "string" && /^\d{4}-\d{2}-\d{2}$/.test(initial) ? initial : "";
    return isEmpty(initial) ? "" : String(initial);
  });
  const [note, setNote] = useState("");
  const first = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const el = first.current;
    el?.focus();
    if (el instanceof HTMLInputElement && el.type === "text") el.select();
  }, []);

  const save = () => {
    let out: unknown = v;
    if (typeof out === "string") out = out.trim() === "" ? null : field.type === "multiline" ? out : out.trim();
    onSave(out, note.trim() || undefined);
  };
  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onCancel(); }
    else if (e.key === "Enter" && !e.shiftKey && !(e.nativeEvent as KeyboardEvent).isComposing) {
      e.preventDefault(); e.stopPropagation(); save();
    }
  };
  const ref = (el: HTMLElement | null) => { first.current = el; };
  const id = `edit-${field.field_id}`;

  let input: React.ReactNode;
  switch (field.type) {
    case "checkbox":
      input = (
        <label className="check-edit">
          <input ref={ref} id={id} type="checkbox" checked={v === true} onChange={(e) => setV(e.target.checked)} onKeyDown={onKey} />
          {v === true ? "Checked (Yes)" : "Unchecked (No)"}
        </label>
      );
      break;
    case "date":
      input = <input ref={ref} id={id} className="input" type="date" value={String(v)} onChange={(e) => setV(e.target.value)} onKeyDown={onKey} />;
      break;
    case "choice":
      input = (
        <select ref={ref} id={id} className="input" value={String(v)} onChange={(e) => setV(e.target.value)} onKeyDown={onKey}>
          <option value="">— none —</option>
          {field.options.map((o) => <option key={o} value={o}>{o}</option>)}
          {!isEmpty(v) && !field.options.includes(String(v)) && <option value={String(v)}>{String(v)} (not an option)</option>}
        </select>
      );
      break;
    case "multiline":
      input = <textarea ref={ref} id={id} className="input" rows={4} value={String(v)} onChange={(e) => setV(e.target.value)} onKeyDown={onKey} />;
      break;
    default:
      input = (
        <input
          ref={ref}
          id={id}
          className="input mono"
          type={field.type === "email" ? "email" : field.type === "phone" ? "tel" : "text"}
          inputMode={field.type === "number" || field.type === "currency" ? "decimal" : undefined}
          placeholder={field.format_hint ?? undefined}
          value={String(v)}
          onChange={(e) => setV(e.target.value)}
          onKeyDown={onKey}
        />
      );
  }

  return (
    <div className="editor" onClick={(e) => e.stopPropagation()}>
      <label htmlFor={id} className="sr-only">New value for {field.label}</label>
      {input}
      {field.kind === "judgment" && (
        <input
          className="input"
          placeholder="Why? (optional — helps improve the rule)"
          aria-label="Reason for changing this judgment (optional)"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={onKey}
        />
      )}
      <div className="editor-actions">
        <button className="btn btn-primary btn-sm" onClick={save} disabled={busy}>Save <kbd>Enter</kbd></button>
        <button className="btn btn-sm btn-ghost" onClick={onCancel}>Cancel <kbd>Esc</kbd></button>
        {field.type === "multiline" && <span className="muted small">Shift+Enter for a new line</span>}
        {field.format_hint && field.type !== "date" && <span className="muted small">Format: {field.format_hint}</span>}
      </div>
    </div>
  );
}
