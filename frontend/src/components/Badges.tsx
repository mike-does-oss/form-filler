import type { Confidence, ReviewStatus, Status } from "../api";

const STATUS: Record<Status, { label: string; icon: string; tone: string; tip: string }> = {
  verified: { label: "Verified", icon: "✓", tone: "ok", tip: "Value found verbatim in a source quote" },
  unverified: { label: "Unverified", icon: "?", tone: "warn", tip: "Model answer could not be matched to the quoted source text" },
  derived: { label: "Derived", icon: "ƒ", tone: "info", tip: "Calculated from other values in the sources" },
  conflict: { label: "Conflict", icon: "⇄", tone: "bad", tip: "Sources disagree; pick a candidate" },
  not_found: { label: "Not found", icon: "∅", tone: "muted", tip: "No value found in the sources" },
  judged: { label: "Judged", icon: "⚖", tone: "judge", tip: "Decided by a judgment rule; always needs review" },
  cannot_determine: { label: "Can't determine", icon: "?", tone: "bad", tip: "Facts are missing; answer the questions" },
};

export function StatusBadge({ status }: { status: Status | null }) {
  if (!status) return <span className="badge tone-muted">No value</span>;
  const s = STATUS[status] ?? { label: status, icon: "", tone: "muted", tip: "" };
  return (
    <span className={`badge tone-${s.tone}`} title={s.tip}>
      <span aria-hidden="true" className="badge-icon">{s.icon}</span>
      {s.label}
    </span>
  );
}

const CONF: Record<Confidence, { label: string; bars: number }> = {
  high: { label: "High", bars: 3 },
  medium: { label: "Med", bars: 2 },
  low: { label: "Low", bars: 1 },
};

export function ConfidenceBadge({ confidence }: { confidence: Confidence | null }) {
  if (!confidence) return null;
  const c = CONF[confidence];
  return (
    <span className={`conf conf-${confidence}`} title={`${c.label === "Med" ? "Medium" : c.label} confidence`}>
      <span className="conf-bars" aria-hidden="true">
        {[1, 2, 3].map((i) => <i key={i} className={i <= c.bars ? "on" : ""} />)}
      </span>
      {c.label}
    </span>
  );
}

const REVIEW: Record<ReviewStatus, { label: string; icon: string }> = {
  pending: { label: "To review", icon: "○" },
  accepted: { label: "Accepted", icon: "✓" },
  edited: { label: "Edited", icon: "✎" },
  not_applicable: { label: "N/A", icon: "–" },
};

export function ReviewMark({ status }: { status: ReviewStatus }) {
  const r = REVIEW[status];
  return (
    <span className={`rmark rmark-${status}`} title={r.label} aria-label={r.label}>
      {r.icon}
    </span>
  );
}

export function reviewLabel(s: ReviewStatus): string {
  return REVIEW[s].label;
}
