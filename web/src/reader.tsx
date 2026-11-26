import { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from "react";
import type { Anchor, Anchors, ItemDocument, ItemEntry, TimePlace } from "./client";
import { BLOCK_ATTRIBUTE, SourceBlock } from "./blocks";
import { unitOffset, unitOffsets } from "./blocktext";
import { originLabel } from "./entry";
import type { Marking } from "./marking";
import type { Clock } from "./player";
import { endGlow, glowOver } from "./glowing";
import { inView, scrollerOf, scrolls } from "./scrolling";
import { translatedStretch } from "./sentences";
import type { Setting } from "./setting";
import { useTextMarks } from "./textmarks";
import { useSelectionHandedOver, type TextOnShow } from "./textselection";
import { rangeIn, rangesThrough, textOf } from "./textpieces";
import { Transcript, LINES_ATTRIBUTE } from "./transcript";
import {
  POINTED_CLASS,
  Row,
  SEGMENT_CLASS,
  SEGMENT_GAP,
  TRANSLATION_ATTRIBUTE,
} from "./translated";
import type { TranslationState } from "./translation";
import { useWheelZoom, useZoom } from "./zoom";

const DOCUMENT_ATTRIBUTE = "data-document";
const NONE_LEFT_OUT: ReadonlySet<number> = new Set();
const NONE_POINTED: ReadonlyMap<number, Anchor> = new Map();
const NOT_ON_SHOW = "这段引文所在的原文不在当前页面上";
const CHANGED = "原文已经变化，找不到这段引文";

// What the text view of a recording needs to show its transcript as lines that follow the
// playing: the clock of the player, the stretches last played from, and a way to play.
export type Playing = {
  clock: Clock;
  played: TimePlace[];
  onPlay: (place: TimePlace) => void;
};

// The text of a document, in the words the setting asks for, with the reader's highlights
// drawn over it. A recording's transcript is shown as lines once a translation is on show; in
// its source alone it is the text it always was.
export function Reader({
  entry,
  title,
  itemDocument,
  setting,
  translation,
  pointed,
  marking,
  playing,
  onShow,
}: {
  entry: ItemEntry;
  title: string;
  itemDocument: ItemDocument;
  setting: Setting;
  translation: TranslationState;
  pointed: Anchors | undefined;
  marking: Marking;
  playing: Playing | undefined;
  onShow: boolean;
}) {
  const blocks = itemDocument.blocks;
  const asLines = playing !== undefined && setting !== "source";
  // The anchors of the passage pointed at in this document, by block, so that each row is
  // given its own and the others are not drawn again.
  const pointedHere = useMemo(
    () =>
      pointed === undefined
        ? NONE_POINTED
        : new Map(
            pointed
              .filter((anchor) => anchor.document_id === itemDocument.id)
              .map((anchor) => [anchor.block_number, anchor] as const),
          ),
    [pointed, itemDocument.id],
  );
  // The blocks a translation that stopped has left in their source: those the service says it
  // translates that have no translation.
  const leftOut = useMemo(() => {
    const loaded = translation.loaded;
    if (loaded?.state !== "ready" || loaded.value.progress.kind !== "stopped") {
      return NONE_LEFT_OUT;
    }
    return new Set(
      loaded.value.blocks_to_translate.filter((number) => !translation.blocks.has(number)),
    );
  }, [translation.loaded, translation.blocks]);
  const translations = setting === "source" ? undefined : translation.blocks;
  // What is on show is made anew, and the highlights drawn again, when a translation that stops
  // leaves blocks in their source as well: such a block is drawn anew, in a frame of its own,
  // and the words the highlights were drawn over leave the page.
  const show = useMemo<TextOnShow>(
    () => ({
      documentId: itemDocument.id,
      blocks,
      translations: translations ?? new Map(),
      setting,
      asLines,
    }),
    [itemDocument.id, blocks, translations, setting, asLines, leftOut],
  );
  const article = useRef<HTMLElement>(null);
  useSelectionHandedOver(article, show, marking, onShow);
  const pressed = useTextMarks(article, show, marking);
  // The text is zoomed as a whole, by the control in the reader's head, or by a pinch or the
  // wheel with Ctrl held over it; the browser keeps the words at the top where they were.
  const zoom = useZoom("text");
  useWheelZoom(article, "text", undefined);
  // The passage glowing in this document goes when the document leaves the page. React runs
  // this before the effects of a reader that takes its place, so the passage that reader is
  // opened for glows.
  useEffect(() => endGlow, []);
  return (
    <article
      ref={article}
      className="reader"
      style={{ zoom }}
      onClickCapture={pressed}
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
            <SourceBlock
              key={number}
              block={block}
              documentId={itemDocument.id}
              number={number}
              hidden
            />
          ))}
          <Transcript
            documentId={itemDocument.id}
            blocks={blocks}
            translations={translation.blocks}
            setting={setting}
            again={translation.again}
            pointed={pointedHere}
            played={playing.played}
            leftOut={leftOut}
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
            documentId={itemDocument.id}
            number={number}
            setting={setting}
            translated={translations?.get(number)}
            again={translation.again.get(number)}
            pointed={pointedHere.get(number)}
            onAgain={translation.translateAgain}
            missing={leftOut.has(number)}
          />
        ))
      )}
    </article>
  );
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

