import { useCallback, useEffect, useLayoutEffect, useRef, type RefObject } from "react";
import { scrollerOf, uncoveredTop } from "./scrolling";
import { useWheelZoom, useZoom } from "./zoom";

// Where the reader stands in the pages of a PDF, how the pages are turned to a place, and how
// the place is kept while the pages are zoomed. The pages are the viewer's children, one for
// each page of the file.

// Where the reader stands in the pages: the page at the top of what the scroller shows them of,
// and how far down that page the view begins, as a part of its height.
export type PagePosition = { page: number; within: number };

export function positionOf(viewer: Element): PagePosition | undefined {
  const top = uncoveredTop(viewer);
  const pages = [...viewer.children];
  const page = pages.findIndex((shown) => shown.getBoundingClientRect().bottom > top);
  const place = pages[page]?.getBoundingClientRect();
  return place === undefined
    ? undefined
    : { page, within: Math.max(0, (top - place.top) / place.height) };
}

// Turns the pages so that the position given stands at the top of what they show, or this
// many pixels below it.
export function turnTo(viewer: Element, { page, within }: PagePosition, margin = 0) {
  const place = viewer.children[page]?.getBoundingClientRect();
  if (place !== undefined) {
    const scroller = scrollerOf(viewer) ?? document.scrollingElement;
    scroller?.scrollBy({
      top: place.top - uncoveredTop(viewer) + within * place.height - margin,
    });
  }
}

// A point of a page held under the pointer while the pages are zoomed around it: the page, the
// point as parts of its width and height, and where the pointer was in the window.
type Pin = { page: number; x: number; y: number; clientX: number; clientY: number };

function pinOf(viewer: Element, event: WheelEvent): Pin | undefined {
  const pages = [...viewer.children];
  const page = pages.findIndex((shown) => shown.getBoundingClientRect().bottom > event.clientY);
  const place = pages[page]?.getBoundingClientRect();
  return place === undefined
    ? undefined
    : {
        page,
        x: (event.clientX - place.left) / place.width,
        y: (event.clientY - place.top) / place.height,
        clientX: event.clientX,
        clientY: event.clientY,
      };
}

// The zoom of the pages, and where the reader stands in them, kept as they scroll and told if
// asked, once a frame at most. A zoom keeps the place under the pointer where it was for a
// pinch or the wheel with Ctrl held, and otherwise the place at the top of what the pages show;
// a zoom made while the pages are not on show, as from another window, moves nothing. Whatever
// scrolls the pages is heard, since a change of the window's width may hand the scrolling from
// the reader's column to the window.
export function usePagesZoom(
  viewer: RefObject<HTMLElement | null>,
  inView: boolean,
  onTurned: ((position: PagePosition) => void) | undefined,
): number {
  const zoom = useZoom("pages");
  const standing = useRef<PagePosition>(undefined);
  const pinned = useRef<Pin>(undefined);
  const shownNow = useRef(inView);
  shownNow.current = inView;

  useEffect(() => {
    const shown = viewer.current;
    if (shown === null) {
      return;
    }
    let frame = 0;
    const scrolled = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        const position = positionOf(shown);
        if (position !== undefined) {
          standing.current = position;
          onTurned?.(position);
        }
      });
    };
    const heard = { capture: true, passive: true };
    document.addEventListener("scroll", scrolled, heard);
    return () => {
      document.removeEventListener("scroll", scrolled, heard);
      cancelAnimationFrame(frame);
    };
  }, [viewer, onTurned]);

  const zooming = useCallback(
    (event: WheelEvent) => {
      pinned.current = viewer.current === null ? undefined : pinOf(viewer.current, event);
    },
    [viewer],
  );
  useWheelZoom(viewer, "pages", zooming);

  useLayoutEffect(() => {
    const shown = viewer.current;
    const pin = pinned.current;
    pinned.current = undefined;
    if (shown === null || !shownNow.current) {
      return;
    }
    const place = pin === undefined ? undefined : shown.children[pin.page]?.getBoundingClientRect();
    if (pin !== undefined && place !== undefined) {
      (scrollerOf(shown) ?? document.scrollingElement)?.scrollBy({
        top: place.top + pin.y * place.height - pin.clientY,
      });
      shown.scrollBy({ left: place.left + pin.x * place.width - pin.clientX });
    } else if (standing.current !== undefined) {
      turnTo(shown, standing.current);
    }
  }, [viewer, zoom]);

  return zoom;
}
