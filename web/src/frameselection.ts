import { useEffect, useMemo, useRef, type RefObject } from "react";
import type { Anchors, Block, ItemDocument } from "./client";
import type { FrameHighlights } from "./framehighlights";
import { heldTextOf, trimmedSpan, type Span } from "./framewords";
import { NO_MARKING, type Marking, type Selected } from "./marking";
import { TRANSLATION_ELEMENT, type FramedPage, type Holders } from "./pagewriter";

// A passage the reader selects in the framed page a web article was read from: from the
// selection to its anchors, and how it is handed over to the reader.

// How long a selection made otherwise than by dragging, with the keyboard or by the handles of
// a touch screen, stays as it is before it is taken as made, in milliseconds.
const SETTLING = 400;
// A line of the selection is one whose boxes are no taller than this many times its last
// character's: taller boxes are those of whole elements the selection holds.
const LINE_HEIGHTS = 2;

// Places in the page are found by walking from node to node, never by comparing two places: a
// browser compares them by counting the siblings of the nodes compared, thousands in a long page.

// The node a place in the page stands just before, at any depth; none at the page's end.
function nodeAfter(container: Node, offset: number): Node | null {
  if (container.nodeType === Node.TEXT_NODE) {
    return offset < (container.textContent ?? "").length ? container : following(container);
  }
  return container.childNodes[offset] ?? following(container);
}

// The node a place in the page stands just after, the deepest of the nodes that end there;
// none at the page's start.
function nodeBefore(container: Node, offset: number): Node | null {
  if (container.nodeType === Node.TEXT_NODE) {
    return offset > 0 ? container : preceding(container);
  }
  const before = container.childNodes[offset - 1];
  return before === undefined ? preceding(container) : deepestLast(before);
}

function deepestLast(node: Node): Node {
  let last = node;
  while (last.lastChild !== null) {
    last = last.lastChild;
  }
  return last;
}

// The node after a node and all it holds.
function following(node: Node): Node | null {
  for (let at: Node | null = node; at !== null; at = at.parentNode) {
    if (at.nextSibling !== null) {
      return at.nextSibling;
    }
  }
  return null;
}

// The node before a node, the deepest of those that end there.
function preceding(node: Node): Node | null {
  const sibling = node.previousSibling;
  return sibling === null ? node.parentNode : deepestLast(sibling);
}

function elementOf(node: Node): Element | null {
  return node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement;
}

// The elements that stand for blocks or cells that a selection reaches into, in the page's
// order: from the first that holds or follows its start to the last that holds or precedes its
// end, each found by walking the page from that end of the selection to the nearest of them.
function holdersIn(holders: Holders, selection: Range): Element[] {
  const start = nodeAfter(selection.startContainer, selection.startOffset);
  let first = start === null ? null : (elementOf(start)?.closest(holders.selector) ?? null);
  for (
    let node = start;
    first === null && node !== null;
    node = node.firstChild ?? following(node)
  ) {
    first = holders.places.has(node as Element) ? (node as Element) : null;
  }
  let last: Element | null = null;
  for (
    let node = nodeBefore(selection.endContainer, selection.endOffset);
    last === null && node !== null;
    node = preceding(node)
  ) {
    last = elementOf(node)?.closest(holders.selector) ?? null;
  }
  const from = first === null ? undefined : holders.places.get(first)?.order;
  const to = last === null ? undefined : holders.places.get(last)?.order;
  return from === undefined || to === undefined ? [] : holders.ordered.slice(from, to + 1);
}

// A selection's part in an element it reaches into: the element's contents, from the
// selection's start where that lies in it, to the selection's end where that lies in it.
function partIn(selection: Range, element: Element): Range {
  const part = element.ownerDocument.createRange();
  part.selectNodeContents(element);
  if (element.contains(selection.startContainer)) {
    part.setStart(selection.startContainer, selection.startOffset);
  }
  if (element.contains(selection.endContainer)) {
    part.setEnd(selection.endContainer, selection.endOffset);
  }
  return part;
}

function holdsWords(range: Range): boolean {
  return range.toString().trim() !== "";
}

// The stretch of an element's text that a selection covers words of. Where the selection
// holds the element whole, where the element shows the text's translation in its place, or
// where the selection covers words of the translation shown after the text and none of the
// text, it is the whole text.
function selectedIn(
  framed: FramedPage,
  element: Element,
  text: string,
  selection: Range,
): Span | undefined {
  const whole =
    !element.contains(selection.startContainer) && !element.contains(selection.endContainer);
  if (whole) {
    return (element.textContent ?? "").trim() === "" ? undefined : trimmedSpan(text);
  }
  const part = partIn(selection, element);
  if (!holdsWords(part)) {
    return undefined;
  }
  const shown = framed.shownIn(element);
  if (shown === "translation") {
    return trimmedSpan(text);
  }
  const span = framed.wordsOf(element, text).spanOf(part);
  if (span !== null) {
    return span;
  }
  const translation = shown === "both" ? element.querySelector(TRANSLATION_ELEMENT) : null;
  return translation !== null && holdsWords(partIn(part, translation))
    ? trimmedSpan(text)
    : undefined;
}

