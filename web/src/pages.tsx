import {
  GlobalWorkerOptions,
  RenderingCancelledException,
  getDocument,
  type PDFDocumentProxy,
  type PDFPageProxy,
  type RenderTask,
} from "pdfjs-dist";
import workerSource from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState } from "react";
import type { AreaText } from "./areas";
import type { PagePlace } from "./client";
import { reasonOf, type Loaded } from "./loading";
import { PageTranslation, type Drawn } from "./pagetranslation";
import { scrollerOf } from "./scrolling";

GlobalWorkerOptions.workerSrc = workerSource;

// Character maps, fonts and image decoders that the renderer fetches when a file needs them.
const RENDERER_FILES = "/node_modules/pdfjs-dist";
// The shape pages are laid out in until the file has told theirs, and the shape a page that
// cannot be opened keeps.
const LETTER_SHAPE = 792 / 612;
// A page is drawn once it comes this close to what its scroller shows, as a part of that
// height.
const DRAWING_MARGIN = "150% 0px";
const LEAST_SHARPNESS = 2;
// The most pixels the floor above draws a page at: a letter or A4 page at twice its printed
// size takes about 2 million, which this leaves whole.
const MOST_FLOOR_PIXELS = 4_000_000;
const BOX_CLASS = "page-box";

function percent(fraction: number): string {
  return `${fraction * 100}%`;
}

// Hands a result or a failure on only while the view that asked is still the current one. A
// file that was closed in the meantime fails what was asked of it, and that is no failure of
// the view that has gone.
function whileCurrent<Value>(
  asked: Promise<Value>,
  isCurrent: () => boolean,
  use: (value: Value) => void,
  fail: (reason: string) => void,
): Promise<void> {
  return asked.then(
    (value) => {
      if (isCurrent()) {
        use(value);
      }
    },
    (error: unknown) => {
      if (isCurrent()) {
        fail(reasonOf(error));
      }
    },
  );
}

// Opens the file at the source, and closes it when the source changes or the view goes.
function useFile(source: string): Loaded<PDFDocumentProxy> {
  const [file, setFile] = useState<Loaded<PDFDocumentProxy>>({ state: "loading" });
  useEffect(() => {
    let current = true;
    const opening = getDocument({
      url: source,
      cMapUrl: `${RENDERER_FILES}/cmaps/`,
      standardFontDataUrl: `${RENDERER_FILES}/standard_fonts/`,
      wasmUrl: `${RENDERER_FILES}/wasm/`,
      iccUrl: `${RENDERER_FILES}/iccs/`,
    });
    setFile({ state: "loading" });
    opening.promise.then(
      (value) => current && setFile({ state: "ready", value }),
      (error: unknown) => current && setFile({ state: "failed", reason: reasonOf(error) }),
    );
    return () => {
      current = false;
      void opening.destroy();
    };
  }, [source]);
  return file;
}

// How a page's translation is shown: over the page alone, over the page and the source beside
// it, or over the page with each area letting the source through when pressed.
export type Overlay = { texts: AreaText[]; paired: boolean; revealing: boolean };

function Boxes({ boxes }: { boxes: PagePlace[] }) {
  return boxes.map((box, index) => (
    <div
      key={index}
      className={BOX_CLASS}
      style={{
        left: percent(box.left),
        top: percent(box.top),
        width: percent(box.width),
        height: percent(box.height),
      }}
    />
  ));
}

