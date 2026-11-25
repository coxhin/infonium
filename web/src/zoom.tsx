import { useEffect, useSyncExternalStore, type RefObject } from "react";
import "./zoom.css";

// How large the reader shows a source, apart from the rest of the window: the pages of a PDF by
// their width, the text as a whole. Each keeps its own zoom, which is the reader's, not an
// item's: it stays as it was set when another item is opened, and over a reload of the page.

export type ZoomKind = "pages" | "text";

// The steps the buttons go by, and the bounds a pinch or the wheel goes within.
const STEPS = [0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3];
const LEAST = 0.5;
const MOST = 3;
const NATURAL = 1;
// How much a turn of the wheel with Ctrl held, or a pinch, zooms by: a notch of a wheel, 100
// pixels, zooms by about a fifth; a pinch, which comes in small steps, as far as it goes.
const WHEEL_RATE = 0.002;
// A zoom is kept to whole percents.
const PERCENT = 100;
// The shown zoom is near enough a step to count as on it.
const NEAR_STEP = 0.005;

const listeners = new Set<() => void>();

function keyOf(kind: ZoomKind): string {
  return `infonium.zoom.${kind}`;
}

function bounded(zoom: number): number {
  return Math.round(Math.min(MOST, Math.max(LEAST, zoom)) * PERCENT) / PERCENT;
}

function storedZoom(kind: ZoomKind): number {
  const value = Number(localStorage.getItem(keyOf(kind)));
  return Number.isFinite(value) && value > 0 ? bounded(value) : NATURAL;
}

// Another window of the app that changes a zoom changes it here too.
function onZoomChange(notify: () => void): () => void {
  listeners.add(notify);
  addEventListener("storage", notify);
  return () => {
    listeners.delete(notify);
    removeEventListener("storage", notify);
  };
}

export function chooseZoom(kind: ZoomKind, zoom: number): void {
  localStorage.setItem(keyOf(kind), String(bounded(zoom)));
  listeners.forEach((notify) => notify());
}

export function useZoom(kind: ZoomKind): number {
  return useSyncExternalStore(onZoomChange, () => storedZoom(kind));
}

// The next step up or down from a zoom, which may lie between steps.
function stepped(zoom: number, up: boolean): number {
  const next = up
    ? STEPS.find((step) => step > zoom + NEAR_STEP)
    : [...STEPS].reverse().find((step) => step < zoom - NEAR_STEP);
  return next ?? (up ? MOST : LEAST);
}

// Ctrl held with the wheel, or a pinch, which the browser tells as the same, zooms what the
// element shows, and not the window; the zoom it comes to is told first, with the event, to
// whoever keeps a place still while it changes.
export function useWheelZoom(
  element: RefObject<HTMLElement | null>,
  kind: ZoomKind,
  onZooming: ((event: WheelEvent) => void) | undefined,
): void {
  useEffect(() => {
    const shown = element.current;
    if (shown === null) {
      return;
    }
    const wheeled = (event: WheelEvent) => {
      if (!event.ctrlKey) {
        return;
      }
      event.preventDefault();
      const before = storedZoom(kind);
      const after = bounded(before * Math.exp(-event.deltaY * WHEEL_RATE));
      if (after !== before) {
        onZooming?.(event);
        chooseZoom(kind, after);
      }
    };
    shown.addEventListener("wheel", wheeled, { passive: false });
    return () => shown.removeEventListener("wheel", wheeled);
  }, [element, kind, onZooming]);
}

// The zoom of what the reader shows: smaller, the zoom itself, which returns to the natural
// size when pressed, and larger.
export function ZoomControl({ kind }: { kind: ZoomKind }) {
  const zoom = useZoom(kind);
  const label = kind === "pages" ? "页面" : "文字";
  return (
    <div className="zoom-control" role="group" aria-label={`${label}大小`}>
      <button
        type="button"
        onClick={() => chooseZoom(kind, stepped(zoom, false))}
        disabled={zoom <= LEAST}
        aria-label={`缩小${label}`}
        title={`缩小${label}（也可按住 Ctrl 滚动滚轮，或双指捏合）`}
      >
        −
      </button>
      <button
        type="button"
        className="zoom-value"
        onClick={() => chooseZoom(kind, NATURAL)}
        title={`${label}恢复原大`}
        aria-label={`${label}现在是 ${Math.round(zoom * PERCENT)}%，按下恢复原大`}
      >
        {Math.round(zoom * PERCENT)}%
      </button>
      <button
        type="button"
        onClick={() => chooseZoom(kind, stepped(zoom, true))}
        disabled={zoom >= MOST}
        aria-label={`放大${label}`}
        title={`放大${label}（也可按住 Ctrl 滚动滚轮，或双指张开）`}
      >
        +
      </button>
    </div>
  );
}
