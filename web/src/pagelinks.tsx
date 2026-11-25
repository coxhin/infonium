import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";
import { useCallback, useEffect, useState, type RefObject } from "react";
import { percent, type Box } from "./areas";
import { positionOf, turnTo, type PagePosition } from "./pageturning";
import "./pagelinks.css";

// The links a PDF's pages hold: where each lies on its page and where it leads, a place of the
// document or an address on the web, drawn over the page so that a press follows it.

// A place of the document: a page, and how far down it, as a part of its height, where the
// link names a height.
export type Place = { page: number; top: number | undefined };

export type LinkTarget = { kind: "address"; url: string } | { kind: "place"; place: Place };

export type PageLink = { box: Box; target: LinkTarget };

// What pdf.js tells of an annotation, as far as a link is read here.
type Annotation = {
  annotationType: number;
  rect: number[];
  url?: string;
  dest?: unknown;
  action?: string;
};

// pdf.js's AnnotationType.LINK.
const LINK = 2;
const WEB_PROTOCOLS = new Set(["http:", "https:"]);
// The named actions of a link that turn the pages, and where each turns them from the page
// the link stands on.
const NAMED_TURNS: Record<string, (page: number, last: number) => number> = {
  NextPage: (page, last) => Math.min(page + 1, last),
  PrevPage: (page) => Math.max(page - 1, 0),
  FirstPage: () => 0,
  LastPage: (_, last) => last,
};

function isRef(value: unknown): value is { num: number; gen: number } {
  return typeof value === "object" && value !== null && "num" in value && "gen" in value;
}

// The height on the page a destination names: the top of an XYZ or a FitH view, or of the
// rectangle of a FitR one; other views show the page from its top.
function namedHeight(kind: unknown, args: unknown[]): unknown {
  const name = typeof kind === "object" && kind !== null && "name" in kind ? kind.name : undefined;
  switch (name) {
    case "XYZ":
      return args[1];
    case "FitH":
    case "FitBH":
      return args[0];
    case "FitR":
      return args[3];
    default:
      return undefined;
  }
}

// The place a destination of the document leads to: one named, which the file resolves, or
// one given whole, as a page and a view of it.
async function placeOf(file: PDFDocumentProxy, destination: unknown): Promise<Place | undefined> {
  const explicit =
    typeof destination === "string" ? await file.getDestination(destination) : destination;
  if (!Array.isArray(explicit)) {
    return undefined;
  }
  const [reference, kind, ...args] = explicit as unknown[];
  const index =
    typeof reference === "number"
      ? reference
      : isRef(reference)
        ? await file.getPageIndex(reference)
        : undefined;
  if (index === undefined || index < 0 || index >= file.numPages) {
    return undefined;
  }
  const height = namedHeight(kind, args);
  if (typeof height !== "number") {
    return { page: index, top: undefined };
  }
  const viewport = (await file.getPage(index + 1)).getViewport({ scale: 1 });
  const [, y] = viewport.convertToViewportPoint(0, height) as [number, number];
  return { page: index, top: Math.min(1, Math.max(0, y / viewport.height)) };
}

// Where a link leads, if it leads anywhere the reader follows: a place of the document, or an
// address on the web that is not infonium's own.
async function targetOf(
  file: PDFDocumentProxy,
  annotation: Annotation,
  page: number,
): Promise<LinkTarget | undefined> {
  if (annotation.url !== undefined) {
    if (!URL.canParse(annotation.url)) {
      return undefined;
    }
    const address = new URL(annotation.url);
    return WEB_PROTOCOLS.has(address.protocol) && address.origin !== location.origin
      ? { kind: "address", url: address.href }
      : undefined;
  }
  if (annotation.dest !== undefined) {
    const place = await placeOf(file, annotation.dest).catch(() => undefined);
    return place === undefined ? undefined : { kind: "place", place };
  }
  const turn = annotation.action === undefined ? undefined : NAMED_TURNS[annotation.action];
  return turn === undefined
    ? undefined
    : { kind: "place", place: { page: turn(page, file.numPages - 1), top: undefined } };
}

