import { useEffect, useRef, type RefObject } from "react";
import type { Anchor, Anchors } from "./client";
import { lastBegun, type Marked, type Selected } from "./marking";
import type { PageMark, SelectionDrawing } from "./pagemarking";
import { nearestLine, type PageText, type Point, type Span } from "./pagelines";
import { scrollerOf, viewOf } from "./scrolling";
import { SELECTION_ACTIONS_CLASS } from "./selectionactions";

// How a reader selects on the pages of a PDF, and presses a highlight drawn there. A drag over
// the source's lines selects by their boxes; the text of a translation set over the pages is
// the browser's to select, and what it selects there is handed over as the blocks of the areas
// it touches, each whole. On a touch screen, scrolling is the browser's alone: a finger held
// still on a line selects its block, and a tap presses a highlight.

// A mouse that moves no further than this, in pixels, from where it was pressed drags nothing:
// a press, not a selection. A finger moves a little even while it is held still.
const STILL_MOUSE = 4;
const STILL_FINGER = 8;
// A finger held this long on a line, in milliseconds, selects its block.
const HOLD = 450;
// A pointer within this many pixels of the edge of the view scrolls it, faster the nearer it
// is, up to this many pixels a frame.
const SCROLL_EDGE = 48;
const MOST_SCROLL_STEP = 24;
// A press this many of a line's depths beside it, as in a paragraph's indent or the margin,
// still begins on it, as the browser's own selection does.
const START_REACH = 1.5;
// The browser's selection has settled when it has not changed for this many milliseconds, as
// a keyboard or the handles of a touch screen move it.
const SETTLED = 250;

// The element of a page as the viewer draws it, which tells its number and whether a
// translation is set over it; the source kept beside it in 对照 has none.
const PAGE_SELECTOR = ".page[data-page]";
const TRANSLATED_AREAS = ".page[data-translated] .area[data-block-translated]";
const AREA_SELECTOR = ".area[data-block-translated]";
const OPEN_FLOW = ".flow[data-open]";
const PRESSABLE_FLOW = '.flow[role="button"]';

// What marking on the pages is told by the view: where the document's text lies, the
// highlights as they are drawn, and where a selection is handed over.
export type PageSelecting = {
  text: PageText;
  documentId: string;
  marked: Marked[];
  pageMarks: Map<number, PageMark[]>;
  onSelect: (selected: Selected | undefined) => void;
  onPress: (highlightId: string) => void;
};

// Where a point falls: the span of text under it and the page it was found on, and the area of
// a translation it was found on, which stands for its block whole.
type Spot = { span: Span; page: HTMLElement; area: HTMLElement | undefined };

type Press = {
  pointerId: number;
  x: number;
  y: number;
  touch: boolean;
  spot: Spot | undefined;
  selecting: boolean;
  held: boolean;
  timer: number | undefined;
};

type Place = { blockNumber: number; offset: number };

function before(one: Place, other: Place): boolean {
  return (
    one.blockNumber < other.blockNumber ||
    (one.blockNumber === other.blockNumber && one.offset < other.offset)
  );
}

function sameSpan(one: Span, other: Span): boolean {
  return (
    one.blockNumber === other.blockNumber && one.start === other.start && one.end === other.end
  );
}

const SPACE = /\s/u;

// The spans of each block the stretch from one span to the other takes text of, in the order
// of the document, without white space at either end.
function stretched(text: PageText, one: Span, other: Span): Span[] {
  const oneStart = { blockNumber: one.blockNumber, offset: one.start };
  const otherStart = { blockNumber: other.blockNumber, offset: other.start };
  const oneEnd = { blockNumber: one.blockNumber, offset: one.end };
  const otherEnd = { blockNumber: other.blockNumber, offset: other.end };
  const first = before(otherStart, oneStart) ? otherStart : oneStart;
  const last = before(oneEnd, otherEnd) ? otherEnd : oneEnd;
  const spans: Span[] = [];
  for (let blockNumber = first.blockNumber; blockNumber <= last.blockNumber; blockNumber += 1) {
    const characters = text.textOf(blockNumber);
    let start = blockNumber === first.blockNumber ? first.offset : 0;
    let end = blockNumber === last.blockNumber ? last.offset : characters.length;
    while (start < end && SPACE.test(characters[start] ?? "")) {
      start += 1;
    }
    while (end > start && SPACE.test(characters[end - 1] ?? "")) {
      end -= 1;
    }
    if (start < end) {
      spans.push({ blockNumber, start, end });
    }
  }
  return spans;
}

