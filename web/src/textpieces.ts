import { FORMULA_ATTRIBUTE } from "./formula";

// How the words of a block, or of its translation, lie in the element that draws them: the
// reader's own walk over the element's text, and the ways between offsets of that text and
// places of the page. Offsets here count the units a string and a DOM range count.

// One piece of an element's text: a text node, or a formula, whose text is the LaTeX it says
// of itself and whose every offset lies at the element that draws it.
export type TextPiece = { node: Text; text: string } | { formula: Element; text: string };

// Which end of a stretch a place of the page is taken as. A formula is taken whole, so a start
// that lies inside one is put before it, and an end that lies inside one after it.
export type Side = "start" | "end";

const FORMULA_SELECTOR = `[${FORMULA_ATTRIBUTE}]`;

// The node after the walker's current one and all it holds, or none at the end of the walk.
function nextOutside(walker: TreeWalker): Node | null {
  while (walker.parentNode() !== null) {
    const after = walker.nextSibling();
    if (after !== null) {
      return after;
    }
  }
  return null;
}

// The pieces of an element that hold its text, in their order.
export function piecesOf(element: Element): TextPiece[] {
  const pieces: TextPiece[] = [];
  const walker = document.createTreeWalker(element, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node !== null) {
    if (node instanceof Element && node.hasAttribute(FORMULA_ATTRIBUTE)) {
      pieces.push({ formula: node, text: node.getAttribute(FORMULA_ATTRIBUTE) ?? "" });
      node = walker.nextSibling() ?? nextOutside(walker);
      continue;
    }
    if (node instanceof Text) {
      pieces.push({ node, text: node.data });
    }
    node = walker.nextNode();
  }
  return pieces;
}

// The text an element holds, as anchors are resolved against it.
export function textOf(element: Element): string {
  return piecesOf(element)
    .map((piece) => piece.text)
    .join("");
}

// The range of the page that shows characters start to end of an element's text. A range that
// begins or ends inside a formula takes in the whole of its drawing.
export function rangeIn(element: Element, start: number, end: number): Range {
  const range = document.createRange();
  let passed = 0;
  let startSet = false;
  for (const piece of piecesOf(element)) {
    const length = piece.text.length;
    if (!startSet && start < passed + length) {
      if ("node" in piece) {
        range.setStart(piece.node, start - passed);
      } else {
        range.setStartBefore(piece.formula);
      }
      startSet = true;
    }
    if (startSet && end <= passed + length) {
      if ("node" in piece) {
        range.setEnd(piece.node, end - passed);
      } else {
        range.setEndAfter(piece.formula);
      }
      return range;
    }
    passed += length;
  }
  throw new Error("页面上的这一段比引文的位置短，找不到这段引文。");
}

function nodeRange(node: Node, whole: boolean): Range {
  const range = document.createRange();
  if (whole) {
    range.selectNode(node);
  } else {
    range.selectNodeContents(node);
  }
  return range;
}

// The offset of an element's text at a place of the page: the length of the text that lies
// before the place, the whole formula counted where the place lies inside one as an end. A
// place before the element is at its start, one after it at its end. Whatever a formula holds
// inside, it is found by the attribute that carries its LaTeX.
export function offsetAt(
  pieces: TextPiece[],
  container: Node,
  offset: number,
  side: Side,
): number {
  const element = container instanceof Element ? container : container.parentElement;
  const formula = element?.closest(FORMULA_SELECTOR) ?? null;
  let passed = 0;
  for (const piece of pieces) {
    if ("node" in piece) {
      if (container === piece.node) {
        return passed + offset;
      }
      if (nodeRange(piece.node, false).comparePoint(container, offset) < 0) {
        return passed;
      }
    } else {
      if (piece.formula === formula) {
        return side === "start" ? passed : passed + piece.text.length;
      }
      const around = nodeRange(piece.formula, true);
      const place = around.comparePoint(container, offset);
      if (place < 0) {
        return passed;
      }
      if (place === 0) {
        const atStart = around.startContainer === container && around.startOffset === offset;
        return atStart || side === "start" ? passed : passed + piece.text.length;
      }
    }
    passed += piece.text.length;
  }
  return passed;
}

// The stretch of the text that the character at an offset lies in: the character itself, or
// the whole of the formula it belongs to.
export function characterAt(
  pieces: TextPiece[],
  offset: number,
): { start: number; end: number } | undefined {
  let passed = 0;
  for (const piece of pieces) {
    const length = piece.text.length;
    if (offset < passed + length) {
      if ("formula" in piece) {
        return { start: passed, end: passed + length };
      }
      const code = piece.text.codePointAt(offset - passed) ?? 0;
      return { start: offset, end: offset + String.fromCodePoint(code).length };
    }
    passed += length;
  }
  return undefined;
}
