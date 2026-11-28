import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  RenderTask,
} from "pdfjs-dist";
import workerSource from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import {
  memo,
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
} from "react";
import { percent } from "./areas";
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
import {
  GoingBack,
  LinksLayer,
  useGoingBack,
  useLinkPresses,
  usePageLinks,
  type PageLink,
  type Place,
} from "./pagelinks";
import { usePageSelecting, type PageSelecting } from "./pageselecting";
import type { Drawn } from "./pagedrawing";
import { NO_PAGE_TEXTS, type PageTexts } from "./pagetexts";
import { PageTranslation } from "./pagetranslation";
import { turnTo, usePagesZoom, type PagePosition } from "./pageturning";
import { scrollerOf } from "./scrolling";

// pdf.js draws the pages. It is a third of the app's script and only a PDF needs it, so it is
// fetched apart from the rest, once, when it is first wanted. A fetch that failed is not made
// again: the browser keeps a module's failure until the page is loaded again, which the failure
// tells the reader to do.
let renderer: Promise<typeof import("pdfjs-dist")> | undefined;

export function fetchRenderer(): Promise<typeof import("pdfjs-dist")> {
  if (renderer === undefined) {
    // The import stands alone, as in formula.tsx: what is chained on it would not see every
    // failure of the build's preloading.
    const module = import("pdfjs-dist");
    renderer = module.then(
      (pdfjs) => {
        pdfjs.GlobalWorkerOptions.workerSrc = workerSource;
        return pdfjs;
      },
      () => {
        throw new Error("没能载入画页面的程序，请刷新页面再试");
      },
    );
  }
  return renderer;
}

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
// The most pixels a page is drawn at however large it is shown, as pdf.js's own viewer bounds
// it: a page zoomed further is drawn this large and stretched.
const MOST_PIXELS = 2 ** 25;
// A page whose settled width asks for a drawing this much sharper than the one it has, as
// through a zoom, is drawn again.
const BLURRED = 1.1;
// A page's width settles once it has stood still this long, in milliseconds: then a page zoomed
// in past the drawing it has is drawn again sharper, and its translation set again.
const WIDTH_SETTLES = 300;
const BOX_CLASS = "page-box";
const UNDER_CLASS = "under";
// The longest a page's lines wait to be read off its drawing for the browser to be idle, in
// milliseconds.
const READING_WAIT = 500;
// The area that holds the translation of the first line pointed at on its page.
const CITED_AREA = ".area[data-cited]";
const PAGE_CLASS_SELECTOR = ".page";

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

// Opens the file at the source, once pdf.js is here, and closes it when the source changes or
// the view goes.
function useFile(source: string): Loaded<PDFDocumentProxy> {
  const [file, setFile] = useState<Loaded<PDFDocumentProxy>>({ state: "loading" });
  useEffect(() => {
    let current = true;
    let opening: PDFDocumentLoadingTask | undefined;
    const fail = (error: unknown) =>
      current && setFile({ state: "failed", reason: reasonOf(error) });
    setFile({ state: "loading" });
    fetchRenderer().then(({ getDocument }) => {
      if (!current) {
        return;
      }
      opening = getDocument({
        url: source,
        cMapUrl: `${RENDERER_FILES}/cmaps/`,
        standardFontDataUrl: `${RENDERER_FILES}/standard_fonts/`,
        wasmUrl: `${RENDERER_FILES}/wasm/`,
        iccUrl: `${RENDERER_FILES}/iccs/`,
      });
      opening.promise.then((value) => current && setFile({ state: "ready", value }), fail);
    }, fail);
    return () => {
      current = false;
      void opening?.destroy();
    };
  }, [source]);
  return file;
}

// How a page's translation is shown: over the page alone, over the page and the source beside
// it, or over the page with each flow letting the source through when pressed.
export type Overlay = { texts: PageTexts; paired: boolean; revealing: boolean };

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