function anchorsOf(text: PageText, spans: Span[], documentId: string): Anchors | undefined {
  const anchors: Anchor[] = spans.map((span) => ({
    document_id: documentId,
    block_number: span.blockNumber,
    start: span.start,
    end: span.end,
    quote: text.textOf(span.blockNumber).slice(span.start, span.end).join(""),
  }));
  const [first, ...rest] = anchors;
  return first === undefined ? undefined : [first, ...rest];
}

function pointOn(page: HTMLElement, x: number, y: number): Point {
  const rect = page.getBoundingClientRect();
  return { x: (x - rect.left) / rect.width, y: (y - rect.top) / rect.height };
}

// Whether a range holds any of an element's text: a range that only touches it at its edge, as
// one a triple click makes ends at the start of the next paragraph, holds none of it.
function holdsTextOf(range: Range, element: Element): boolean {
  if (!range.intersectsNode(element)) {
    return false;
  }
  const inside = document.createRange();
  inside.selectNodeContents(element);
  if (range.compareBoundaryPoints(Range.START_TO_START, inside) > 0) {
    inside.setStart(range.startContainer, range.startOffset);
  }
  if (range.compareBoundaryPoints(Range.END_TO_END, inside) < 0) {
    inside.setEnd(range.endContainer, range.endOffset);
  }
  return inside.toString().trim() !== "";
}

// An area lets the source through, and so stands for nothing, where its flow was pressed to
// show it.
function standing(area: Element): boolean {
  return !(area.closest(".revealing") !== null && area.closest(OPEN_FLOW) !== null);
}

// An area whose flow does something of its own when pressed: shows the source under it, or its
// text whole.
function pressable(area: Element): boolean {
  return area.closest(PRESSABLE_FLOW) !== null;
}

function areaOf(page: HTMLElement, blockNumber: number): HTMLElement | undefined {
  return [...page.querySelectorAll<HTMLElement>(`.area[data-block-translated="${blockNumber}"]`)]
    .filter(standing)
    .at(-1);
}

// Whether a press fell on the scroll bar of the element pressed, which is no press on what it
// shows.
function onScrollBar(event: MouseEvent): boolean {
  const target = event.target;
  return (
    target instanceof HTMLElement &&
    target.clientWidth > 0 &&
    (event.offsetX >= target.clientWidth || event.offsetY >= target.clientHeight)
  );
}

