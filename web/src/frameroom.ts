import { useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from "react";
import { scrollerOf, scrolls } from "./scrolling";

// The reader around the frame: what scrolls in one column, where the window scrolls itself.
const READER = "main";
// A frame never shorter than this, in pixels, however little of the window is left for it:
// enough for a few lines of a page. A phone in one column, 390 by 844 pixels with the column
// beside the reader open, leaves 316.
const SHORTEST_FRAME = 160;
// A part of the frame in view smaller than this, in pixels, is no room to show a passage in;
// the whole frame is taken instead.
const LEAST_ROOM = 48;
const HELD_POSITIONS = ["sticky", "fixed"];

// How much of the foot of the window is covered by what is held there over the reader, such
// as the column beside the reader in one column, open or folded to its tabs.
function coveredAtFoot(frame: HTMLIFrameElement): number {
  const place = frame.getBoundingClientRect();
  const found = document.elementFromPoint(place.left + place.width / 2, innerHeight - 1);
  if (found === null || frame.closest(READER)?.contains(found) === true) {
    return 0;
  }
  for (let held: Element | null = found; held !== null; held = held.parentElement) {
    if (HELD_POSITIONS.includes(getComputedStyle(held).position)) {
      return Math.max(0, innerHeight - held.getBoundingClientRect().top);
    }
  }
  return 0;
}

// An element and where it stands in what the frame shows.
type Place = { element: Element; top: number; height: number };

function placeOf(element: Element): Place {
  const place = element.getBoundingClientRect();
  return { element, top: place.top, height: place.height };
}

// The place to keep while the content of the page's elements that hold blocks changes, which
// moves one only with the elements before it and with its own height: that of the passage
// pointed at, while it is in what the frame shows, and otherwise that of the first of those
// elements, in the page's order, that reaches down into it.
export function placeToKeep(
  page: Document,
  holders: string,
  pointed: Element | null,
): Place | undefined {
  const shown = page.documentElement.clientHeight;
  const place = pointed === null ? undefined : placeOf(pointed);
  const inView = place !== undefined && place.top + place.height > 0 && place.top < shown;
  if (inView && place.height > 0) {
    return place;
  }
  for (const element of page.querySelectorAll(holders)) {
    const held = placeOf(element);
    if (held.height > 0 && held.top + held.height > 0) {
      return held;
    }
  }
  return undefined;
}

// Changes the page and keeps the reader where they were: the element whose place is kept is
// put back where it stood, as large a share of it above the frame's top as before, as near as
// its changed height allows.
export function keepingPlace(before: Place | undefined, view: Window, change: () => void): void {
  change();
  if (before === undefined || !before.element.isConnected) {
    return;
  }
  const share = before.height > 0 ? Math.min(1, Math.max(0, -before.top / before.height)) : 0;
  const after = placeOf(before.element);
  const shift = after.top + share * after.height - (before.top + share * before.height);
  if (Math.abs(shift) >= 1) {
    view.scrollBy({ top: shift, behavior: "instant" });
  }
}

// The part of the frame, in the frame's own coordinates, that the window shows the reader: not
// what lies above the reader's top or under what is held at the foot of the window.
function roomInFrame(frame: HTMLIFrameElement): { top: number; bottom: number } {
  const scroller = scrollerOf(frame);
  const view = scroller?.getBoundingClientRect() ?? new DOMRect(0, 0, innerWidth, innerHeight);
  const top = frame.getBoundingClientRect().top + frame.clientTop;
  const room = {
    top: Math.max(top, view.top) - top,
    bottom: Math.min(top + frame.clientHeight, view.bottom - coveredAtFoot(frame)) - top,
  };
  return room.bottom - room.top < LEAST_ROOM ? { top: 0, bottom: frame.clientHeight } : room;
}

// Where the window itself scrolls, as in one column, brings the reader to the top of the
// window, where the frame stands whole below the reader's head.
export function bringReaderToTop(frame: HTMLIFrameElement): void {
  const reader = frame.closest(READER);
  if (reader !== null && scrollerOf(frame) === null) {
    scrollBy({ top: reader.getBoundingClientRect().top, behavior: "instant" });
  }
}

// Brings the reader to the top of the window where the window scrolls, then puts the passage
// in the middle of everything in the frame that scrolls around it, innermost first, and in the
// page itself in the middle of the room the window shows of the frame. A passage larger than
// the room is put at its start. The jumps are instant, as the reader's are.
export function bringIntoFrame(frame: HTMLIFrameElement, view: Window, passage: Range): void {
  bringReaderToTop(frame);
  const room = roomInFrame(frame);
  const page = view.document;
  for (
    let ancestor = passage.startContainer.parentElement;
    ancestor !== null;
    ancestor = ancestor.parentElement
  ) {
    const isPage = ancestor === page.scrollingElement;
    const style = view.getComputedStyle(ancestor);
    const place = passage.getBoundingClientRect();
    const shown = isPage
      ? new DOMRect(0, room.top, page.documentElement.clientWidth, room.bottom - room.top)
      : ancestor.getBoundingClientRect();
    if ((isPage || scrolls(style.overflowY)) && ancestor.scrollHeight > ancestor.clientHeight) {
      const margin = Math.max(0, (shown.height - place.height) / 2);
      ancestor.scrollBy({ top: place.top - shown.top - margin, behavior: "instant" });
    }
    if ((isPage || scrolls(style.overflowX)) && ancestor.scrollWidth > ancestor.clientWidth) {
      const margin = Math.max(0, (shown.width - place.width) / 2);
      ancestor.scrollBy({ left: place.left - shown.left - margin, behavior: "instant" });
    }
  }
}

// The frame as tall as what the reader has of the window below what stands above the frame in
// the reader, and above what is held at the foot of the window; measured again whenever any
// of it changes size, as when the column held there is folded.
export function useFrameHeight(
  frame: RefObject<HTMLIFrameElement | null>,
  onShow: boolean,
): number | undefined {
  const [height, setHeight] = useState<number>();
  useLayoutEffect(() => {
    const element = frame.current;
    const reader = element?.closest(READER) ?? null;
    if (!onShow || element === null || reader === null) {
      return;
    }
    function measure() {
      if (element === null || reader === null) {
        return;
      }
      const scrolled = scrolls(getComputedStyle(reader).overflowY);
      const above =
        element.getBoundingClientRect().top -
        reader.getBoundingClientRect().top +
        (scrolled ? reader.scrollTop : 0);
      const view = scrolled ? reader.clientHeight : innerHeight - coveredAtFoot(element);
      const border = element.offsetHeight - element.clientHeight;
      setHeight(Math.max(SHORTEST_FRAME, Math.floor(view - above - border)));
    }
    measure();
    const resized = new ResizeObserver(measure);
    resized.observe(document.documentElement);
    resized.observe(reader);
    for (const child of reader.children) {
      resized.observe(child);
    }
    addEventListener("resize", measure);
    return () => {
      resized.disconnect();
      removeEventListener("resize", measure);
    };
  }, [frame, onShow]);
  return height;
}

// What the reader does to move the page or to point elsewhere: after it, the place is theirs.
const READER_INPUT = ["wheel", "touchmove", "keydown", "pointerdown"];
// A scroll position this many pixels from the one infonium left is taken for another's.
const MOVED = 1;

// A passage brought into view is kept in view while the page's layout settles under it: the
// page is parsed before its stylesheets, pictures and fonts arrive, and each of them moves
// what follows it. The passage is brought into view again whenever the page changes its size
// or something in it loads, until the reader scrolls, presses a key or points anywhere, in
// the page or in the app around it, or anything else than infonium scrolls the page.
export type Following = {
  // Begins to follow a passage, or stops; called right after it is brought into view.
  follow: (passage: Range | undefined) => void;
  // Puts a passage in the place of the one followed, as when the page's content is written
  // again and its place kept, while one is followed.
  renew: (passage: Range) => void;
};

function scrollTopOf(view: Window | undefined): number | undefined {
  return view?.document.scrollingElement?.scrollTop;
}

export function usePassageFollowed(
  frame: RefObject<HTMLIFrameElement | null>,
  view: Window | undefined,
  onShow: boolean,
): Following {
  const followed = useRef<Range>(undefined);
  // Where infonium last left the page's scroll.
  const left = useRef<number>(undefined);
  const shownView = useRef<Window>(undefined);
  shownView.current = view;
  useEffect(() => {
    const element = frame.current;
    if (view === undefined || element === null || !onShow) {
      return;
    }
    const shown = view;
    const page = shown.document;
    let pending = 0;
    function bringAgain() {
      if (followed.current === undefined || pending !== 0) {
        return;
      }
      pending = requestAnimationFrame(() => {
        pending = 0;
        if (followed.current !== undefined && element !== null && element.isConnected) {
          bringIntoFrame(element, shown, followed.current);
          left.current = scrollTopOf(shown);
        }
      });
    }
    function leave() {
      followed.current = undefined;
    }
    function scrolled() {
      const top = scrollTopOf(shown);
      if (top !== undefined && left.current !== undefined && Math.abs(top - left.current) > MOVED) {
        leave();
      }
    }
    const resized = new ResizeObserver(bringAgain);
    resized.observe(page.documentElement);
    if (page.body !== null) {
      resized.observe(page.body);
    }
    resized.observe(element);
    page.addEventListener("load", bringAgain, true);
    page.fonts.addEventListener("loadingdone", bringAgain);
    const input = { capture: true, passive: true };
    for (const kind of READER_INPUT) {
      page.addEventListener(kind, leave, input);
      addEventListener(kind, leave, input);
    }
    shown.addEventListener("scroll", scrolled, { passive: true });
    return () => {
      cancelAnimationFrame(pending);
      resized.disconnect();
      page.removeEventListener("load", bringAgain, true);
      page.fonts.removeEventListener("loadingdone", bringAgain);
      for (const kind of READER_INPUT) {
        page.removeEventListener(kind, leave, input);
        removeEventListener(kind, leave, input);
      }
      shown.removeEventListener("scroll", scrolled);
    };
  }, [frame, view, onShow]);
  return useMemo(
    () => ({
      follow: (passage) => {
        followed.current = passage;
        left.current = scrollTopOf(shownView.current);
      },
      renew: (passage) => {
        if (followed.current !== undefined) {
          followed.current = passage;
          left.current = scrollTopOf(shownView.current);
        }
      },
    }),
    [],
  );
}