// The scale a page is drawn at for a width on show. A page is drawn no smaller than at its
// printed size twice over, however small it is on show: the colours of a translation are read
// off the drawing, and drawn smaller, a thin stroke covers no pixel whole and black print reads
// grey. That floor is bounded by a count of pixels, as a file may give its pages in pixels
// rather than points; a page so large holds its strokes at many pixels already. A page shown
// larger than either is drawn as it is shown, as before, and none larger than a bound.
function sharpnessOf(shownWidth: number, width: number, height: number): number {
  const shown = (shownWidth / width) * devicePixelRatio;
  const bounded = Math.sqrt(MOST_FLOOR_PIXELS / (width * height));
  const most = Math.sqrt(MOST_PIXELS / (width * height));
  return Math.min(most, Math.max(shown, Math.min(LEAST_SHARPNESS, bounded)));
}

// A drawing given up: its pixels, as large as a photograph's, are let go at once.
function giveUp(drawnCanvas: HTMLCanvasElement | null | undefined): void {
  if (drawnCanvas !== null && drawnCanvas !== undefined) {
    drawnCanvas.width = 0;
    drawnCanvas.height = 0;
  }
}

// The width a page last settled at, once it stood still for a moment. The first width it is
// given is taken at once; a page hidden, which is given none, keeps the width it last settled
// at, so that a zoom begun as it shows again waits as any other.
function useSettled(width: number): number {
  const [settled, setSettled] = useState(width);
  useEffect(() => {
    if (width === 0) {
      return;
    }
    setSettled((before) => (before === 0 ? width : before));
    const settling = window.setTimeout(() => setSettled(width), WIDTH_SETTLES);
    return () => window.clearTimeout(settling);
  }, [width]);
  return settled;
}

// The scale a page was last drawn at, and its printed size, which the drawing sets and a page
// that has gone far forgets.
type DrawnAt = { scale: number; width: number; height: number };

// How many times a page was drawn again to be sharp at a width it was made wider to, as by a
// zoom. A page whose settled width asks for a scale more than a tenth larger than it was drawn
// at is drawn again; a page made narrower keeps its sharper drawing, and one still drawn at
// the floor, as at a small zoom, is not drawn again at the same scale.
function useSharpening(
  near: boolean,
  settledWidth: number,
): { sharpened: number; drawnAt: { current: DrawnAt | undefined } } {
  const drawnAt = useRef<DrawnAt>(undefined);
  const [sharpened, setSharpened] = useState(0);
  useEffect(() => {
    const at = drawnAt.current;
    if (
      near &&
      at !== undefined &&
      sharpnessOf(settledWidth, at.width, at.height) > at.scale * BLURRED
    ) {
      setSharpened((count) => count + 1);
    }
  }, [near, settledWidth]);
  return { sharpened, drawnAt };
}