// Lets the reader select on the pages and press a highlight drawn there, while the view is on
// show and marking is on.
export function usePageSelecting(
  viewer: RefObject<HTMLDivElement | null>,
  drawing: SelectionDrawing,
  selecting: PageSelecting | undefined,
  onShow: boolean,
): void {
  const current = useRef(selecting);
  useEffect(() => {
    current.current = selecting;
  }, [selecting]);
  const enabled = selecting !== undefined;

  useEffect(() => {
    const root = viewer.current;
    if (root === null || !enabled || !onShow) {
      return;
    }
    let press: Press | undefined;
    // The selection drawn by boxes, from the span pressed to the span the pointer is over.
    let drawn: { from: Span; to: Span } | undefined;
    // The passage handed over: kept, with its clear, to be handed over again where it stands
    // after a scroll; native where the browser's selection holds it.
    let handed:
      | { anchors: Anchors; at: () => DOMRect | undefined; clear: () => void; native: boolean }
      | undefined;
    let pointer = { x: 0, y: 0 };
    let frame: number | undefined;
    let moving: number | undefined;
    let settling: number | undefined;
    let buttonHeld = false;
    let dragged = false;

    function nativeRange(): Range | undefined {
      const selection = getSelection();
      if (selection === null || selection.rangeCount === 0 || selection.isCollapsed) {
        return undefined;
      }
      const range = selection.getRangeAt(0);
      return root?.contains(range.commonAncestorContainer) === true ||
        range.intersectsNode(root as Node)
        ? range
        : undefined;
    }

    function letGo(): void {
      if (drawn !== undefined) {
        drawn = undefined;
        drawing.clear();
      }
      if (handed !== undefined) {
        const { native } = handed;
        handed = undefined;
        if (native && nativeRange() !== undefined) {
          getSelection()?.removeAllRanges();
        }
        current.current?.onSelect(undefined);
      }
    }

    function handOver(anchors: Anchors, at: () => DOMRect | undefined, native: boolean): void {
      const marking = current.current;
      const where = at();
      if (marking === undefined || where === undefined) {
        letGo();
        return;
      }
      const kept = { anchors, at, native, clear: () => undefined };
      kept.clear = () => {
        if (handed === kept) {
          letGo();
        }
      };
      handed = kept;
      marking.onSelect({ anchors, at: where, clear: kept.clear });
    }

    // Hands the same passage over again, where it stands now.
    function handAgain(): void {
      moving = undefined;
      const marking = current.current;
      const at = handed?.at();
      if (marking !== undefined && handed !== undefined && at !== undefined) {
        marking.onSelect({ anchors: handed.anchors, at, clear: handed.clear });
      }
    }

    function moved(): void {
      if (handed !== undefined && moving === undefined) {
        moving = requestAnimationFrame(handAgain);
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

    // The span under a point. To begin a selection, only on a line, or within reach of one, or
    // on an area; otherwise the nearest line of the page under the point, or of the nearest
    // page, where an area that covers the line stands for its block whole.
    function spotAt(x: number, y: number, beginning: boolean): Spot | undefined {
      const marking = current.current;
      if (marking === undefined) {
        return undefined;
      }
      const { text } = marking;
      const hit = document.elementFromPoint(x, y);
      const onPage = hit?.closest<HTMLElement>(PAGE_SELECTOR) ?? undefined;
      const page = onPage !== undefined && root?.contains(onPage) === true ? onPage : undefined;
      const found = page ?? (beginning ? undefined : nearestPage(y));
      if (found === undefined) {
        return undefined;
      }
      const translated = found.dataset.translated !== undefined;
      const area = page === undefined ? undefined : hit?.closest<HTMLElement>(AREA_SELECTOR);
      if (translated && area !== undefined && area !== null && standing(area)) {
        const blockNumber = Number(area.dataset.blockTranslated);
        return { span: text.wholeBlock(blockNumber), page: found, area };
      }
      // A page is read off its drawing, if it was not yet, when a pointer first comes to it.
      text.read(Number(found.dataset.page));
      const point = pointOn(found, x, y);
      const rect = found.getBoundingClientRect();
      const near = nearestLine(
        text.onPages.get(Number(found.dataset.page)) ?? [],
        point,
        rect.height / rect.width,
      );
      if (near === undefined || (beginning && near.depths > START_REACH)) {
        return undefined;
      }
      const { line } = near;
      const covering = translated ? areaOf(found, line.blockNumber) : undefined;
      if (covering === undefined) {
        return { span: text.spanAt(line, point), page: found, area: undefined };
      }
      return beginning
        ? undefined
        : { span: text.wholeBlock(line.blockNumber), page: found, area: covering };
    }

    function draw(from: Span, to: Span): void {
      const marking = current.current;
      if (marking === undefined) {
        return;
      }
      const spans = stretched(marking.text, from, to);
      drawing.show(
        spans.flatMap((span) => marking.text.coveredBoxes(span)),
        spans.map((span) => span.blockNumber),
      );
      drawn = { from, to };
    }

    // Where a selection by boxes ends on the screen: the last line it takes, on the page the
    // pointer was last on where that page shows it, or the area that holds the last block
    // where the lines are covered.
    function endOf(spans: Span[], focus: HTMLElement): DOMRect | undefined {
      const marking = current.current;
      const last = spans.at(-1);
      if (marking === undefined || last === undefined) {
        return undefined;
      }
      const lastBox = marking.text.coveredBoxes(last).at(-1);
      if (lastBox === undefined) {
        return undefined;
      }
      const pages = [focus, ...(root?.querySelectorAll<HTMLElement>(PAGE_SELECTOR) ?? [])];
      const page = pages.find(
        (each) => Number(each.dataset.page) === lastBox.page && each.checkVisibility(),
      );
      if (page === undefined) {
        return undefined;
      }
      const area =
        page.dataset.translated !== undefined ? areaOf(page, last.blockNumber) : undefined;
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

    function handOverDrawn(focus: HTMLElement): void {
      const marking = current.current;
      if (marking === undefined || drawn === undefined) {
        return;
      }
      const spans = stretched(marking.text, drawn.from, drawn.to);
      const anchors = anchorsOf(marking.text, spans, marking.documentId);
      if (anchors === undefined) {
        letGo();
        return;
      }
      handOver(anchors, () => endOf(spans, focus), false);
    }

    // The browser's selection, once it has settled, as the blocks of the areas it touches.
    function settle(): void {
      settling = undefined;
      const marking = current.current;
      const range = nativeRange();
      const areas =
        range === undefined
          ? []
          : [...(root?.querySelectorAll<HTMLElement>(TRANSLATED_AREAS) ?? [])].filter(
              (area) => standing(area) && holdsTextOf(range, area),
            );
      if (marking === undefined || areas.length === 0) {
        if (handed?.native === true) {
          handed = undefined;
          marking?.onSelect(undefined);
        }
        return;
      }
      const blocks = [...new Set(areas.map((area) => Number(area.dataset.blockTranslated)))].sort(
        (one, other) => one - other,
      );
      const anchors = anchorsOf(
        marking.text,
        blocks.map((blockNumber) => marking.text.wholeBlock(blockNumber)),
        marking.documentId,
      );
      if (anchors === undefined) {
        return;
      }
      // The same blocks are the same passage: handed over again where it stands, with the
      // clear an action may already hold.
      if (
        handed?.native === true &&
        handed.anchors.length === anchors.length &&
        handed.anchors.every(
          (anchor, index) => anchor.block_number === anchors[index]?.block_number,
        )
      ) {
        handAgain();
        return;
      }
      if (drawn !== undefined) {
        drawn = undefined;
        drawing.clear();
      }
      handOver(
        anchors,
        () => [...(nativeRange()?.getClientRects() ?? [])].filter((rect) => rect.width > 0).at(-1),
        true,
      );
    }

    function settleLater(wait: number): void {
      window.clearTimeout(settling);
      settling = window.setTimeout(settle, wait);
    }

    function selectionChanged(): void {
      // While the mouse is held the selection grows; it is read when the button is let go.
      if (!buttonHeld) {
        settleLater(SETTLED);
      }
    }

    // Follows the pointer: the selection runs from the press to the span under it, and the
    // view scrolls while it is held at an edge.
    function follow(): void {
      frame = undefined;
      if (press === undefined || !press.selecting || press.spot === undefined) {
        return;
      }
      const focus = spotAt(pointer.x, pointer.y, false);
      // Within the same word nothing is drawn again.
      if (focus !== undefined && !(drawn !== undefined && sameSpan(drawn.to, focus.span))) {
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
        const number = Number(spot.page.dataset.page);
        const point = pointOn(spot.page, x, y);
        const ids = new Set(
          (marking.pageMarks.get(number) ?? [])
            .filter((mark) =>
              mark.spans
                .flatMap((span) => marking.text.coveredBoxes(span))
                .some(
                  ({ page, box }) =>
                    page === number &&
                    point.x >= box.left &&
                    point.x <= box.left + box.width &&
                    point.y >= box.top &&
                    point.y <= box.top + box.height,
                ),
            )
            .map((mark) => mark.id),
        );
        under = marking.marked.filter((each) => ids.has(each.id));
      }
      const chosen = lastBegun(under);
      if (chosen !== undefined) {
        marking.onPress(chosen.id);
      }
    }

    // A finger held still on a line selects its block whole.
    function hold(held: Press): void {
      held.timer = undefined;
      const marking = current.current;
      if (press !== held || held.spot === undefined || marking === undefined) {
        return;
      }
      held.held = true;
      const whole = marking.text.wholeBlock(held.spot.span.blockNumber);
      draw(whole, whole);
      handOverDrawn(held.spot.page);
    }

    function pressed(event: PointerEvent): void {
      dragged = false;
      if (!event.isPrimary) {
        // A second finger is a gesture of the browser's: what the first began is forgotten,
        // and a block the first was held on is let go.
        window.clearTimeout(press?.timer);
        if (press?.held === true) {
          letGo();
        }
        press = undefined;
        return;
      }
      if (event.button !== 0) {
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
        held: false,
        timer: undefined,
      };
      // A press on the text of a translation is the browser's own.
      if (spot === undefined || spot.area !== undefined) {
        return;
      }
      if (touch) {
        const held = press;
        held.timer = window.setTimeout(() => hold(held), HOLD);
      } else {
        // The dragging of what is under the press gives way to the selection by boxes.
        event.preventDefault();
      }
    }

    function pointerMoved(event: PointerEvent): void {
      if (press === undefined || event.pointerId !== press.pointerId) {
        return;
      }
      pointer = { x: event.clientX, y: event.clientY };
      const distance = Math.hypot(event.clientX - press.x, event.clientY - press.y);
      if (press.touch) {
        if (distance > STILL_FINGER) {
          window.clearTimeout(press.timer);
          press = undefined;
        }
        return;
      }
      if (!press.selecting) {
        if (distance <= STILL_MOUSE || press.spot === undefined || press.spot.area !== undefined) {
          return;
        }
        press.selecting = true;
        root?.setPointerCapture(press.pointerId);
        draw(press.spot.span, press.spot.span);
      }
      if (frame === undefined) {
        frame = requestAnimationFrame(follow);
      }
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
      if (ended.selecting && ended.spot !== undefined) {
        dragged = true;
        const focus = spotAt(event.clientX, event.clientY, false) ?? ended.spot;
        draw(ended.spot.span, focus.span);
        handOverDrawn(focus.page);
        return;
      }
      const { spot } = ended;
      const still =
        Math.hypot(event.clientX - ended.x, event.clientY - ended.y) <=
        (ended.touch ? STILL_FINGER : STILL_MOUSE);
      // An area that does something when pressed does that alone.
      if (!ended.held && still && spot !== undefined && !(spot.area && pressable(spot.area))) {
        pressHighlight(spot, event.clientX, event.clientY);
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

    // A drag ends in a click on what is under it, which is no press of an area.
    function clicked(event: MouseEvent): void {
      if (dragged) {
        dragged = false;
        event.stopPropagation();
        event.preventDefault();
      }
    }

    function buttonDown(event: PointerEvent): void {
      if (event.pointerType === "mouse") {
        buttonHeld = true;
      }
    }

    function buttonUp(event: PointerEvent): void {
      if (event.pointerType === "mouse" && buttonHeld) {
        buttonHeld = false;
        settleLater(0);
      }
    }

    // A press anywhere else lets go of the selection, but not one on what is offered on it,
    // nor one on a scroll bar.
    function pressedElsewhere(event: MouseEvent): void {
      const target = event.target;
      if (
        !(target instanceof Element && target.closest(`.${SELECTION_ACTIONS_CLASS}`) !== null) &&
        !onScrollBar(event) &&
        !(target instanceof Node && root?.contains(target))
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
    root.addEventListener("pointermove", pointerMoved);
    root.addEventListener("pointerup", released);
    root.addEventListener("pointercancel", cancelled);
    root.addEventListener("click", clicked, true);
    document.addEventListener("pointerdown", buttonDown, true);
    document.addEventListener("pointerup", buttonUp, true);
    document.addEventListener("selectionchange", selectionChanged);
    document.addEventListener("mousedown", pressedElsewhere);
    document.addEventListener("keydown", keyed);
    document.addEventListener("scroll", moved, { capture: true, passive: true });
    window.addEventListener("resize", moved);
    return () => {
      root.removeEventListener("pointerdown", pressed);
      root.removeEventListener("pointermove", pointerMoved);
      root.removeEventListener("pointerup", released);
      root.removeEventListener("pointercancel", cancelled);
      root.removeEventListener("click", clicked, true);
      document.removeEventListener("pointerdown", buttonDown, true);
      document.removeEventListener("pointerup", buttonUp, true);
      document.removeEventListener("selectionchange", selectionChanged);
      document.removeEventListener("mousedown", pressedElsewhere);
      document.removeEventListener("keydown", keyed);
      document.removeEventListener("scroll", moved, { capture: true });
      window.removeEventListener("resize", moved);
      window.clearTimeout(press?.timer);
      window.clearTimeout(settling);
      for (const pending of [frame, moving]) {
        if (pending !== undefined) {
          cancelAnimationFrame(pending);
        }
      }
      letGo();
    };
  }, [viewer, drawing, enabled, onShow]);
}
