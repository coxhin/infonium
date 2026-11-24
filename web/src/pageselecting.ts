import { useEffect, useRef, type RefObject } from "react";
import type { Marked, Selected } from "./marking";
import type { SelectionDrawing } from "./pagemarking";
import {
  anchorsOfSpans,
  beginningLast,
  coveredBoxes,
  lineAt,
  marksAt,
  nearestLine,
  spanAt,
  spansOf,
  stretchOf,
  wholeBlock,
  type Lines,
  type PageMark,
  type Point,
  type Span,
} from "./pagemarks";
import { scrollerOf, viewOf } from "./scrolling";

// A pointer that moves no further than this, in pixels, from where it was pressed drags
// nothing: a press, not a selection.
const STILL_MOUSE = 4;
// A finger moves a little even while it is held still.
const STILL_FINGER = 8;
// A finger held this long on a line, in milliseconds, begins a selection there; one that moves
// before scrolls the pages, as it always did.
const HOLD = 450;
// A pointer within this many pixels of the edge of the view scrolls it, faster the nearer it
// is, up to this many pixels a frame.
const SCROLL_EDGE = 48;
const MOST_SCROLL_STEP = 24;
// A press this far beside a line, as a part of the page's width, is on the line: the boxes of
// lines hug their letters.
const LINE_SLACK = 0.004;

// The element of a page as the viewer draws it, which tells its number and whether a
// translation is set over it; the source kept beside it in 对照 has none.
export const PAGE_SELECTOR = ".page[data-page]";
const AREA_SELECTOR = ".area[data-block-translated]";

// What marking on the pages is told by the view: the document's lines and text, the
// highlights as they are drawn, and where a selection is handed over.
export type PageSelecting = {
  lines: Lines;
  charactersOf: (blockNumber: number) => string[];
  documentId: string;
  marked: Marked[];
  pageMarks: Map<number, PageMark[]>;
  onSelect: (selected: Selected | undefined) => void;
  onPress: (highlightId: string) => void;
};

// Where a point falls: the span of text under it and the page it was found on, and whether it
// was found on an area of a translation, which stands for its block whole.
type Spot = { span: Span; page: HTMLElement; area: HTMLElement | undefined };

type Press = {
  pointerId: number;
  x: number;
  y: number;
  touch: boolean;
  spot: Spot | undefined;
  selecting: boolean;
  timer: number | undefined;
};

function pointOn(page: HTMLElement, x: number, y: number): Point {
  const rect = page.getBoundingClientRect();
  return { x: (x - rect.left) / rect.width, y: (y - rect.top) / rect.height };
}

// An area lets the source through, and so stands for nothing, where it was pressed to show it.
function standing(area: Element): boolean {
  return !(area.closest(".revealing") !== null && area.hasAttribute("data-open"));
}

function areaOf(page: HTMLElement, blockNumber: number): HTMLElement | undefined {
  return [...page.querySelectorAll<HTMLElement>(`.area[data-block-translated="${blockNumber}"]`)]
    .filter(standing)
    .at(-1);
}