// The range of the page that shows an anchor's passage in its source block, and a refusal when
// the page does not hold the document the anchor points into or when the text there is no
// longer the quoted text.
function rangeOfAnchor(anchor: Anchor): Range {
  const blockElement = blockElementOf(anchor);
  const text = textOf(blockElement);
  const offsets = unitOffsets(text);
  const start = unitOffset(offsets, anchor.start);
  const end = unitOffset(offsets, anchor.end);
  // The range may hold a formula's drawing as well, so the quote is checked against the text.
  if (text.slice(start, end) !== anchor.quote) {
    throw new Error(CHANGED);
  }
  return rangeIn(blockElement, start, end);
}

// The ranges of the page that show the translation of an anchor's passage, where the block's
// translation is on show: the sentences of the translation that stand for the sentences the
// passage runs through in the source, since a translation is given whole for its block.
function translatedRanges(anchor: Anchor, source: string): Range[] {
  const translation = document.querySelector(
    `[${DOCUMENT_ATTRIBUTE}="${anchor.document_id}"] [${TRANSLATION_ATTRIBUTE}="${anchor.block_number}"]`,
  );
  if (translation === null || !translation.checkVisibility()) {
    return [];
  }
  // A table's cells hold no element of their own around each segment: the table holds the
  // translation's text whole, its cells' separators among it.
  const segments = [...translation.querySelectorAll(`.${SEGMENT_CLASS}`)];
  const table = translation.querySelector("table");
  const parts = segments.length > 0 ? segments : table === null ? [] : [table];
  const text = parts.map(textOf).join(SEGMENT_GAP);
  return rangesThrough(parts, SEGMENT_GAP, translatedStretch(source, text, anchor));
}

// Makes a passage glow where it is on show, and brings into view where its start is: in its
// source blocks where they are on show, and where only a block's translation is, in the
// sentences of the translation that stand for the passage. Where a recording's lines are on
// show, the lines marked as pointed at are brought into view. Every anchor is checked against
// the source first, so that a passage whose quoted text is no longer there is refused.
export function showPassage(anchors: Anchors): void {
  const sources = anchors.map((anchor) => ({ anchor, range: rangeOfAnchor(anchor) }));
  const shown = sources.flatMap(({ anchor, range }) => {
    const blockElement = blockElementOf(anchor);
    return blockElement.checkVisibility()
      ? [range]
      : translatedRanges(anchor, textOf(blockElement));
  });
  glowOver(shown);
  const first = shown[0];
  if (first !== undefined) {
    bringIntoView(first);
    return;
  }
  const lines = pointedTranslationOf(anchors[0]);
  if (lines !== null) {
    const place = document.createRange();
    place.selectNodeContents(lines);
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
