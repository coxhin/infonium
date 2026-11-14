import type { Anchor } from "./client";

const ANCHOR_HIGHLIGHT = "anchor";

// The element marked with a block's number holds exactly that block's text, so an anchor's
// character range can be found by walking the element's text nodes.
function rangeIn(blockElement: Element, start: number, end: number): Range {
  const range = document.createRange();
  const texts = document.createTreeWalker(blockElement, NodeFilter.SHOW_TEXT);
  let passed = 0;
  let startSet = false;
  for (let text = texts.nextNode(); text !== null; text = texts.nextNode()) {
    const length = text.textContent?.length ?? 0;
    if (!startSet && start < passed + length) {
      range.setStart(text, start - passed);
      startSet = true;
    }
    if (startSet && end <= passed + length) {
      range.setEnd(text, end - passed);
      return range;
    }
    passed += length;
  }
  throw new Error(`characters ${start}–${end} lie outside the block's text`);
}

export function showAnchor(anchor: Anchor): void {
  const blockElement = document.querySelector(`[data-block="${anchor.block}"]`);
  if (blockElement === null) {
    throw new Error(`block ${anchor.block} is not on the page`);
  }
  const range = rangeIn(blockElement, anchor.start, anchor.end);
  CSS.highlights.set(ANCHOR_HIGHLIGHT, new Highlight(range));
  // The jump is instant: an animated scroll aims at where the passage was when it started,
  // and misses once images on the way load and push the passage down.
  (range.startContainer.parentElement ?? blockElement).scrollIntoView({ block: "center" });
}

export function hideAnchor(): void {
  CSS.highlights.delete(ANCHOR_HIGHLIGHT);
}
