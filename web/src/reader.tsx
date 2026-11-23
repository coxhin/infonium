import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import type { Anchor, ItemDocument, ItemEntry, TimePlace } from "./client";
import { BLOCK_ATTRIBUTE, SourceBlock, unitOffset, unitOffsets } from "./blocks";
import { originLabel } from "./entry";
import { FORMULA_ATTRIBUTE } from "./formula";
import type { Clock } from "./player";
import { inView, scrollerOf, scrolls } from "./scrolling";
import type { Setting } from "./setting";
import { Transcript, LINES_ATTRIBUTE } from "./transcript";
import { POINTED_CLASS, Row, TRANSLATION_ATTRIBUTE } from "./translated";
import type { TranslationState } from "./translation";

const DOCUMENT_ATTRIBUTE = "data-document";
const ANCHOR_HIGHLIGHT = "anchor";
const NOT_ON_SHOW = "这段引文所在的原文不在当前页面上";
const CHANGED = "原文已经变化，找不到这段引文";

// What the text view of a recording needs to show its transcript as lines that follow the
// playing: the clock of the player, the stretches last played from, and a way to play.
export type Playing = {
  clock: Clock;
  played: TimePlace[];
  onPlay: (place: TimePlace) => void;
};

// The text of a document, in the words the setting asks for. A recording's transcript is
// shown as lines once a translation is on show; in its source alone it is the text it always
// was.
export function Reader({
  entry,
  title,
  itemDocument,
  setting,
  translation,
  pointed,
  playing,
}: {
  entry: ItemEntry;
  title: string;
  itemDocument: ItemDocument;
  setting: Setting;
  translation: TranslationState;
  pointed: Anchor | undefined;
  playing: Playing | undefined;
}) {
  const blocks = itemDocument.blocks;
  const pointedHere = pointed?.document_id === itemDocument.id ? pointed : undefined;
  const asLines = playing !== undefined && setting !== "source";
  // The passage highlighted in this document goes when the document leaves the page. React
  // runs this before the effects of a reader that takes its place, so the passage that reader
  // is opened for stays.
  useEffect(() => hideAnchor, []);
  return (
    <article
      className="reader"
      {...{ [DOCUMENT_ATTRIBUTE]: itemDocument.id }}
    >
      <header>
        {entry.origin.kind === "link" ? (
          <a href={entry.origin.url} target="_blank" rel="noreferrer noopener">
            {originLabel(entry.origin)} ↗
          </a>
        ) : (
          <span>{originLabel(entry.origin)}</span>
        )}
        {blocks[0]?.kind !== "heading" && <h1>{title}</h1>}
      </header>
      {asLines ? (
        <>
          {/* The source blocks stay in the page, out of sight, so that citations resolve. */}
          {blocks.map((block, number) => (
            <SourceBlock key={number} block={block} number={number} hidden />
          ))}
          <Transcript
            documentId={itemDocument.id}
            blocks={blocks}
            translations={translation.blocks}
            setting={setting}
            again={translation.again}
            pointed={pointedHere}
            played={playing.played}
            clock={playing.clock}
            onAgain={translation.translateAgain}
            onPlay={playing.onPlay}
          />
        </>
      ) : (
        blocks.map((block, number) => (
          <Row
            key={number}
            block={block}
            number={number}
            setting={setting}
            translated={setting === "source" ? undefined : translation.blocks.get(number)}
            again={translation.again.get(number)}
            pointed={pointedHere?.block_number === number ? pointedHere : undefined}
            onAgain={translation.translateAgain}
          />
        ))
      )}
    </article>
  );
}

// One piece of a block's text as its element holds it: a text node, or a formula, whose text is
// the LaTeX it says of itself and whose every offset lies at the element that draws it.
type TextPiece = { node: Text; text: string } | { formula: Element; text: string };