async function linksOf(file: PDFDocumentProxy, page: PDFPageProxy): Promise<PageLink[]> {
  const annotations = (await page.getAnnotations({ intent: "display" })) as Annotation[];
  const viewport = page.getViewport({ scale: 1 });
  const links = await Promise.all(
    annotations
      .filter((annotation) => annotation.annotationType === LINK && annotation.rect.length === 4)
      .map(async (annotation): Promise<PageLink | undefined> => {
        const [x1 = 0, y1 = 0, x2 = 0, y2 = 0] = annotation.rect;
        const [left, top] = viewport.convertToViewportPoint(x1, y1) as [number, number];
        const [right, bottom] = viewport.convertToViewportPoint(x2, y2) as [number, number];
        const target = await targetOf(file, annotation, page.pageNumber - 1);
        if (target === undefined) {
          return undefined;
        }
        return {
          box: {
            left: Math.min(left, right) / viewport.width,
            top: Math.min(top, bottom) / viewport.height,
            width: Math.abs(right - left) / viewport.width,
            height: Math.abs(bottom - top) / viewport.height,
          },
          target,
        };
      }),
  );
  return links.filter((link) => link !== undefined);
}

// The links of each page of a file, read once a file.
const readLinks = new WeakMap<PDFDocumentProxy, Map<number, Promise<PageLink[]>>>();

function pageLinks(file: PDFDocumentProxy, number: number): Promise<PageLink[]> {
  let ofFile = readLinks.get(file);
  if (ofFile === undefined) {
    ofFile = new Map();
    readLinks.set(file, ofFile);
  }
  let links = ofFile.get(number);
  if (links === undefined) {
    // A page whose links cannot be read is shown without them: its drawing says itself why
    // the page cannot be opened, if it cannot.
    links = file.getPage(number + 1).then(
      (page) => linksOf(file, page),
      () => [],
    );
    ofFile.set(number, links);
  }
  return links;
}

const NO_LINKS: PageLink[] = [];

// The links of a page, read once it has come near what is on show.
export function usePageLinks(file: PDFDocumentProxy, number: number, near: boolean): PageLink[] {
  const [links, setLinks] = useState<PageLink[]>(NO_LINKS);
  useEffect(() => {
    if (!near) {
      return;
    }
    let current = true;
    void pageLinks(file, number).then(
      (read) => current && setLinks(read.length === 0 ? NO_LINKS : read),
      () => undefined,
    );
    return () => {
      current = false;
    };
  }, [file, number, near]);
  return links;
}

// A place of the document as a link of a translation names it, which the pages follow when it
// is pressed (followedPlace) and which leads nowhere of itself.
const PLACE_LINK = "#infonium-place:";

export function placeLink({ page, top }: Place): string {
  return `${PLACE_LINK}${page}:${top ?? ""}`;
}

export function linkOfTarget(target: LinkTarget): string {
  return target.kind === "address" ? target.url : placeLink(target.place);
}

// The place a link of a translation leads to, where the element pressed lies in one that
// names a place of the document.
export function followedPlace(pressed: EventTarget | null): Place | undefined {
  const link = pressed instanceof Element ? pressed.closest("a[href]") : null;
  const href = link?.getAttribute("href") ?? "";
  if (!href.startsWith(PLACE_LINK)) {
    return undefined;
  }
  const [page, top] = href.slice(PLACE_LINK.length).split(":");
  const number = Number(page);
  return Number.isInteger(number)
    ? { page: number, top: top === undefined || top === "" ? undefined : Number(top) }
    : undefined;
}

// The links of one page, over the page and the reader's marks: on a page that shows the
// translation they lie under it, and only those no area covers can be pressed.
export function LinksLayer({
  links,
  under,
  onPlace,
}: {
  links: PageLink[];
  under: boolean;
  onPlace: (place: Place) => void;
}) {
  if (links.length === 0) {
    return null;
  }
  return (
    <div className={under ? "page-links under" : "page-links"}>
      {links.map(({ box, target }, index) => {
        const style = {
          left: percent(box.left),
          top: percent(box.top),
          width: percent(box.width),
          height: percent(box.height),
        };
        return target.kind === "address" ? (
          <a
            key={index}
            className={PAGE_LINK_CLASS}
            href={target.url}
            target="_blank"
            rel="noreferrer noopener"
            title={target.url}
            aria-label={target.url}
            style={style}
          />
        ) : (
          <button
            key={index}
            type="button"
            className={PAGE_LINK_CLASS}
            title={`跳到第 ${target.place.page + 1} 页`}
            aria-label={`跳到第 ${target.place.page + 1} 页`}
            style={style}
            onClick={() => onPlace(target.place)}
          />
        );
      })}
    </div>
  );
}

