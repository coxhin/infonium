import { useEffect, useRef, type RefObject } from "react";
import type { ItemDocument } from "./client";
import { NO_MARKING, type Marking } from "./marking";
import type { PageMarks } from "./pagemarks";
import type { Holders } from "./pagewriter";

// How long a selection made otherwise than by dragging, with the keyboard or by the handles of
// a touch screen, stays as it is before it is taken as made, in milliseconds.
const SETTLING = 400;
// A line of the selection is one whose boxes are no taller than this many times its last
// character's: taller boxes are those of whole elements the selection holds.
const LINE_HEIGHTS = 2;

// The last character of a selection that is not white space, as a range of the page.
function lastCharacter(selection: Range): Range | undefined {
  const page = selection.startContainer.ownerDocument;
  const common = selection.commonAncestorContainer;
  if (page === null) {
    return undefined;
  }
  const walker = page.createTreeWalker(common, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      selection.intersectsNode(node) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP,
  });
  let node: Node | null =
    common.nodeType === Node.TEXT_NODE ? common : walker.lastChild();
  if (node !== null) {
    walker.currentNode = node;
  }
  for (; node !== null; node = walker.previousNode()) {
    const text = node.textContent ?? "";
    const from = node === selection.startContainer ? selection.startOffset : 0;
    let end = node === selection.endContainer ? selection.endOffset : text.length;
    while (end > from && (text[end - 1] ?? " ").trim() === "") {
      end -= 1;
    }
    if (end > from) {
      const character = page.createRange();
      character.setStart(node, Math.max(from, end - 1));
      character.setEnd(node, end);
      if (character.getBoundingClientRect().height > 0) {
        return character;
      }
    }
  }
  return undefined;
}

// Where a selection ends, as the window sees it through the frame: the boxes of its last line,
// up to and with its last character.
function endOf(selection: Range, frame: HTMLIFrameElement): DOMRect | undefined {
  const last = lastCharacter(selection)?.getBoundingClientRect();
  if (last === undefined) {
    return undefined;
  }
  const left = [...selection.getClientRects()]
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

// The reader's selections and presses in the page, handed over through the marking: a
// selection once the pointer that made it is let go, or once one made otherwise has settled;
// that it is gone as soon as it is; and, while one is handed over, where it ends as the page
// or the window scrolls. With nothing to mark, nothing is listened for.
export function usePageMarking(
  frame: RefObject<HTMLIFrameElement | null>,
  marks: PageMarks | undefined,
  holders: Holders | undefined,
  itemDocument: ItemDocument,
  marking: Marking,
  onShow: boolean,
): void {
  const latest = useRef({ itemDocument, marking });
  latest.current = { itemDocument, marking };
  const inert = marking === NO_MARKING;
  useEffect(() => {
    if (marks === undefined || holders === undefined || !onShow || inert) {
      return;
    }
    const page = marks.framed.document;
    const view = marks.framed.view;
    let pressing = false;
    let handed = false;
    let settling = 0;
    let moving = 0;

    function clear() {
      page.getSelection()?.removeAllRanges();
    }

    function handOver() {
      clearTimeout(settling);
      const selection = page.getSelection();
      const range =
        selection !== null && selection.rangeCount > 0 && !selection.isCollapsed
          ? selection.getRangeAt(0)
          : undefined;
      const { itemDocument, marking } = latest.current;
      const anchors =
        range === undefined || holders === undefined
          ? undefined
          : marks?.selected(holders, itemDocument.blocks, itemDocument.id, range);
      const at =
        range === undefined || anchors === undefined || frame.current === null
          ? undefined
          : endOf(range, frame.current);
      if (anchors === undefined || at === undefined) {
        if (handed) {
          handed = false;
          marking.onSelect(undefined);
        }
        return;
      }
      handed = true;
      marking.onSelect({ anchors, at, clear });
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
        handOver();
      } else if (!pressing) {
        settling = window.setTimeout(handOver, SETTLING);
      }
    }
    function moved() {
      if (handed && moving === 0) {
        moving = requestAnimationFrame(() => {
          moving = 0;
          handOver();
        });
      }
    }
    function clicked(event: MouseEvent) {
      if (holders === undefined) {
        return;
      }
      const highlightId = marks?.pressed(holders, event);
      if (highlightId !== undefined) {
        latest.current.marking.onPress(highlightId);
      }
    }

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
      if (handed) {
        latest.current.marking.onSelect(undefined);
      }
    };
  }, [frame, marks, holders, onShow, inert]);
}