// The pieces of a block's element that hold its text, in their order.
function piecesOf(blockElement: Element): TextPiece[] {
  const pieces: TextPiece[] = [];
  const walker = document.createTreeWalker(
    blockElement,
    NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
  );
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

// The text a block's element holds, as anchors are resolved against it.
export function textOf(blockElement: Element): string {
  return piecesOf(blockElement)
    .map((piece) => piece.text)
    .join("");
}

// The range of the page that shows characters start to end of a block's text. A range that
// begins or ends inside a formula takes in the whole of its drawing.
function rangeIn(blockElement: Element, start: number, end: number): Range {
  const range = document.createRange();
  let passed = 0;
  let startSet = false;
  for (const piece of piecesOf(blockElement)) {
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

// A scroll padding in pixels, as the browser computes it; "auto" sets nothing aside.
function paddingOf(computed: string): number {
  const pixels = parseFloat(computed);
  return Number.isNaN(pixels) ? 0 : pixels;
}

// Puts the passage in the middle of everything around it that has room to scroll, innermost
// first: a table that scrolls sideways, then the column that holds the reader. A passage
// larger than the view is put at its start. The jump is instant: an animated scroll aims at
// where the passage was when it started, and misses once images on the way load and push the
// passage down.
function bringIntoView(passage: Range): void {
  for (
    let ancestor = passage.startContainer.parentElement;
    ancestor !== null;
    ancestor = ancestor.parentElement
  ) {
    const isPage = ancestor === document.scrollingElement;
    const style = getComputedStyle(ancestor);
    const view = isPage ? new DOMRect(0, 0, 0, 0) : ancestor.getBoundingClientRect();
    const place = passage.getBoundingClientRect();
    if ((isPage || scrolls(style.overflowY)) && ancestor.scrollHeight > ancestor.clientHeight) {
      // What the scroll padding sets aside, such as the column held at the foot of the window
      // in one column, is not room to put the passage in.
      const above = paddingOf(style.scrollPaddingTop);
      const room = ancestor.clientHeight - above - paddingOf(style.scrollPaddingBottom);
      const margin = above + Math.max(0, (room - place.height) / 2);
      ancestor.scrollBy({ top: place.top - view.top - margin });
    }
    if ((isPage || scrolls(style.overflowX)) && ancestor.scrollWidth > ancestor.clientWidth) {
      const margin = Math.max(0, (ancestor.clientWidth - place.width) / 2);
      ancestor.scrollBy({ left: place.left - view.left - margin });
    }
  }
}

function blockElementOf(anchor: Anchor): Element {
  const blockElement = document.querySelector(
    `[${DOCUMENT_ATTRIBUTE}="${anchor.document_id}"] [${BLOCK_ATTRIBUTE}="${anchor.block_number}"]`,
  );
  if (blockElement === null) {
    throw new Error(NOT_ON_SHOW);
  }
  return blockElement;
}

// Refuses an anchor that does not point into the document on show, or whose range no longer
// holds the quoted text, against the document itself: the text view, where the passage is
// highlighted, may not have been drawn yet.
export function checkAnchor(itemDocument: ItemDocument, anchor: Anchor): void {
  if (anchor.document_id !== itemDocument.id) {
    throw new Error(NOT_ON_SHOW);
  }
  const text = itemDocument.blocks[anchor.block_number]?.text;
  if (text === undefined) {
    throw new Error(CHANGED);
  }
  const offsets = unitOffsets(text);
  if (anchor.end >= offsets.length || anchor.start > anchor.end) {
    throw new Error(CHANGED);
  }
  const quoted = text.slice(unitOffset(offsets, anchor.start), unitOffset(offsets, anchor.end));
  if (quoted !== anchor.quote) {
    throw new Error(CHANGED);
  }
}

// Highlights an anchor's passage, and refuses when the page does not hold the document the
// anchor points into or when the text there is no longer the quoted text. The passage is
// highlighted in the source block whether or not that block is on show.
export function highlightAnchor(anchor: Anchor): Range {
  const blockElement = blockElementOf(anchor);
  const text = textOf(blockElement);
  const offsets = unitOffsets(text);
  const start = unitOffset(offsets, anchor.start);
  const end = unitOffset(offsets, anchor.end);
  // The range may hold a formula's drawing as well, so the quote is checked against the text.
  if (text.slice(start, end) !== anchor.quote) {
    throw new Error(CHANGED);
  }
  const passage = rangeIn(blockElement, start, end);
  CSS.highlights.set(ANCHOR_HIGHLIGHT, new Highlight(passage));
  return passage;
}

// Highlights an anchor's passage and brings into view where it is on show: the passage in its
// source block, or, where only the block's translation or its lines are on show, the parts of
// them that the reader has marked as pointed at.
export function showAnchor(anchor: Anchor): void {
  const passage = highlightAnchor(anchor);
  if (blockElementOf(anchor).checkVisibility()) {
    bringIntoView(passage);
    return;
  }
  const shown = pointedTranslationOf(anchor);
  if (shown !== null) {
    const place = document.createRange();
    place.selectNodeContents(shown);
    bringIntoView(place);
  }
}

function pointedTranslationOf(anchor: Anchor): Element | null {
  return document.querySelector(
    `[${DOCUMENT_ATTRIBUTE}="${anchor.document_id}"] :is([${TRANSLATION_ATTRIBUTE}="${anchor.block_number}"], [${LINES_ATTRIBUTE}="${anchor.block_number}"]) .${POINTED_CLASS}`,
  );
}

// Where a passage is on show: its source block, where that is on show, or else the parts of
// the block's translation or lines that are marked as pointed at.
function passageOnShow(anchor: Anchor): Element | null {
  const blockElement = document.querySelector(
    `[${DOCUMENT_ATTRIBUTE}="${anchor.document_id}"] [${BLOCK_ATTRIBUTE}="${anchor.block_number}"]`,
  );
  if (blockElement === null) {
    return null;
  }
  return blockElement.checkVisibility() ? blockElement : pointedTranslationOf(anchor);
}

// Where a passage pointed at should stand in the window, and the scroller and its scroll
// position when that was last settled. The browser's own anchoring of the scroll is turned off
// in that scroller for as long as the passage is kept, so that every scroll there is the
// reader's or this keeping's own.
type Kept = {
  anchor: Anchor;
  setting: Setting;
  top: number;
  height: number;
  scroller: Element;
  position: number;
};

const OVERFLOW_ANCHOR = "overflow-anchor";
const NO_SCROLL_ANCHORING = "none";

function scrollerAround(element: Element): Element {
  return scrollerOf(element) ?? document.scrollingElement ?? document.documentElement;
}

function viewOfScroller(scroller: Element): DOMRect {
  return scroller === document.scrollingElement
    ? new DOMRect(0, 0, innerWidth, innerHeight)
    : scroller.getBoundingClientRect();
}

// Keeps a passage that is pointed at where it stands on show while what lies around it
// changes: a translation that arrives in the passage's block or in blocks above it, or a
// picture above it that loads, moves it, and the view is moved back by as much. Only a passage
// in view, in a view that is on show, is kept; once the reader scrolls it out of view it is
// left, and the browser anchors the scroll as it would. The result is to be called once the
// passage has been brought into view.
export function usePassageKept(
  anchor: Anchor | undefined,
  onShow: boolean,
  setting: Setting,
): () => void {
  const kept = useRef<Kept>(undefined);

  const release = useCallback(() => {
    const scroller = kept.current?.scroller;
    if (scroller instanceof HTMLElement) {
      scroller.style.removeProperty(OVERFLOW_ANCHOR);
    }
    kept.current = undefined;
  }, []);

  const remember = useCallback(() => {
    const element = anchor !== undefined && onShow ? passageOnShow(anchor) : null;
    if (anchor === undefined || element === null || !inView(element)) {
      release();
      return;
    }
    const scroller = scrollerAround(element);
    if (kept.current !== undefined && kept.current.scroller !== scroller) {
      release();
    }
    if (scroller instanceof HTMLElement) {
      scroller.style.setProperty(OVERFLOW_ANCHOR, NO_SCROLL_ANCHORING);
    }
    const place = element.getBoundingClientRect();
    kept.current = {
      anchor,
      setting,
      top: place.top,
      height: place.height,
      scroller,
      position: scroller.scrollTop,
    };
  }, [anchor, onShow, setting, release]);

  // Puts the passage back where it should stand, after what lies above it has changed.
  const keep = useCallback(() => {
    const before = kept.current;
    const element = anchor !== undefined && onShow ? passageOnShow(anchor) : null;
    const same = before !== undefined && before.anchor === anchor && before.setting === setting;
    if (same && element !== null) {
      const shift = element.getBoundingClientRect().top - before.top;
      if (Math.abs(shift) >= 1) {
        before.scroller.scrollBy({ top: shift });
      }
    }
    remember();
  }, [anchor, onShow, setting, remember]);

  // A scroll moves where the passage stands by as much as it scrolls, without measuring the
  // passage, which may have moved for other reasons that are not settled yet.
  const scrolled = useCallback(() => {
    const before = kept.current;
    if (before === undefined) {
      return;
    }
    const position = before.scroller.scrollTop;
    const top = before.top - (position - before.position);
    const view = viewOfScroller(before.scroller);
    if (top + before.height <= view.top || top >= view.bottom) {
      release();
    } else {
      kept.current = { ...before, top, position };
    }
  }, [release]);

  // After every drawing of the view, and whenever the document's height changes without one.
  useLayoutEffect(keep);
  useEffect(() => {
    const article =
      anchor === undefined
        ? null
        : document.querySelector(`[${DOCUMENT_ATTRIBUTE}="${anchor.document_id}"]`);
    if (article === null) {
      return;
    }
    const resized = new ResizeObserver(keep);
    resized.observe(article);
    addEventListener("scroll", scrolled, { capture: true, passive: true });
    return () => {
      resized.disconnect();
      removeEventListener("scroll", scrolled, { capture: true });
    };
  }, [anchor, keep, scrolled]);
  useEffect(() => release, [release]);

  return remember;
}

export function hideAnchor(): void {
  CSS.highlights.delete(ANCHOR_HIGHLIGHT);
}