const PAGE_LINK_CLASS = "page-link";
// A link over the page, or one a translation set over it carries.
const ANY_LINK = `.${PAGE_LINK_CLASS}, .page-translation a[href]`;
const TRANSLATED_LINK = ".page-translation a[href]";

// Presses on the links of the pages: a press on a link follows it alone, so no selection
// begins there and no highlight under it opens; a link of a translation that names a place of
// the document turns the pages there, and one to the web opens in a tab of its own without
// turning the area it stands in, whether pressed or reached by the keyboard. A click that ends
// a drag follows nothing, and a place of the document opens in no other tab.
export function useLinkPresses(
  viewer: RefObject<HTMLElement | null>,
  onPlace: (place: Place) => void,
): void {
  useEffect(() => {
    const shown = viewer.current;
    if (shown === null) {
      return;
    }
    const within = (event: Event, selector: string) =>
      event.target instanceof Element && event.target.closest(selector) !== null;
    const pressed = (event: PointerEvent) => {
      if (within(event, ANY_LINK)) {
        event.stopPropagation();
      }
    };
    const clicked = (event: MouseEvent) => {
      if (event.defaultPrevented) {
        return;
      }
      const place = followedPlace(event.target);
      if (place !== undefined) {
        event.preventDefault();
        event.stopPropagation();
        onPlace(place);
      } else if (within(event, TRANSLATED_LINK)) {
        event.stopPropagation();
      }
    };
    // Enter on a link of a translation follows it by the click it fires; neither it nor a space
    // reaches the area the link stands in, which they would open or close.
    const keyed = (event: KeyboardEvent) => {
      if ((event.key === "Enter" || event.key === " ") && within(event, TRANSLATED_LINK)) {
        event.stopPropagation();
      }
    };
    const auxiliary = (event: MouseEvent) => {
      if (followedPlace(event.target) !== undefined) {
        event.preventDefault();
        event.stopPropagation();
      }
    };
    shown.addEventListener("pointerdown", pressed, true);
    shown.addEventListener("click", clicked, true);
    shown.addEventListener("keydown", keyed, true);
    shown.addEventListener("auxclick", auxiliary, true);
    return () => {
      shown.removeEventListener("pointerdown", pressed, true);
      shown.removeEventListener("click", clicked, true);
      shown.removeEventListener("keydown", keyed, true);
      shown.removeEventListener("auxclick", auxiliary, true);
    };
  }, [viewer, onPlace]);
}

// How far below the top of what the pages show a place a link leads to is put, in pixels, so
// that the line above it shows too.
const PLACE_MARGIN = 24;

// The way back from places links led to: where the reader stood before each link that led to a
// place of the document, last last; following a link, returning through them, and giving them
// up.
export type GoingBackState = {
  behind: PagePosition[];
  follow: (place: Place) => void;
  back: () => void;
  dismiss: () => void;
};

export function useGoingBack(viewer: RefObject<HTMLElement | null>): GoingBackState {
  const [behind, setBehind] = useState<PagePosition[]>([]);
  const follow = useCallback(
    (place: Place) => {
      const shown = viewer.current;
      const from = shown === null ? undefined : positionOf(shown);
      if (shown === null || from === undefined) {
        return;
      }
      setBehind((before) => [...before, from]);
      turnTo(shown, { page: place.page, within: place.top ?? 0 }, PLACE_MARGIN);
    },
    [viewer],
  );
  const back = useCallback(() => {
    const shown = viewer.current;
    const to = behind.at(-1);
    if (shown !== null && to !== undefined) {
      turnTo(shown, to);
    }
    setBehind((before) => before.slice(0, -1));
  }, [viewer, behind]);
  const dismiss = useCallback(() => setBehind([]), []);
  return { behind, follow, back, dismiss };
}

// The way back from the places links led to, last first: a press returns to where the reader
// stood before the last one, and the other gives them all up.
export function GoingBack({ going }: { going: GoingBackState }) {
  const last = going.behind.at(-1);
  if (last === undefined) {
    return null;
  }
  return (
    <div className="going-back">
      <div role="group" aria-label="跳转前的位置">
        <button type="button" onClick={going.back} title="回到点链接之前看的位置">
          ↩ 回到第 {last.page + 1} 页
        </button>
        <button type="button" onClick={going.dismiss} aria-label="不用回去" title="不用回去">
          ×
        </button>
      </div>
    </div>
  );
}
