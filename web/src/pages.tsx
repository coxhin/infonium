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
import type { PagePlace } from "./client";
import { reasonOf, type Loaded } from "./loading";
import { scrolls } from "./reader";

GlobalWorkerOptions.workerSrc = workerSource;

// Character maps, fonts and image decoders that the renderer fetches when a file needs them.
const RENDERER_FILES = "/node_modules/pdfjs-dist";
// The shape pages are laid out in until the file's first page has told its own.
const LETTER_SHAPE = 792 / 612;
// A page is drawn once it comes this close to what its scroller shows, as a part of that
// height.
const DRAWING_MARGIN = "150% 0px";
const BOX_CLASS = "page-box";

function percent(fraction: number): string {
  return `${fraction * 100}%`;
}

// The nearest element around this one that scrolls its content, or none when the window does.
function scrollerOf(element: Element): Element | null {
  for (
    let ancestor = element.parentElement;
    ancestor !== null;
    ancestor = ancestor.parentElement
  ) {
    if (scrolls(getComputedStyle(ancestor).overflowY)) {
      return ancestor;
    }
  }
  return null;
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

function Page({
  file,
  number,
  shape,
  boxes,
}: {
  file: PDFDocumentProxy;
  number: number;
  shape: number;
  boxes: PagePlace[];
}) {
  const frame = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [near, setNear] = useState(false);
  const [ownShape, setOwnShape] = useState<number>();
  const [failure, setFailure] = useState<string>();

  useEffect(() => {
    if (frame.current === null) {
      return;
    }
    const watcher = new IntersectionObserver(
      (sightings) => setNear(sightings.at(-1)?.isIntersecting ?? false),
      { root: scrollerOf(frame.current), rootMargin: DRAWING_MARGIN },
    );
    watcher.observe(frame.current);
    return () => watcher.disconnect();
  }, []);

  useEffect(() => {
    if (!near) {
      return;
    }
    let current = true;
    let drawing: RenderTask | undefined;
    function draw(page: PDFPageProxy) {
      if (canvas.current === null || frame.current === null) {
        return;
      }
      const natural = page.getViewport({ scale: 1 });
      const sharpness = (frame.current.clientWidth / natural.width) * devicePixelRatio;
      const viewport = page.getViewport({ scale: sharpness });
      canvas.current.width = viewport.width;
      canvas.current.height = viewport.height;
      setOwnShape(natural.height / natural.width);
      drawing = page.render({ canvas: canvas.current, viewport });
      // Leaving the page cancels its drawing, which is no failure; anything else is one.
      drawing.promise.catch((error: unknown) => {
        if (!(error instanceof RenderingCancelledException)) {
          setFailure(reasonOf(error));
        }
      });
    }
    void whileCurrent(file.getPage(number + 1), () => current, draw, setFailure);
    return () => {
      current = false;
      drawing?.cancel();
      // A page that has gone far gives up its drawing, which is as large as a photograph; it
      // is drawn again when it comes near.
      if (canvas.current !== null) {
        canvas.current.width = 0;
        canvas.current.height = 0;
      }
    };
  }, [near, file, number]);

  return (
    <div className="page" ref={frame} style={{ aspectRatio: `1 / ${ownShape ?? shape}` }}>
      <canvas ref={canvas} />
      {failure !== undefined && <p className="status failed">这一页画不出来：{failure}</p>}
      {boxes.map((box, index) => (
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
      ))}
    </div>
  );
}

function Pages({
  file,
  pointed,
  inView,
}: {
  file: PDFDocumentProxy;
  pointed: PagePlace[];
  inView: boolean;
}) {
  const viewer = useRef<HTMLDivElement>(null);
  const [shape, setShape] = useState(LETTER_SHAPE);

  useEffect(() => {
    let current = true;
    // A first page that cannot be opened keeps the shape assumed for it, and says why when
    // it is drawn.
    void whileCurrent(
      file.getPage(1),
      () => current,
      (page) => {
        const natural = page.getViewport({ scale: 1 });
        setShape(natural.height / natural.width);
      },
      () => undefined,
    );
    return () => {
      current = false;
    };
  }, [file]);

  // Turns to the first of the boxes that were pointed at; one pointed at while the pages
  // were out of view is turned to when they come back.
  useEffect(() => {
    viewer.current?.querySelector(`.${BOX_CLASS}`)?.scrollIntoView({ block: "center" });
  }, [pointed, inView]);

  return (
    <div className="pages" ref={viewer}>
      {Array.from({ length: file.numPages }, (_, number) => (
        <Page
          key={number}
          file={file}
          number={number}
          shape={shape}
          boxes={pointed.filter((place) => place.page === number)}
        />
      ))}
    </div>
  );
}

// Shows a PDF page by page, and outlines the boxes that were pointed at.
export function PageViewer({
  source,
  pointed,
  inView,
}: {
  source: string;
  pointed: PagePlace[];
  inView: boolean;
}) {
  const file = useFile(source);
  return (
    <>
      {file.state === "loading" && <p className="status">正在打开原件…</p>}
      {file.state === "failed" && <p className="status failed">原件打不开：{file.reason}</p>}
      {file.state === "ready" && <Pages file={file.value} pointed={pointed} inView={inView} />}
    </>
  );
}
