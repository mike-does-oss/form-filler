import type { FieldValue } from "./api";

/** The value that would go into the form right now. */
export function effectiveValue(f: FieldValue): unknown {
  if (f.review_status === "accepted" || f.review_status === "edited") return f.final_value;
  if (f.review_status === "not_applicable") return null;
  return f.value;
}

export function isEmpty(v: unknown): boolean {
  return v === null || v === undefined || v === "";
}

export function fmtValue(v: unknown, type?: string): string {
  if (isEmpty(v)) return "";
  if (type === "checkbox" || typeof v === "boolean") return v === true || v === "true" ? "Yes" : "No";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function agreementText(f: FieldValue): string | null {
  if (f.run_agreement == null) return null;
  const runs = f.rule?.runs;
  if (runs && runs > 1) {
    const agreed = Math.round(f.run_agreement * runs);
    return `${agreed}/${runs} runs agreed`;
  }
  return `${Math.round(f.run_agreement * 100)}% agreement`;
}

export function isTypingTarget(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false;
  const tag = t.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || t.isContentEditable;
}
