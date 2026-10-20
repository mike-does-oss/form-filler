import { useEffect, useMemo, useRef, useState } from "react";
import { api, errMsg, FIELD_TYPES, type Bbox, type FieldDef, type Form } from "../api";
import { toast } from "../components/Toast";
import { isTypingTarget } from "../format";

const DPI = 110;
interface MF extends Partial<FieldDef> { uid: string; label: string; type: string; kind: "lookup" | "judgment"; required: boolean; page: number; bbox: Bbox | null; moved?: boolean }
type Drag =
  | { mode: "draw"; x0: number; y0: number; x1: number; y1: number }
  | { mode: "move"; uid: string; dx: number; dy: number; start: Bbox; px: number; py: number }
  | { mode: "resize"; uid: string; start: Bbox };

let uidSeq = 0;
const toMF = (f: FieldDef): MF => ({ ...f, uid: f.key, page: f.page ?? 1, bbox: f.bbox ?? null, section: f.section ?? null });

export function MapperPage({ formId }: { formId: number }) {
  const [form, setForm] = useState<Form | null>(null);
  const [fields, setFields] = useState<MF[]>([]);
  const [page, setPage] = useState(1);
  const [sel, setSel] = useState<string | null>(null);
  const [dims, setDims] = useState<Record<number, { w: number; h: number }>>({});
  const [drag, setDrag] = useState<Drag | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const svgRef = useRef<SVGSVGElement>(null);

  const load = async () => {
    try {
      const f = await api.getForm(formId);
      setForm(f);
      setFields(f.fields.map(toMF));
      setDirty(false);
    } catch (e) {
      toast(errMsg(e), "error");
    }
  };
  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [formId]);

  const d = dims[page] ?? { w: 612, h: 792 };
  const onPage = fields.filter((f) => f.page === page && f.bbox);
  const selected = fields.find((f) => f.uid === sel) ?? null;
  const readOnly = form ? form.form_type !== "flat_pdf" : true;

  const update = (uid: string, p: Partial<MF>) => {
    setFields((fs) => fs.map((f) => (f.uid === uid ? { ...f, ...p } : f)));
    setDirty(true);
  };
  const remove = (uid: string) => {
    setFields((fs) => fs.filter((f) => f.uid !== uid));
    setSel(null);
    setDirty(true);
  };

  // Keyboard: Delete removes the selected box, Esc deselects, arrows nudge.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isTypingTarget(e.target) || readOnly) return;
      if (!sel) return;
      if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); remove(sel); }
      else if (e.key === "Escape") setSel(null);
      else if (e.key.startsWith("Arrow")) {
        const f = fields.find((x) => x.uid === sel);
        if (!f?.bbox) return;
        e.preventDefault();
        const step = e.shiftKey ? 5 : 1;
        const [dx, dy] = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key] ?? [0, 0];
        const b = f.bbox;
        update(sel, { bbox: [b[0] + dx, b[1] + dy, b[2] + dx, b[3] + dy], moved: true });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sel, fields, readOnly]);

  const pt = (e: React.PointerEvent): [number, number] => {
    const r = svgRef.current!.getBoundingClientRect();
    return [((e.clientX - r.left) / r.width) * d.w, ((e.clientY - r.top) / r.height) * d.h];
  };
  const clamp = (b: Bbox): Bbox => [Math.max(0, b[0]), Math.max(0, b[1]), Math.min(d.w, b[2]), Math.min(d.h, b[3])];

  const onDown = (e: React.PointerEvent) => {
    if (readOnly || e.button !== 0) return;
    const [x, y] = pt(e);
    const t = e.target as Element;
    const uid = t.getAttribute("data-uid");
    const handle = t.getAttribute("data-handle");
    svgRef.current!.setPointerCapture(e.pointerId);
    if (uid) {
      const f = fields.find((ff) => ff.uid === uid)!;
      setSel(uid);
      if (handle) setDrag({ mode: "resize", uid, start: f.bbox! });
      else setDrag({ mode: "move", uid, dx: 0, dy: 0, start: f.bbox!, px: x, py: y });
    } else {
      setSel(null);
      setDrag({ mode: "draw", x0: x, y0: y, x1: x, y1: y });
    }
  };
  const onMove = (e: React.PointerEvent) => {
    if (!drag) return;
    const [x, y] = pt(e);
    if (drag.mode === "draw") setDrag({ ...drag, x1: x, y1: y });
    else if (drag.mode === "move") {
      const dx = x - drag.px, dy = y - drag.py;
      const s = drag.start;
      setFields((fs) => fs.map((f) => (f.uid === drag.uid ? { ...f, bbox: [s[0] + dx, s[1] + dy, s[2] + dx, s[3] + dy], moved: true } : f)));
      if (Math.abs(dx) + Math.abs(dy) > 0.5) setDirty(true);
    } else {
      const s = drag.start;
      setFields((fs) => fs.map((f) => (f.uid === drag.uid ? { ...f, bbox: [s[0], s[1], Math.max(s[0] + 6, x), Math.max(s[1] + 6, y)], moved: true } : f)));
      setDirty(true);
    }
  };
  const onUp = () => {
    if (drag?.mode === "draw") {
      const b: Bbox = clamp([Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1), Math.max(drag.x0, drag.x1), Math.max(drag.y0, drag.y1)]);
      if (b[2] - b[0] > 6 && b[3] - b[1] > 6) {
        const uid = `new-${++uidSeq}`;
        const n = fields.filter((f) => !f.key).length + 1;
        setFields((fs) => [...fs, { uid, label: `New field ${n}`, type: "text", kind: "lookup", required: false, page, bbox: b, options: [], section: null }]);
        setSel(uid);
        setDirty(true);
        setTimeout(() => document.getElementById("mf-label")?.focus(), 30);
      }
    } else if (drag) {
      setFields((fs) => fs.map((f) => (f.uid === drag.uid && f.bbox ? { ...f, bbox: clamp(f.bbox) } : f)));
    }
    setDrag(null);
  };

  const save = async () => {
    if (!form) return;
    setBusy("save");
    try {
      const sorted = [...fields].sort((a, b) => a.page - b.page || (a.bbox?.[1] ?? 0) - (b.bbox?.[1] ?? 0) || (a.bbox?.[0] ?? 0) - (b.bbox?.[0] ?? 0));
      const body: Partial<FieldDef>[] = sorted.map((f, i) => {
        const out: Partial<FieldDef> = {
          label: f.label, type: f.type, kind: f.kind, required: f.required, page: f.page,
          bbox: f.bbox ? (f.bbox.map((v) => Math.round(v * 10) / 10) as Bbox) : null,
          section: f.section ?? null, options: f.options ?? [], order_index: i,
        };
        if (f.key) out.key = f.key;
        if (f.key && f.moved) out.source_ref = null; // regenerate the overlay ref for the new position
        return out;
      });
      const nf = await api.putFields(form.id, body, true);
      setForm(nf);
      setFields(nf.fields.map(toMF));
      setSel(null);
      setDirty(false);
      toast(`Saved mapping: ${nf.fields.length} fields`, "ok");
    } catch (e) {
      toast(`Save failed: ${errMsg(e)}`, "error");
    } finally {
      setBusy(null);
    }
  };
  const redetect = async () => {
    if (!form || !confirm("Re-run automatic detection? This replaces the current mapping, including manual boxes.")) return;
    setBusy("detect");
    try {
      const nf = await api.detect(form.id);
      setForm(nf);
      setFields(nf.fields.map(toMF));
      setSel(null);
      setDirty(false);
      toast(`Detected ${nf.fields.length} fields`, "ok");
    } catch (e) {
      toast(errMsg(e), "error");
    } finally {
      setBusy(null);
    }
  };

  const pageList = useMemo(() => Array.from({ length: form?.page_count ?? 0 }, (_, i) => i + 1), [form]);
  if (!form) return <div className="page"><div className="busy"><span className="spinner" /> Loading form…</div></div>;

  const draw = drag?.mode === "draw" ? [Math.min(drag.x0, drag.x1), Math.min(drag.y0, drag.y1), Math.abs(drag.x1 - drag.x0), Math.abs(drag.y1 - drag.y0)] : null;
  const hs = 7 * (d.w / 612); // handle size in points

  return (
    <div className="mapper">
      <div className="review-bar">
        <div className="rb-title">
          <a href={form.template_id ? `#/templates/${form.template_id}` : "#/"} className="muted small">← {form.template_id ? "Template" : "Jobs"}</a>
          <h1 className="h1 ellipsis">Field mapping · {form.filename}</h1>
        </div>
        <div className="rb-actions">
          {dirty && <span className="small">Unsaved changes</span>}
          {!readOnly && <button className="btn btn-sm btn-ghost" onClick={redetect} disabled={!!busy}>{busy === "detect" ? "Detecting…" : "Re-detect"}</button>}
          {!readOnly && <button className="btn btn-sm btn-ghost" onClick={load} disabled={!dirty || !!busy}>Discard</button>}
          {!readOnly && <button className="btn btn-sm btn-primary" onClick={save} disabled={!dirty || !!busy}>{busy === "save" ? "Saving…" : "Save mapping"}</button>}
        </div>
      </div>
      {readOnly && <div className="callout callout-info">This is a {form.form_type} form: its fields come from the document itself and are shown read-only.</div>}
      <div className="mapper-split">
        <div className="mapper-canvas">
          <div className="viewer-toolbar">
            <div className="vt-group">
              <button className="icon-btn" onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1} aria-label="Previous page">‹</button>
              <span className="mono small vt-page">{page} / {form.page_count}</span>
              <button className="icon-btn" onClick={() => setPage((p) => Math.min(form.page_count, p + 1))} disabled={page >= form.page_count} aria-label="Next page">›</button>
            </div>
            <div className="vt-group">
              <button className="icon-btn" onClick={() => setZoom((z) => Math.max(0.5, z - 0.25))} aria-label="Zoom out">−</button>
              <button className="btn btn-sm btn-ghost mono vt-zoom" onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</button>
              <button className="icon-btn" onClick={() => setZoom((z) => Math.min(3, z + 0.25))} aria-label="Zoom in">+</button>
            </div>
            {!readOnly && <span className="muted small">Drag on the page to draw a field. Drag a box to move it, its corner to resize. Delete removes, arrows nudge.</span>}
          </div>
          <div className="pages-scroll">
            <div className="page-frame mapper-page" style={{ width: `calc((100% - 24px) * ${zoom})`, aspectRatio: `${d.w} / ${d.h}` }}>
              <img
                src={api.formPageUrl(form.id, page, DPI)}
                alt={`Form page ${page}`}
                draggable={false}
                onLoad={(e) => {
                  const im = e.currentTarget;
                  setDims((m) => ({ ...m, [page]: { w: (im.naturalWidth * 72) / DPI, h: (im.naturalHeight * 72) / DPI } }));
                }}
              />
              <svg ref={svgRef} className={`overlay mapper-ov${readOnly ? "" : " editable"}`} viewBox={`0 0 ${d.w} ${d.h}`} preserveAspectRatio="none"
                onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp}>
                {onPage.map((f) => {
                  const b = f.bbox!;
                  const on = f.uid === sel;
                  return (
                    <g key={f.uid}>
                      <rect data-uid={f.uid} className={`mbox${on ? " on" : ""}${f.key ? "" : " new"}${f.kind === "judgment" ? " judge" : ""}`}
                        x={b[0]} y={b[1]} width={b[2] - b[0]} height={b[3] - b[1]}>
                        <title>{f.label}</title>
                      </rect>
                      {on && !readOnly && (
                        <rect data-uid={f.uid} data-handle="se" className="mhandle" x={b[2] - hs / 2} y={b[3] - hs / 2} width={hs} height={hs} />
                      )}
                    </g>
                  );
                })}
                {draw && <rect className="mbox drawing" x={draw[0]} y={draw[1]} width={draw[2]} height={draw[3]} />}
              </svg>
            </div>
          </div>
        </div>
        <aside className="mapper-side">
          {selected ? (
            <div className="panel">
              <div className="panel-head"><h2 className="h2">{selected.key ? "Field" : "New field"}</h2>{selected.key && <span className="mono muted tiny">{selected.key}</span>}</div>
              <label className="block">Label
                <input id="mf-label" className="input" value={selected.label} disabled={readOnly} onChange={(e) => update(selected.uid, { label: e.target.value })} />
              </label>
              <div className="row2">
                <label className="block">Type
                  <select className="input" value={selected.type} disabled={readOnly} onChange={(e) => update(selected.uid, { type: e.target.value })}>
                    {FIELD_TYPES.map((t) => <option key={t}>{t}</option>)}
                  </select>
                </label>
                <label className="block">Kind
                  <select className="input" value={selected.kind} disabled={readOnly} onChange={(e) => update(selected.uid, { kind: e.target.value as MF["kind"] })}>
                    <option value="lookup">lookup</option>
                    <option value="judgment">judgment</option>
                  </select>
                </label>
              </div>
              <label className="block">Section
                <input className="input" value={selected.section ?? ""} disabled={readOnly} onChange={(e) => update(selected.uid, { section: e.target.value || null })} />
              </label>
              {selected.type === "choice" && (
                <label className="block">Options (one per line)
                  <textarea className="input" rows={3} disabled={readOnly} value={(selected.options ?? []).join("\n")}
                    onChange={(e) => update(selected.uid, { options: e.target.value.split("\n").map((s) => s.trim()).filter(Boolean) })} />
                </label>
              )}
              <label className="check"><input type="checkbox" checked={selected.required} disabled={readOnly} onChange={(e) => update(selected.uid, { required: e.target.checked })} /> Required</label>
              {selected.bbox && <div className="mono muted tiny">p.{selected.page} [{selected.bbox.map((v) => v.toFixed(0)).join(", ")}] pt</div>}
              {!readOnly && <button className="btn btn-sm danger" onClick={() => remove(selected.uid)}>Delete field <kbd>Del</kbd></button>}
            </div>
          ) : (
            <div className="panel muted small">Select a box to edit it{readOnly ? "" : ", or drag on the page to add a field"}.</div>
          )}
          <div className="panel">
            <div className="panel-head"><h2 className="h2">Page {page}</h2><span className="muted small">{onPage.length} of {fields.length} fields</span></div>
            <ul className="mlist">
              {onPage.map((f) => (
                <li key={f.uid}>
                  <button className={f.uid === sel ? "on" : ""} onClick={() => setSel(f.uid)}>
                    <span className="ellipsis">{f.label}</span>
                    <span className="mono muted tiny">{f.type}{f.key ? "" : " · new"}</span>
                  </button>
                </li>
              ))}
            </ul>
            {pageList.length > 1 && (
              <div className="pagechips">
                {pageList.map((p) => (
                  <button key={p} className={`chipbtn${p === page ? " on" : ""}`} onClick={() => setPage(p)}>
                    p.{p} <span className="muted">{fields.filter((f) => f.page === p).length}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
