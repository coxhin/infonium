import {
  GlobalWorkerOptions,
  RenderingCancelledException,
  getDocument,
  type PDFDocumentProxy,
  type PDFPageProxy,
  type RenderTask,
} from "pdfjs-dist";
import workerSource from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { memo, useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { percent, type AreaText } from "./areas";
import type { PagePlace } from "./client";
import { reasonOf, type Loaded } from "./loading";
import type { PageText } from "./pagelines";
import {
  MarksLayer,
  SelectionDrawing,
  SelectionLayer,
  type MarkState,
  type PageMark,
} from "./pagemarking";
import { glowsOn, PageGlow, useTranslatedGlow, type Glow } from "./pageglow";
import { usePageSelecting, type PageSelecting } from "./pageselecting";
import { PageTranslation, type Drawn } from "./pagetranslation";
import { scrollerOf, uncoveredTop } from "./scrolling";

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
const UNDER_CLASS = "under";
// The longest a page's lines wait to be read off its drawing for the browser to be idle, in
// milliseconds.
const READING_WAIT = 500;
const POINTED_AREA = "area.pointed";

// Where the reader stands in the pages: the page at the top of what the scroller shows them of,
// and how far down that page the view begins, as a part of its height.
export type PagePosition = { page: number; within: number };

function positionOf(viewer: Element): PagePosition | undefined {
  const top = uncoveredTop(viewer);
  const pages = [...viewer.children];
  const page = pages.findIndex((shown) => shown.getBoundingClientRect().bottom > top);
  const place = pages[page]?.getBoundingClientRect();
  return place === undefined
    ? undefined
    : { page, within: Math.max(0, (top - place.top) / place.height) };
}

function turnTo(viewer: Element, { page, within }: PagePosition) {
  const place = viewer.children[page]?.getBoundingClientRect();
  if (place !== undefined) {
    const scroller = scrollerOf(viewer) ?? document.scrollingElement;
    scroller?.scrollBy({ top: place.top - uncoveredTop(viewer) + within * place.height });
  }
}

function overlaps(one: DOMRect, other: DOMRect): boolean {
  return (
    one.left < other.right &&
    other.left < one.right &&
    one.top < other.bottom &&
    other.top < one.bottom
  );
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

// The reader's highlights as the pages draw them: on each page, and by the blocks they run
// through, for the areas of a translation; and how a selection is made, where one can be.
export type PageMarks = {
  text: PageText;
  onPages: Map<number, PageMark[]>;
  blocks: ReadonlyMap<number, MarkState>;
  selecting: PageSelecting | undefined;
};

const NO_MARKS: PageMark[] = [];

// The lines pointed at, which the pages turn to; the reader sees the passage glow instead
// (pageglow.tsx). On a page that shows the translation they lie under the translation: where
// an area holds the cited block's translation, the pages turn to it.
function Boxes({ boxes, under }: { boxes: PagePlace[]; under: boolean }) {
  return boxes.map((box, index) => (
    <div
      key={index}
      className={under ? `${BOX_CLASS} ${UNDER_CLASS}` : BOX_CLASS}
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
  pointedBlocks,
  onMarked,
  overlay,
  text,
  marks,
  markedBlocks,
  drawing,
  glow,
}: {
  file: PDFDocumentProxy;
  number: number;
  shape: number;
  boxes: PagePlace[];
  pointedBlocks: ReadonlySet<number>;
  onMarked: (area: HTMLElement) => void;
  overlay: Overlay | undefined;
  text: PageText;
  marks: PageMark[];
  markedBlocks: ReadonlyMap<number, MarkState>;
  drawing: SelectionDrawing;
  glow: Glow | undefined;
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

  // The lines of the page are read off its drawing when it arrives; the highlights are drawn
  // again whenever the lines on this page are cut anew, as when the drawing of another page a
  // block of this one runs over arrives.
  // A page that holds highlights, or a passage pointed at, is read as its drawing arrives; any
  // other on the first press on it. Reading takes some milliseconds a page, so it waits for a
  // moment the browser is idle, as while the pages scroll every frame counts.
  const holdsMarks = marks.length > 0 || glowsOn(glow, text, number);
  useEffect(() => {
    text.drawnOn(number, drawn?.canvas);
    if (!holdsMarks) {
      return;
    }
    const reading = requestIdleCallback(() => text.read(number), { timeout: READING_WAIT });
    return () => cancelIdleCallback(reading);
  }, [text, number, drawn, holdsMarks]);
  const drawings = useSyncExternalStore(text.subscribe, () => text.versionOf(number));

  // One shape of tree whether or not the source stands beside the page, so that the page's
  // canvas, and its drawing, stay when the pairing changes. Highlights and a selection are
  // drawn as lines on the source's page, and under the translation on a page that shows it.
  const translated = overlay !== undefined;
  return (
    <div className="page-pair">
      <div
        className="page"
        hidden={!paired}
        style={{ aspectRatio: `1 / ${shape}` }}
        aria-label="原文的这一页"
        data-page={number}
      >
        <canvas ref={copy} />
        {paired && (
          <MarksLayer marks={marks} text={text} page={number} drawings={drawings} under={false} />
        )}
        {paired && <SelectionLayer drawing={drawing} page={number} under={false} />}
        <Boxes boxes={boxes} under={false} />
        {paired && (
          <PageGlow glow={glow} text={text} page={number} drawings={drawings} under={false} />
        )}
      </div>
      <div
        className="page"
        ref={frame}
        style={{ aspectRatio: `1 / ${shape}` }}
        data-page={number}
        data-translated={translated ? "" : undefined}
      >
        <canvas ref={canvas} />
        <MarksLayer
          marks={marks}
          text={text}
          page={number}
          drawings={drawings}
          under={translated}
        />
        <SelectionLayer drawing={drawing} page={number} under={translated} />
        {failure !== undefined && <p className="status failed">这一页画不出来：{failure}</p>}
        {overlay !== undefined && near && drawn !== undefined && (
          <PageTranslation
            texts={overlay.texts}
            shape={shape}
            drawn={drawn}
            revealing={overlay.revealing}
            width={width}
            pointedBlocks={pointedBlocks}
            onMarked={onMarked}
            markedBlocks={markedBlocks}
            drawing={drawing}
          />
        )}
        <Boxes boxes={boxes} under={overlay !== undefined} />
        <PageGlow glow={glow} text={text} page={number} drawings={drawings} under={translated} />
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
const NO_BLOCKS: ReadonlySet<number> = new Set();

function Pages({
  file,
  pointed,
  pointedBlocks,
  glow,
  inView,
  overlays,
  pairing,
  marks,
  opening,
  onTurned,
}: {
  file: PDFDocumentProxy;
  pointed: PagePlace[];
  pointedBlocks: ReadonlySet<number>;
  glow: Glow | undefined;
  inView: boolean;
  overlays: Map<number, AreaText[]> | undefined;
  pairing: boolean;
  marks: PageMarks;
  opening: PagePosition | undefined;
  onTurned: ((position: PagePosition) => void) | undefined;
}) {
  const viewer = useRef<HTMLDivElement>(null);
  const [drawing] = useState(() => new SelectionDrawing());
  usePageSelecting(viewer, drawing, marks.selecting, inView);
  useTranslatedGlow(viewer, glow, inView);
  const [shapes, setShapes] = useState<number[]>();
  const [wide, setWide] = useState(false);
  // The outline turned to under the translation before the area over it was set: the area is
  // turned to when it comes.
  const awaited = useRef<Element>(undefined);
  const marked = useCallback((area: HTMLElement) => {
    const box = awaited.current;
    if (
      box !== undefined &&
      box.isConnected &&
      overlaps(area.getBoundingClientRect(), box.getBoundingClientRect())
    ) {
      awaited.current = undefined;
      area.scrollIntoView({ block: "center" });
    }
  }, []);

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

  // The pages open where they were asked to, once they have their shapes and are on show,
  // unless a passage is pointed at then, which they turn to instead.
  const opened = useRef(false);
  useEffect(() => {
    if (opening !== undefined && !opened.current && shapes !== undefined && inView) {
      opened.current = true;
      if (pointed.length === 0 && viewer.current !== null) {
        turnTo(viewer.current, opening);
      }
    }
  }, [opening, shapes, inView, pointed]);

  // Where the reader stands is told as the pages scroll, once a frame at most. Whatever scrolls
  // them is heard, since a change of the window's width may hand the scrolling from the
  // reader's column to the window.
  useEffect(() => {
    const shown = viewer.current;
    if (onTurned === undefined || shown === null) {
      return;
    }
    let frame = 0;
    const scrolled = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const position = positionOf(shown);
        if (position !== undefined) {
          onTurned(position);
        }
      });
    };
    const heard = { capture: true, passive: true };
    document.addEventListener("scroll", scrolled, heard);
    return () => {
      document.removeEventListener("scroll", scrolled, heard);
      cancelAnimationFrame(frame);
    };
  }, [onTurned]);

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
  // hidden, so what is turned to is the first outline on show; where it lies under the
  // translation, the marked area over it. A block's areas can lie apart, the cited lines in one
  // of them.
  useEffect(() => {
    if (shapes !== undefined) {
      const box = [...(viewer.current?.querySelectorAll(`.${BOX_CLASS}`) ?? [])].find((each) =>
        each.checkVisibility(),
      );
      const under = box?.classList.contains(UNDER_CLASS) === true;
      const over = under
        ? [...(box.parentElement?.querySelectorAll(`.${POINTED_AREA}`) ?? [])].find((area) =>
            overlaps(area.getBoundingClientRect(), box.getBoundingClientRect()),
          )
        : undefined;
      awaited.current = under && over === undefined ? box : undefined;
      (over ?? box)?.scrollIntoView({ block: "center" });
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
          pointedBlocks={
            pointed.some((place) => place.page === number) ? pointedBlocks : NO_BLOCKS
          }
          onMarked={marked}
          text={marks.text}
          marks={marks.onPages.get(number) ?? NO_MARKS}
          markedBlocks={marks.blocks}
          drawing={drawing}
          glow={glow}
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

// Shows a PDF page by page, turns to the boxes that were pointed at and makes the passage glow
// there, draws the reader's highlights, lets the reader select by dragging over the lines, and
// sets a translation over the pages where one is given, by page; with the source beside each
// page, or under each area, where pairing is asked for. It opens at the position given, if one is, and tells where the
// reader stands as they scroll, if asked to. The reader is drawn again whenever a view hands a
// selection over, as at every frame of a scroll while one is held; the pages only when what
// they are given changes.
export const PageViewer = memo(function PageViewer({
  source,
  pointed,
  pointedBlocks,
  glow,
  inView,
  overlays,
  pairing,
  marks,
  opening,
  onTurned,
}: {
  source: string;
  pointed: PagePlace[];
  // The blocks the passage pointed at runs through, whose translation's areas the pages turn
  // to.
  pointedBlocks: ReadonlySet<number>;
  // The passage pointed at, as it glows.
  glow: Glow | undefined;
  inView: boolean;
  overlays: Map<number, AreaText[]> | undefined;
  pairing: boolean;
  marks: PageMarks;
  opening: PagePosition | undefined;
  onTurned: ((position: PagePosition) => void) | undefined;
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
          pointedBlocks={pointedBlocks}
          glow={glow}
          inView={inView}
          overlays={overlays}
          pairing={pairing}
          marks={marks}
          opening={opening}
          onTurned={onTurned}
        />
      )}
    </>
  );
});