function Page({
  file,
  number,
  shape,
  boxes,
  overlay,
}: {
  file: PDFDocumentProxy;
  number: number;
  shape: number;
  boxes: PagePlace[];
  overlay: Overlay | undefined;
}) {
  const frame = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const copy = useRef<HTMLCanvasElement>(null);
  const [near, setNear] = useState(false);
  const [failure, setFailure] = useState<string>();
  // The canvas of the page's latest drawing that has ended, and how many have, so that what is
  // read from a drawing is read again from the next. Nothing is set over the page without it.
  const [drawn, setDrawn] = useState<Drawn>();
  const [width, setWidth] = useState(0);
  const paired = overlay?.paired ?? false;

  useEffect(() => {
    if (frame.current === null) {
      return;
    }
    const watcher = new IntersectionObserver(
      (sightings) => setNear(sightings.at(-1)?.isIntersecting ?? false),
      { root: scrollerOf(frame.current), rootMargin: DRAWING_MARGIN },
    );
    watcher.observe(frame.current);
    // A page made narrower or wider has its translation fitted again.
    const resized = new ResizeObserver((entries) =>
      setWidth(Math.round(entries.at(-1)?.contentRect.width ?? 0)),
    );
    resized.observe(frame.current);
    return () => {
      watcher.disconnect();
      resized.disconnect();
    };
  }, []);

  useEffect(() => {
    if (!near) {
      return;
    }
    let current = true;
    let drawnPage: PDFPageProxy | undefined;
    let drawing: RenderTask | undefined;
    function draw(page: PDFPageProxy) {
      const target = canvas.current;
      if (target === null || frame.current === null) {
        return;
      }
      drawnPage = page;
      setFailure(undefined);
      const natural = page.getViewport({ scale: 1 });
      // A page is drawn no smaller than at its printed size twice over, however small it is on
      // show: the colours of a translation are read off the drawing, and drawn smaller, a thin
      // stroke covers no pixel whole and black print reads grey. That floor is bounded by a
      // count of pixels, as a file may give its pages in pixels rather than points; a page so
      // large holds its strokes at many pixels already. A page shown larger than either is
      // drawn as it is shown, as before.
      const shown = (frame.current.clientWidth / natural.width) * devicePixelRatio;
      const bounded = Math.sqrt(MOST_FLOOR_PIXELS / (natural.width * natural.height));
      const sharpness = Math.max(shown, Math.min(LEAST_SHARPNESS, bounded));
      const viewport = page.getViewport({ scale: sharpness });
      target.width = viewport.width;
      target.height = viewport.height;
      drawing = page.render({ canvas: target, viewport });
      // Leaving the page cancels its drawing, which is no failure; anything else is one.
      drawing.promise.then(
        () =>
          current &&
          setDrawn((before) => ({ canvas: target, count: (before?.count ?? 0) + 1 })),
        (error: unknown) => {
          if (!(error instanceof RenderingCancelledException)) {
            setFailure(reasonOf(error));
          }
        },
      );
    }
    void whileCurrent(file.getPage(number + 1), () => current, draw, setFailure);
    return () => {
      current = false;
      drawing?.cancel();
      setDrawn(undefined);
      // A page that has gone far gives up its drawing, which is as large as a photograph,
      // and once the drawing has ended, what the file keeps of the page's pictures; it is
      // drawn again when it comes near.
      for (const drawnCanvas of [canvas.current, copy.current]) {
        if (drawnCanvas !== null) {
          drawnCanvas.width = 0;
          drawnCanvas.height = 0;
        }
      }
      const release = () => void drawnPage?.cleanup();
      void drawing?.promise.then(release, release);
    };
  }, [near, file, number]);

  // The source beside the translated page is the same drawing, copied.
  useEffect(() => {
    const beside = copy.current;
    if (paired && drawn !== undefined && beside !== null) {
      beside.width = drawn.canvas.width;
      beside.height = drawn.canvas.height;
      beside.getContext("2d")?.drawImage(drawn.canvas, 0, 0);
    }
  }, [paired, drawn]);

  // One shape of tree whether or not the source stands beside the page, so that the page's
  // canvas, and its drawing, stay when the pairing changes.
  return (
    <div className="page-pair">
      <div
        className="page"
        hidden={!paired}
        style={{ aspectRatio: `1 / ${shape}` }}
        aria-label="原文的这一页"
      >
        <canvas ref={copy} />
        <Boxes boxes={boxes} />
      </div>
      <div className="page" ref={frame} style={{ aspectRatio: `1 / ${shape}` }}>
        <canvas ref={canvas} />
        {failure !== undefined && <p className="status failed">这一页画不出来：{failure}</p>}
        {overlay !== undefined && near && drawn !== undefined && (
          <PageTranslation
            texts={overlay.texts}
            shape={shape}
            drawn={drawn}
            revealing={overlay.revealing}
            width={width}
          />
        )}
        <Boxes boxes={boxes} />
      </div>
    </div>
  );
}