// Lets the reader select on the pages by dragging over the lines, or over the areas of a
// translation set over them, and press a highlight drawn there. Nothing is listened to unless
// marking is on.
export function usePageSelecting(
  viewer: RefObject<HTMLDivElement | null>,
  drawing: SelectionDrawing,
  selecting: PageSelecting | undefined,
  inView: boolean,
): void {
  const current = useRef(selecting);
  current.current = selecting;
  const enabled = selecting !== undefined;

  useEffect(() => {
    const root = viewer.current;
    if (root === null || !enabled) {
      return;
    }
    let press: Press | undefined;
    // The selection on show: the spans it was drawn from, and a token that tells it from the
    // next, so that the clear handed over with one never takes another away.
    let shown: { from: Span; to: Span; token: object } | undefined;
    let pointer = { x: 0, y: 0 };
    let frame: number | undefined;
    let dragged = false;

    function letGo(): void {
      if (shown !== undefined) {
        shown = undefined;
        drawing.clear();
        current.current?.onSelect(undefined);
      }
    }

    function nearestPage(y: number): HTMLElement | undefined {
      let nearest: HTMLElement | undefined;
      let least = Infinity;
      for (const page of root?.querySelectorAll<HTMLElement>(PAGE_SELECTOR) ?? []) {
        if (!page.checkVisibility()) {
          continue;
        }
        const rect = page.getBoundingClientRect();
        const distance = Math.max(rect.top - y, 0, y - rect.bottom);
        if (distance < least) {
          least = distance;
          nearest = page;
        }
      }
      return nearest;
    }

    // The span under a point. Strictly, only on a line or an area; otherwise the nearest line
    // of the page under the point, or of the nearest page.
    function spotAt(x: number, y: number, strict: boolean): Spot | undefined {
      const marking = current.current;
      if (marking === undefined) {
        return undefined;
      }
      const hit = document.elementFromPoint(x, y);
      const onPage = hit?.closest<HTMLElement>(PAGE_SELECTOR);
      const page =
        onPage !== null && onPage !== undefined && root?.contains(onPage) ? onPage : undefined;
      const found = page ?? (strict ? undefined : nearestPage(y));
      if (found === undefined) {
        return undefined;
      }
      const translated = found.dataset.translated !== undefined;
      const area = page === undefined ? null : (hit?.closest<HTMLElement>(AREA_SELECTOR) ?? null);
      if (translated && area !== null && standing(area)) {
        const blockNumber = Number(area.dataset.blockTranslated);
        return {
          span: wholeBlock(blockNumber, marking.charactersOf(blockNumber)),
          page: found,
          area,
        };
      }
      const point = pointOn(found, x, y);
      const lines = marking.lines.onPages.get(Number(found.dataset.page)) ?? [];
      const rect = found.getBoundingClientRect();
      const line = strict
        ? lineAt(lines, point, LINE_SLACK)
        : nearestLine(lines, point, rect.height / rect.width);
      if (line === undefined) {
        return undefined;
      }
      const characters = marking.charactersOf(line.blockNumber);
      // Lines that an area covers are selected as the area is: whole.
      const covering = translated ? areaOf(found, line.blockNumber) : undefined;
      return covering === undefined
        ? { span: spanAt(line, point), page: found, area: undefined }
        : { span: wholeBlock(line.blockNumber, characters), page: found, area: covering };
    }

    function spansBetween(from: Span, to: Span): Span[] {
      const marking = current.current;
      return marking === undefined ? [] : spansOf(stretchOf(from, to), marking.charactersOf);
    }

    function draw(from: Span, to: Span): void {
      const marking = current.current;
      if (marking === undefined) {
        return;
      }
      const spans = spansBetween(from, to);
      drawing.show(
        spans.flatMap((span) =>
          coveredBoxes(marking.lines, span.blockNumber, span.start, span.end),
        ),
        spans.map((span) => span.blockNumber),
      );
      shown = { from, to, token: {} };
    }

    // Where the selection ends on the screen: the last line it takes, or the area that holds
    // the last block where the lines are covered.
    function endOf(spans: Span[], focus: Spot): DOMRect | undefined {
      const marking = current.current;
      const last = spans.at(-1);
      if (marking === undefined || last === undefined) {
        return undefined;
      }
      const boxes = coveredBoxes(marking.lines, last.blockNumber, last.start, last.end);
      const lastBox = boxes.at(-1);
      const translated = (page: HTMLElement) => page.dataset.translated !== undefined;
      const pages = [focus.page, ...(root?.querySelectorAll<HTMLElement>(PAGE_SELECTOR) ?? [])];
      if (lastBox === undefined) {
        return focus.page.getBoundingClientRect();
      }
      const page = pages.find(
        (each) => Number(each.dataset.page) === lastBox.page && each.checkVisibility(),
      );
      if (page === undefined) {
        return focus.page.getBoundingClientRect();
      }
      const area = translated(page) ? areaOf(page, last.blockNumber) : undefined;
      if (area !== undefined) {
        return area.getBoundingClientRect();
      }
      const rect = page.getBoundingClientRect();
      const { box } = lastBox;
      return new DOMRect(
        rect.left + box.left * rect.width,
        rect.top + box.top * rect.height,
        box.width * rect.width,
        box.height * rect.height,
      );
    }

    function handOver(focus: Spot): void {
      const marking = current.current;
      if (marking === undefined || shown === undefined) {
        return;
      }
      const spans = spansBetween(shown.from, shown.to);
      const anchors = anchorsOfSpans(spans, marking.documentId, marking.charactersOf);
      const at = endOf(spans, focus);
      if (anchors === undefined || at === undefined) {
        letGo();
        return;
      }
      const { token } = shown;
      marking.onSelect({
        anchors,
        at,
        clear: () => {
          if (shown?.token === token) {
            letGo();
          }
        },
      });
    }

    // Follows the pointer: the selection runs from the press to the span under it, and the
    // view scrolls while it is held at an edge.
    function follow(): void {
      frame = undefined;
      if (press === undefined || !press.selecting || press.spot === undefined) {
        return;
      }
      const focus = spotAt(pointer.x, pointer.y, false);
      if (focus !== undefined) {
        draw(press.spot.span, focus.span);
      }
      const view = viewOf(root as Element);
      const above = view.top + SCROLL_EDGE - pointer.y;
      const below = pointer.y - (view.bottom - SCROLL_EDGE);
      const step =
        above > 0
          ? -Math.min(MOST_SCROLL_STEP, above / 2)
          : below > 0
            ? Math.min(MOST_SCROLL_STEP, below / 2)
            : 0;
      if (step !== 0) {
        const scroller = scrollerOf(root as Element);
        if (scroller === null) {
          scrollBy(0, step);
        } else {
          scroller.scrollTop += step;
        }
        frame = requestAnimationFrame(follow);
      }
    }

    function later(): void {
      if (frame === undefined) {
        frame = requestAnimationFrame(follow);
      }
    }

    function begin(started: Press): void {
      started.selecting = true;
      root?.setPointerCapture(started.pointerId);
      if (started.spot !== undefined) {
        draw(started.spot.span, started.spot.span);
      }
    }

    function pressed(event: PointerEvent): void {
      dragged = false;
      if (!event.isPrimary || event.button !== 0) {
        return;
      }
      letGo();
      const spot = spotAt(event.clientX, event.clientY, true);
      const touch = event.pointerType === "touch";
      press = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        touch,
        spot,
        selecting: false,
        timer: undefined,
      };
      if (spot === undefined) {
        return;
      }
      if (touch) {
        const held = press;
        held.timer = window.setTimeout(() => {
          held.timer = undefined;
          if (press === held) {
            pointer = { x: held.x, y: held.y };
            begin(held);
          }
        }, HOLD);
      } else {
        // The browser's own selection, and the dragging of what is under the press, give way.
        event.preventDefault();
      }
    }

    function moved(event: PointerEvent): void {
      if (press === undefined || event.pointerId !== press.pointerId) {
        return;
      }
      pointer = { x: event.clientX, y: event.clientY };
      if (!press.selecting) {
        const distance = Math.hypot(event.clientX - press.x, event.clientY - press.y);
        if (distance <= (press.touch ? STILL_FINGER : STILL_MOUSE)) {
          return;
        }
        if (press.touch) {
          // A finger that moves before it is held scrolls.
          window.clearTimeout(press.timer);
          press = undefined;
          return;
        }
        if (press.spot === undefined) {
          return;
        }
        begin(press);
      }
      later();
    }

    function released(event: PointerEvent): void {
      if (press === undefined || event.pointerId !== press.pointerId) {
        return;
      }
      const ended = press;
      press = undefined;
      window.clearTimeout(ended.timer);
      if (frame !== undefined) {
        cancelAnimationFrame(frame);
        frame = undefined;
      }
      if (ended.selecting) {
        dragged = true;
        const focus = spotAt(event.clientX, event.clientY, false) ?? ended.spot;
        if (focus !== undefined && ended.spot !== undefined) {
          draw(ended.spot.span, focus.span);
          handOver(focus);
        }
        return;
      }
      if (ended.spot !== undefined) {
        pressHighlight(ended.spot, event.clientX, event.clientY);
      }
    }

    function cancelled(event: PointerEvent): void {
      if (press !== undefined && event.pointerId === press.pointerId) {
        window.clearTimeout(press.timer);
        const ended = press;
        press = undefined;
        if (ended.selecting) {
          letGo();
        }
      }
    }

    // A press on a highlight, the one that begins last where several lie under it; on an area,
    // those that run through its block.
    function pressHighlight(spot: Spot, x: number, y: number): void {
      const marking = current.current;
      if (marking === undefined) {
        return;
      }
      let under: Marked[];
      if (spot.area !== undefined) {
        under = marking.marked.filter((each) =>
          each.anchors.some((anchor) => anchor.block_number === spot.span.blockNumber),
        );
      } else {
        const ids = new Set(
          marksAt(
            marking.pageMarks.get(Number(spot.page.dataset.page)) ?? [],
            pointOn(spot.page, x, y),
          ),
        );
        under = marking.marked.filter((each) => ids.has(each.id));
      }
      const chosen = beginningLast(under);
      if (chosen !== undefined) {
        marking.onPress(chosen.id);
      }
    }

    // A drag ends in a click on what is under it, which is no press of an area.
    function clicked(event: MouseEvent): void {
      if (dragged) {
        dragged = false;
        event.stopPropagation();
        event.preventDefault();
      }
    }

    // While a finger selects, it does not scroll the pages, nor open the browser's menu.
    function heldStill(event: Event): void {
      if (press?.selecting === true && press.touch) {
        event.preventDefault();
      }
    }

    // A press anywhere else lets go of the selection, but not one on what is offered on it,
    // which says so by taking the press for itself.
    function pressedElsewhere(event: MouseEvent): void {
      if (
        !event.defaultPrevented &&
        !(event.target instanceof Node && root?.contains(event.target))
      ) {
        letGo();
      }
    }

    function keyed(event: KeyboardEvent): void {
      if (event.key === "Escape") {
        letGo();
      }
    }

    root.addEventListener("pointerdown", pressed);
    root.addEventListener("pointermove", moved);
    root.addEventListener("pointerup", released);
    root.addEventListener("pointercancel", cancelled);
    root.addEventListener("click", clicked, true);
    root.addEventListener("touchmove", heldStill, { passive: false });
    root.addEventListener("contextmenu", heldStill);
    document.addEventListener("mousedown", pressedElsewhere);
    document.addEventListener("keydown", keyed);
    return () => {
      root.removeEventListener("pointerdown", pressed);
      root.removeEventListener("pointermove", moved);
      root.removeEventListener("pointerup", released);
      root.removeEventListener("pointercancel", cancelled);
      root.removeEventListener("click", clicked, true);
      root.removeEventListener("touchmove", heldStill);
      root.removeEventListener("contextmenu", heldStill);
      document.removeEventListener("mousedown", pressedElsewhere);
      document.removeEventListener("keydown", keyed);
      window.clearTimeout(press?.timer);
      if (frame !== undefined) {
        cancelAnimationFrame(frame);
      }
      letGo();
    };
  }, [viewer, drawing, enabled, inView]);
}
