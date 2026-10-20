import { useEffect, useState } from "react";

type Kind = "info" | "error" | "warn" | "ok";
interface T { id: number; kind: Kind; text: string; detail?: string[] }
let seq = 0;
const listeners = new Set<(t: T) => void>();

export function toast(text: string, kind: Kind = "info", detail?: string[]): void {
  const t = { id: ++seq, kind, text, detail };
  listeners.forEach((l) => l(t));
}

export function ToastHost() {
  const [items, setItems] = useState<T[]>([]);
  useEffect(() => {
    const l = (t: T) => {
      setItems((xs) => [...xs.slice(-3), t]);
      const ms = t.kind === "error" || t.detail?.length ? 9000 : 3500;
      setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== t.id)), ms);
    };
    listeners.add(l);
    return () => void listeners.delete(l);
  }, []);
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`}>
          <div className="toast-row">
            <span>{t.text}</span>
            <button className="icon-btn" aria-label="Dismiss" onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}>×</button>
          </div>
          {t.detail && t.detail.length > 0 && (
            <ul className="toast-detail">{t.detail.map((d, i) => <li key={i}>{d}</li>)}</ul>
          )}
        </div>
      ))}
    </div>
  );
}