// Two pages stand side by side only where each can be read: a page 600 px wide sets the 10 pt
// body text of a letter page, 612 points across, at 9.8 px, about the smallest a reader reads
// without leaning in. The pair holds two such pages and the gap between them.
const READABLE_PAGE_WIDTH = 600;
const PAIR_GAP = 10;
const PAIRED_WIDTH = 2 * READABLE_PAGE_WIDTH + PAIR_GAP;
const NO_TEXTS: AreaText[] = [];

function Pages({
  file,
  pointed,
  inView,
  overlays,
  pairing,
}: {
  file: PDFDocumentProxy;
  pointed: PagePlace[];
  inView: boolean;
  overlays: Map<number, AreaText[]> | undefined;
  pairing: boolean;
}) {
  const viewer = useRef<HTMLDivElement>(null);
  const [shapes, setShapes] = useState<number[]>();
  const [wide, setWide] = useState(false);

  // Pages of one file may differ in shape, so the file is asked for the shape of every page,
  // once. A page that cannot be opened says why when it is drawn.
  useEffect(() => {
    let current = true;
    const asked = Array.from({ length: file.numPages }, (_, number) =>
      file.getPage(number + 1).then(
        (page) => {
          const natural = page.getViewport({ scale: 1 });
          return natural.height / natural.width;
        },
        () => LETTER_SHAPE,
      ),
    );
    void whileCurrent(Promise.all(asked), () => current, setShapes, () => undefined);
    return () => {
      current = false;
    };
  }, [file]);

  useEffect(() => {
    if (viewer.current === null) {
      return;
    }
    const resized = new ResizeObserver((entries) =>
      setWide((entries.at(-1)?.contentRect.width ?? 0) >= PAIRED_WIDTH),
    );
    resized.observe(viewer.current);
    return () => resized.disconnect();
  }, []);

  // Turns to the first of the boxes that were pointed at, once the pages have their shapes: a
  // page that took its shape afterwards would move the box out of where it was turned to.
  // One pointed at before the shapes were known, or while the pages were out of view, is
  // turned to when the shapes come or the pages come back.
  // The boxes of the source page kept beside each page for 对照 stand in the page while it is
  // hidden, so the box turned to is the first that is on show.
  useEffect(() => {
    if (shapes !== undefined) {
      [...(viewer.current?.querySelectorAll(`.${BOX_CLASS}`) ?? [])]
        .find((box) => box.checkVisibility())
        ?.scrollIntoView({ block: "center" });
    }
  }, [pointed, inView, shapes, pairing, wide]);

  return (
    <div className="pages" ref={viewer}>
      {Array.from({ length: file.numPages }, (_, number) => (
        <Page
          key={number}
          file={file}
          number={number}
          shape={shapes?.[number] ?? LETTER_SHAPE}
          boxes={pointed.filter((place) => place.page === number)}
          overlay={
            overlays === undefined
              ? undefined
              : {
                  texts: overlays.get(number) ?? NO_TEXTS,
                  paired: pairing && wide,
                  revealing: pairing && !wide,
                }
          }
        />
      ))}
    </div>
  );
}

// Shows a PDF page by page, outlines the boxes that were pointed at, and sets a translation
// over the pages where one is given, by page; with the source beside each page, or under each
// area, where pairing is asked for.
export function PageViewer({
  source,
  pointed,
  inView,
  overlays,
  pairing,
}: {
  source: string;
  pointed: PagePlace[];
  inView: boolean;
  overlays: Map<number, AreaText[]> | undefined;
  pairing: boolean;
}) {
  const file = useFile(source);
  return (
    <>
      {file.state === "loading" && <p className="status">正在打开原件…</p>}
      {file.state === "failed" && <p className="status failed">原件打不开：{file.reason}</p>}
      {file.state === "ready" && (
        <Pages
          file={file.value}
          pointed={pointed}
          inView={inView}
          overlays={overlays}
          pairing={pairing}
        />
      )}
    </>
  );
}