function Page({
  file,
  number,
  shape,
  boxes,
  onMarked,
  overlay,
  text,
  marks,
  markedBlocks,
  drawing,
  glow,
  onPlace,
  onLinks,
}: {
  file: PDFDocumentProxy;
  number: number;
  shape: number;
  boxes: PagePlace[];
  onMarked: (area: HTMLElement) => void;
  overlay: Overlay | undefined;
  text: PageText;
  marks: PageMark[];
  markedBlocks: ReadonlyMap<number, MarkState>;
  drawing: SelectionDrawing;
  glow: Glow | undefined;
  onPlace: (place: Place) => void;
  onLinks: ((page: number, links: PageLink[]) => void) | undefined;
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
  // Whether the page is near what is on show now, as the drawing's cleanup must know.
  const nearNow = useRef(false);
  const settledWidth = useSettled(width);
  const { sharpened, drawnAt } = useSharpening(near, settledWidth);
  // The page's links are read once it comes near, and told to whoever sets a translation over
  // the pages, which carries those it can.
  const links = usePageLinks(file, number, near);
  useEffect(() => {
    if (links.length > 0) {
      onLinks?.(number, links);
    }
  }, [links, number, onLinks]);

  useEffect(() => {
    if (frame.current === null) {
      return;
    }
    const watcher = new IntersectionObserver(
      (sightings) => {
        nearNow.current = sightings.at(-1)?.isIntersecting ?? false;
        setNear(nearNow.current);
      },
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
    let aside: HTMLCanvasElement | undefined;
    function draw(page: PDFPageProxy) {
      const target = canvas.current;
      if (target === null || frame.current === null) {
        return;
      }
      drawnPage = page;
      setFailure(undefined);
      const natural = page.getViewport({ scale: 1 });
      const scale = sharpnessOf(frame.current.clientWidth, natural.width, natural.height);
      drawnAt.current = { scale, width: natural.width, height: natural.height };
      const viewport = page.getViewport({ scale });
      // A page drawn again to be sharper is drawn aside, and shown once it is whole, so that it
      // never shows blank meanwhile.
      const into = target.width > 0 && sharpened > 0 ? document.createElement("canvas") : target;
      aside = into === target ? undefined : into;
      into.width = viewport.width;
      into.height = viewport.height;
      drawing = page.render({ canvas: into, viewport });
      // Leaving the page cancels its drawing, which is no failure; anything else is one.
      drawing.promise.then(
        () => {
          if (!current) {
            return;
          }
          if (into !== target) {
            target.width = into.width;
            target.height = into.height;
            target.getContext("2d")?.drawImage(into, 0, 0);
            giveUp(into);
          }
          setDrawn((before) => ({ canvas: target, count: (before?.count ?? 0) + 1 }));
        },
        (error: unknown) => {
          // pdf.js names the error a cancelled drawing fails with.
          if (!(error instanceof Error && error.name === "RenderingCancelledException")) {
            setFailure(reasonOf(error));
          }
        },
      );
    }
    void whileCurrent(file.getPage(number + 1), () => current, draw, setFailure);
    return () => {
      current = false;
      drawing?.cancel();
      // A page that has gone far gives up its drawing, which is as large as a photograph,
      // and once the drawing has ended, what the file keeps of the page's pictures; it is
      // drawn again when it comes near, at the width it then has. A page drawn again to be
      // sharper keeps its drawing until the new one is whole.
      if (!nearNow.current) {
        setDrawn(undefined);
        giveUp(canvas.current);
        giveUp(copy.current);
        drawnAt.current = undefined;
      }
      const release = () => {
        void drawnPage?.cleanup();
        giveUp(aside);
      };
      void drawing?.promise.then(release, release);
    };
  }, [near, file, number, sharpened]);
  // A page that leaves the pages gives up its drawings at once. The canvases are taken while
  // they stand, as their refs are emptied before this runs.
  useEffect(() => {
    const drawnCanvas = canvas.current;
    const copied = copy.current;
    return () => {
      giveUp(drawnCanvas);
      giveUp(copied);
    };
  }, []);

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
    <div className="page-pair" data-paired={paired ? "" : undefined}>
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
        {paired && <LinksLayer links={links} under={false} onPlace={onPlace} />}
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
            page={number}
            texts={overlay.texts}
            shape={shape}
            drawn={drawn}
            revealing={overlay.revealing}
            settledWidth={settledWidth}
            pointed={boxes}
            onMarked={onMarked}
            markedBlocks={markedBlocks}
            drawing={drawing}
          />
        )}
        <Boxes boxes={boxes} under={overlay !== undefined} />
        <LinksLayer links={links} under={translated} onPlace={onPlace} />
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
const NO_TEXTS: PageTexts = NO_PAGE_TEXTS;

function Pages({
  file,
  pointed,
  glow,
  inView,
  overlays,
  pairing,
  marks,
  opening,
  onTurned,
  onLinks,
}: {
  file: PDFDocumentProxy;
  pointed: PagePlace[];
  glow: Glow | undefined;
  inView: boolean;
  overlays: Map<number, PageTexts> | undefined;
  pairing: boolean;
  marks: PageMarks;
  opening: PagePosition | undefined;
  onTurned: ((position: PagePosition) => void) | undefined;
  onLinks: ((page: number, links: PageLink[]) => void) | undefined;
}) {
  const viewer = useRef<HTMLDivElement>(null);
  const [drawing] = useState(() => new SelectionDrawing());
  usePageSelecting(viewer, drawing, marks.selecting, inView);
  useTranslatedGlow(viewer, glow, inView);
  // A link that leads to a place of the document turns the pages there, with a way back.
  const goingBack = useGoingBack(viewer);
  useLinkPresses(viewer, goingBack.follow);
  const zoom = usePagesZoom(viewer, inView, onTurned);
  const [shapes, setShapes] = useState<number[]>();
  const [wide, setWide] = useState(false);
  // The outline turned to under the translation before the area that holds its line was set:
  // the area is turned to when it comes.
  const awaited = useRef<Element>(undefined);
  const marked = useCallback((area: HTMLElement) => {
    const box = awaited.current;
    if (
      box !== undefined &&
      box.isConnected &&
      area.closest(PAGE_CLASS_SELECTOR) === box.closest(PAGE_CLASS_SELECTOR)
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
  // translation, the area that holds the translation of its line, wherever that stands in its
  // flow.
  useEffect(() => {
    if (shapes !== undefined) {
      const box = [...(viewer.current?.querySelectorAll(`.${BOX_CLASS}`) ?? [])].find((each) =>
        each.checkVisibility(),
      );
      const under = box?.classList.contains(UNDER_CLASS) === true;
      const over = under
        ? (box.closest(PAGE_CLASS_SELECTOR)?.querySelector(CITED_AREA) ?? undefined)
        : undefined;
      awaited.current = under && over === undefined ? box : undefined;
      (over ?? box)?.scrollIntoView({ block: "center" });
    }
  }, [pointed, inView, shapes, pairing, wide]);

  const pages = (
    <div className="pages" ref={viewer} style={{ "--zoom": zoom } as CSSProperties}>
      {Array.from({ length: file.numPages }, (_, number) => (
        <Page
          key={number}
          file={file}
          number={number}
          shape={shapes?.[number] ?? LETTER_SHAPE}
          boxes={pointed.filter((place) => place.page === number)}
          onMarked={marked}
          text={marks.text}
          marks={marks.onPages.get(number) ?? NO_MARKS}
          markedBlocks={marks.blocks}
          drawing={drawing}
          glow={glow}
          onPlace={goingBack.follow}
          onLinks={onLinks}
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
  return (
    <>
      {pages}
      <GoingBack going={goingBack} />
    </>
  );
}

// Shows a PDF page by page, turns to the boxes that were pointed at and makes the passage glow
// there, draws the reader's highlights, lets the reader select by dragging over the lines, and
// sets a translation over the pages where one is given, by page; with the source beside each
// page, or under each flow, where pairing is asked for. A link of a page leads where it leads,
// with a way back from a place of the document. It opens at the position given, if one is,
// and tells where the reader stands as they scroll, if asked to; it tells the links of each
// page as they are read, if asked to. The reader is drawn again whenever a view hands a
// selection over, as at every frame of a scroll while one is held; the pages only when what
// they are given changes.
export const PageViewer = memo(function PageViewer({
  source,
  pointed,
  glow,
  inView,
  overlays,
  pairing,
  marks,
  opening,
  onTurned,
  onLinks,
}: {
  source: string;
  pointed: PagePlace[];
  // The passage pointed at, as it glows.
  glow: Glow | undefined;
  inView: boolean;
  overlays: Map<number, PageTexts> | undefined;
  pairing: boolean;
  marks: PageMarks;
  opening: PagePosition | undefined;
  onTurned: ((position: PagePosition) => void) | undefined;
  onLinks: ((page: number, links: PageLink[]) => void) | undefined;
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
          glow={glow}
          inView={inView}
          overlays={overlays}
          pairing={pairing}
          marks={marks}
          opening={opening}
          onTurned={onTurned}
          onLinks={onLinks}
        />
      )}
    </>
  );
});