// The passage a selection is in the document: for each block whose elements it covers words
// of, the stretch of the block's text those words are, in the order of the document. None
// where it covers no words of any.
export function selectedPassage(
  framed: FramedPage,
  holders: Holders,
  blocks: Block[],
  documentId: string,
  selection: Range,
): Anchors | undefined {
  const spans = new Map<number, Span>();
  for (const element of holdersIn(holders, selection)) {
    const place = holders.places.get(element);
    const block = place === undefined ? undefined : blocks[place.number];
    const held =
      block === undefined || place === undefined ? undefined : heldTextOf(block, place);
    const span =
      held === undefined ? undefined : selectedIn(framed, element, held.text, selection);
    if (place === undefined || held === undefined || span === undefined) {
      continue;
    }
    const known = spans.get(place.number);
    const start = span.start + held.offset;
    const end = span.end + held.offset;
    spans.set(place.number, {
      start: Math.min(start, known?.start ?? start),
      end: Math.max(end, known?.end ?? end),
    });
  }
  const anchors = [...spans.entries()]
    .sort(([first], [second]) => first - second)
    .map(([number, { start, end }]) => ({
      document_id: documentId,
      block_number: number,
      start,
      end,
      quote: [...(blocks[number]?.text ?? "")].slice(start, end).join(""),
    }));
  const [first, ...rest] = anchors;
  return first === undefined ? undefined : [first, ...rest];
}

// The last character of a selection that is not white space, as a range of the page, and the
// part of the selection in the text that holds it, up to it. The page is walked back from the
// selection's end, never further than its start.
function lastCharacter(selection: Range): { character: Range; line: Range } | undefined {
  const page = selection.startContainer.ownerDocument;
  const start = selection.startContainer;
  if (page === null) {
    return undefined;
  }
  for (
    let node = nodeBefore(selection.endContainer, selection.endOffset);
    node !== null;
    node = node.contains(start) ? null : preceding(node)
  ) {
    if (node.nodeType !== Node.TEXT_NODE) {
      continue;
    }
    const text = node.textContent ?? "";
    const from = node === start ? selection.startOffset : 0;
    let end = node === selection.endContainer ? selection.endOffset : text.length;
    while (end > from && (text[end - 1] ?? " ").trim() === "") {
      end -= 1;
    }
    if (end > from) {
      const character = page.createRange();
      character.setStart(node, Math.max(from, end - 1));
      character.setEnd(node, end);
      if (character.getBoundingClientRect().height > 0) {
        const line = page.createRange();
        line.setStart(node, from);
        line.setEnd(node, end);
        return { character, line };
      }
    }
  }
  return undefined;
}

// Where a selection ends, as the window sees it through the frame: the boxes of its last line,
// up to and with its last character, as far as the text that holds that character reaches.
function endOf(selection: Range, frame: HTMLIFrameElement): DOMRect | undefined {
  const found = lastCharacter(selection);
  const last = found?.character.getBoundingClientRect();
  if (found === undefined || last === undefined) {
    return undefined;
  }
  const left = [...found.line.getClientRects()]
    .filter(
      (box) =>
        box.height > 0 &&
        box.height <= last.height * LINE_HEIGHTS &&
        box.top < last.bottom &&
        box.bottom > last.top &&
        box.left <= last.left,
    )
    .reduce((leftmost, box) => Math.min(leftmost, box.left), last.left);
  const place = frame.getBoundingClientRect();
  return new DOMRect(
    place.left + frame.clientLeft + left,
    place.top + frame.clientTop + last.top,
    last.right - left,
    last.height,
  );
}

function samePlace(one: DOMRect, other: DOMRect): boolean {
  return (
    one.x === other.x &&
    one.y === other.y &&
    one.width === other.width &&
    one.height === other.height
  );
}

// What the view does with the page's selection when it writes the page: which elements that
// stand for blocks the selection reaches into, and letting the selection go.
export type FrameSelection = {
  selectedHolders: () => Element[];
  letGo: () => void;
};

