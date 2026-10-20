import { useRef, useState } from "react";

interface Props {
  accept: string;
  multiple?: boolean;
  disabled?: boolean;
  onFiles: (files: File[]) => void;
  title: string;
  hint?: string;
  compact?: boolean;
}

export function DropZone({ accept, multiple, disabled, onFiles, title, hint, compact }: Props) {
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const exts = accept.split(",").map((s) => s.trim().toLowerCase());
  const take = (list: FileList | null) => {
    if (!list || disabled) return;
    const files = Array.from(list).filter((f) => exts.some((e) => f.name.toLowerCase().endsWith(e)));
    if (files.length) onFiles(multiple ? files : files.slice(0, 1));
  };
  return (
    <div
      className={`dropzone${over ? " over" : ""}${disabled ? " disabled" : ""}${compact ? " compact" : ""}`}
      onDragOver={(e) => { e.preventDefault(); if (!disabled) setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files); }}
    >
      <svg width="22" height="22" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.6">
        <path d="M12 16V4m0 0l-4 4m4-4l4 4M4 16v3a1 1 0 001 1h14a1 1 0 001-1v-3" />
      </svg>
      <div className="dz-text">
        <strong>{title}</strong>
        <span>
          Drag &amp; drop or{" "}
          <button type="button" className="linklike" disabled={disabled} onClick={() => input.current?.click()}>
            browse
          </button>
          {hint ? <> · {hint}</> : null}
        </span>
      </div>
      <input
        ref={input}
        type="file"
        accept={accept}
        multiple={multiple}
        hidden
        onChange={(e) => { take(e.target.files); e.target.value = ""; }}
      />
    </div>
  );
}
