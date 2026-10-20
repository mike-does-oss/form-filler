import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Bbox } from "../api";

export interface PageDim { page: number; width?: number; height?: number }
export interface Overlay {
  id: string | number;
  page: number;
  boxes: Bbox[];
  tone?: "hit" | "alt" | "field" | "field-active";
  title?: string;
  onClick?: () => void;
}
export interface ScrollTarget { page: number; box?: Bbox | null; nonce: number }

interface Props {
  pages: PageDim[];
  imgUrl: (page: number, dpi: number) => string;
  overlays?: Overlay[];
  target?: ScrollTarget | null;
  /** Shown in the toolbar left of the page controls (e.g. a document picker). */
  toolbarStart?: React.ReactNode;
  ariaLabel: string;
}

const DPI_STEPS = [72, 110, 150, 200, 250];
const ZOOMS = [0.5, 0.67, 0.8, 1, 1.25, 1.5, 2, 2.5, 3];

/** Continuous vertical stack of page PNGs with a box overlay in PDF points (top-left origin). */
export function PageStack({ pages, imgUrl, overlays = [], target, toolbarStart, ariaLabel }: Props) {
  const scroller = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(600);
  const [zoom, setZoom] = useState(1);
  const [current, setCurrent] = useState(pages[0]?.page ?? 1);
  const [measured, setMeasured] = useState<Record<number, { w: number; h: number }>>({});

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const dims = useCallback(
    (p: PageDim) => {
      const m = measured[p.page];
      const w = p.width ?? m?.w ?? 612;
      const h = p.height ?? m?.h ?? 792;
      return { w, h };
    },
    [measured],
  );

  const pageCss = Math.max(200, (width - 24) * zoom);
  const dpr = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;

  const pageEls = useRef(new Map<number, HTMLDivElement>());

  const scrollToPage = useCallback((page: number, box?: Bbox | null, smooth = true) => {
    const el = scroller.current;
    const pe = pageEls.current.get(page);
    if (!el || !pe) return;
    const p = pages.find((x) => x.page === page);
    const { w } = p ? dims(p) : { w: 612 };
    const scale = pe.clientWidth / w;
    let top = pe.offsetTop - 8;
    let left = 0;
    if (box) {
      top = pe.offsetTop + box[1] * scale - el.clientHeight * 0.3;
      const cx = pe.offsetLeft + ((box[0] + box[2]) / 2) * scale;
      left = cx - el.clientWidth / 2;
    }
    el.scrollTo({ top: Math.max(0, top), left: Math.max(0, left), behavior: smooth ? "smooth" : "auto" });
  }, [pages, dims]);

  // Jump to a target whenever its nonce changes (wait a frame so new pages are laid out).
  useEffect(() => {
    if (!target) return;
    const id = requestAnimationFrame(() => scrollToPage(target.page, target.box ?? null));
    return () => cancelAnimationFrame(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.nonce, target?.page, pages.length]);

  // Track which page is in view.
  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const mid = el.scrollTop + el.clientHeight * 0.35;
    let cur = pages[0]?.page ?? 1;
    for (const p of pages) {
      const pe = pageEls.current.get(p.page);
      if (pe && pe.offsetTop <= mid) cur = p.page;
    }
    setCurrent(cur);
  };

  const idx = pages.findIndex((p) => p.page === current);
  const go = (d: number) => {
    const p = pages[Math.max(0, Math.min(pages.length - 1, idx + d))];
    if (p) scrollToPage(p.page);
  };
  const zi = ZOOMS.findIndex((z) => z >= zoom - 1e-6);
  const setZ = (z: number) => {
    // Keep the current scroll position proportional.
    const el = scroller.current;
    const ratio = el ? el.scrollTop / Math.max(1, el.scrollHeight) : 0;
    setZoom(z);
    requestAnimationFrame(() => {
      if (el) el.scrollTop = ratio * el.scrollHeight;
    });
  };

  return (
    <div className="pagestack">
      <div className="viewer-toolbar">
        {toolbarStart}
        <div className="vt-group" role="group" aria-label="Pages">
          <button className="icon-btn" onClick={() => go(-1)} disabled={idx <= 0} aria-label="Previous page" title="Previous page">‹</button>
          <span className="mono small vt-page">{current} / {pages.length || 0}</span>
          <button className="icon-btn" onClick={() => go(1)} disabled={idx >= pages.length - 1} aria-label="Next page" title="Next page">›</button>
        </div>
        <div className="vt-group" role="group" aria-label="Zoom">
          <button className="icon-btn" onClick={() => setZ(ZOOMS[Math.max(0, zi - 1)])} disabled={zi <= 0} aria-label="Zoom out" title="Zoom out">−</button>
          <button className="btn btn-sm btn-ghost mono vt-zoom" onClick={() => setZ(1)} title="Fit width">{Math.round(zoom * 100)}%</button>
          <button className="icon-btn" onClick={() => setZ(ZOOMS[Math.min(ZOOMS.length - 1, zi + 1)])} disabled={zi >= ZOOMS.length - 1} aria-label="Zoom in" title="Zoom in">+</button>
        </div>
      </div>
      <div className="pages-scroll" ref={scroller} onScroll={onScroll} aria-label={ariaLabel} tabIndex={0}>
        {pages.map((p) => {
          const { w, h } = dims(p);
          const want = (pageCss * dpr * 72) / w;
          const dpi = DPI_STEPS.find((d) => d >= want) ?? DPI_STEPS[DPI_STEPS.length - 1];
          const ovs = overlays.filter((o) => o.page === p.page);
          return (
            <div
              key={p.page}
              className="page-frame"
              ref={(el) => {
                if (el) pageEls.current.set(p.page, el);
                else pageEls.current.delete(p.page);
              }}
              style={{ width: pageCss, aspectRatio: `${w} / ${h}` }}
            >
              <img
                src={imgUrl(p.page, dpi)}
                alt={`Page ${p.page}`}
                loading="lazy"
                draggable={false}
                onLoad={(e) => {
                  if (p.width && p.height) return;
                  const im = e.currentTarget;
                  const mw = (im.naturalWidth * 72) / dpi;
                  const mh = (im.naturalHeight * 72) / dpi;
                  setMeasured((m) => (m[p.page] && Math.abs(m[p.page].w - mw) < 1 ? m : { ...m, [p.page]: { w: mw, h: mh } }));
                }}
              />
              {ovs.length > 0 && (
                <svg className="overlay" viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" aria-hidden="true">
                  {ovs.map((o) =>
                    o.boxes.map((b, i) => (
                      <rect
                        key={`${o.id}-${i}`}
                        className={`ov ov-${o.tone ?? "hit"}${o.onClick ? " clickable" : ""}`}
                        x={b[0] - 1.5}
                        y={b[1] - 1.5}
                        width={Math.max(2, b[2] - b[0] + 3)}
                        height={Math.max(2, b[3] - b[1] + 3)}
                        rx={2}
                        onClick={o.onClick}
                      >
                        {o.title ? <title>{o.title}</title> : null}
                      </rect>
                    )),
                  )}
                </svg>
              )}
              <span className="page-num" aria-hidden="true">{p.page}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