// The reader's selections and presses in the page, handed over through the marking: a
// selection once the pointer that made it is let go, or once one made otherwise has settled;
// that it is gone as soon as it is; and, while one is handed over, where it ends whenever the
// page or the window scrolls it elsewhere, with the same anchors. With nothing to mark, nothing
// is listened for.
export function useFrameSelection(
  frame: RefObject<HTMLIFrameElement | null>,
  framed: FramedPage | undefined,
  highlights: FrameHighlights | undefined,
  holders: Holders | undefined,
  itemDocument: ItemDocument,
  marking: Marking,
  onShow: boolean,
): FrameSelection {
  const latest = useRef({ itemDocument, marking });
  latest.current = { itemDocument, marking };
  const current = useRef<FrameSelection>({ selectedHolders: () => [], letGo: () => undefined });
  const inert = marking === NO_MARKING;
  useEffect(() => {
    if (
      framed === undefined ||
      highlights === undefined ||
      holders === undefined ||
      !onShow ||
      inert
    ) {
      return;
    }
    const page = framed.document;
    const view = framed.view;
    let pressing = false;
    let handed: Selected | undefined;
    let settling = 0;
    let moving = 0;

    function clear() {
      page.getSelection()?.removeAllRanges();
    }

    function rangeOf(): Range | undefined {
      const selection = page.getSelection();
      return selection !== null && selection.rangeCount > 0 && !selection.isCollapsed
        ? selection.getRangeAt(0)
        : undefined;
    }

    function letGo() {
      clearTimeout(settling);
      if (handed !== undefined) {
        handed = undefined;
        latest.current.marking.onSelect(undefined);
      }
    }

    function handOver() {
      clearTimeout(settling);
      const range = rangeOf();
      const { itemDocument, marking } = latest.current;
      const anchors =
        range === undefined || framed === undefined || holders === undefined
          ? undefined
          : selectedPassage(framed, holders, itemDocument.blocks, itemDocument.id, range);
      const at =
        range === undefined || anchors === undefined || frame.current === null
          ? undefined
          : endOf(range, frame.current);
      if (anchors === undefined || at === undefined) {
        letGo();
        return;
      }
      handed = { anchors, at, clear };
      marking.onSelect(handed);
    }

    function pressed() {
      pressing = true;
    }
    function released() {
      pressing = false;
      setTimeout(handOver);
    }
    function changed() {
      clearTimeout(settling);
      if (page.getSelection()?.isCollapsed !== false) {
        letGo();
      } else if (!pressing) {
        settling = window.setTimeout(handOver, SETTLING);
      }
    }
    // The passage stays as it was; only where it ends on the screen is told again, and only
    // when that has changed.
    function moved() {
      if (handed === undefined || moving !== 0) {
        return;
      }
      moving = requestAnimationFrame(() => {
        moving = 0;
        const range = rangeOf();
        const at =
          range === undefined || frame.current === null ? undefined : endOf(range, frame.current);
        if (handed === undefined) {
          return;
        }
        if (at === undefined) {
          letGo();
        } else if (!samePlace(at, handed.at)) {
          handed = { ...handed, at };
          latest.current.marking.onSelect(handed);
        }
      });
    }
    function clicked(event: MouseEvent) {
      const highlightId =
        holders === undefined ? undefined : highlights?.pressed(holders, event);
      if (highlightId !== undefined) {
        latest.current.marking.onPress(highlightId);
      }
    }

    current.current = {
      selectedHolders: () => {
        const range = rangeOf();
        return range === undefined || holders === undefined ? [] : holdersIn(holders, range);
      },
      letGo: () => {
        clear();
        letGo();
      },
    };
    const passive = { passive: true };
    const scrolling = { capture: true, passive: true };
    page.addEventListener("pointerdown", pressed, passive);
    page.addEventListener("pointerup", released, passive);
    page.addEventListener("pointercancel", released, passive);
    page.addEventListener("selectionchange", changed);
    page.addEventListener("click", clicked);
    view.addEventListener("scroll", moved, scrolling);
    addEventListener("scroll", moved, scrolling);
    addEventListener("resize", moved, passive);
    return () => {
      clearTimeout(settling);
      cancelAnimationFrame(moving);
      page.removeEventListener("pointerdown", pressed);
      page.removeEventListener("pointerup", released);
      page.removeEventListener("pointercancel", released);
      page.removeEventListener("selectionchange", changed);
      page.removeEventListener("click", clicked);
      view.removeEventListener("scroll", moved, scrolling);
      removeEventListener("scroll", moved, scrolling);
      removeEventListener("resize", moved);
      current.current = { selectedHolders: () => [], letGo: () => undefined };
      letGo();
    };
  }, [frame, framed, highlights, holders, onShow, inert]);
  return useMemo(
    () => ({
      selectedHolders: () => current.current.selectedHolders(),
      letGo: () => current.current.letGo(),
    }),
    [],
  );
}
