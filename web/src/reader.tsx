import type { Anchor, ItemDocument, ItemEntry, TimePlace } from "./client";
import { BLOCK_ATTRIBUTE, SourceBlock, unitOffset, unitOffsets } from "./blocks";
import { originLabel } from "./entry";
import type { Clock } from "./player";
import { scrolls } from "./scrolling";
import type { Setting } from "./setting";
import { Transcript, LINES_ATTRIBUTE } from "./transcript";
import { POINTED_CLASS, Row, TRANSLATION_ATTRIBUTE } from "./translated";
import type { TranslationState } from "./translation";

const DOCUMENT_ATTRIBUTE = "data-document";
const ANCHOR_HIGHLIGHT = "anchor";

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
  return (
    <article className="reader" {...{ [DOCUMENT_ATTRIBUTE]: itemDocument.id }}>
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
      const margin = Math.max(0, (ancestor.clientHeight - place.height) / 2);
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
    throw new Error("这段引文所在的原文不在当前页面上");
  }
  return blockElement;
}

// Highlights an anchor's passage, and refuses when the page does not hold the document the
// anchor points into or when the text there is no longer the quoted text. The passage is
// highlighted in the source block whether or not that block is on show.
export function highlightAnchor(anchor: Anchor): Range {
  const blockElement = blockElementOf(anchor);
  const offsets = unitOffsets(blockElement.textContent ?? "");
  const passage = rangeIn(
    blockElement,
    unitOffset(offsets, anchor.start),
    unitOffset(offsets, anchor.end),
  );
  if (passage.toString() !== anchor.quote) {
    throw new Error("原文已经变化，找不到这段引文");
  }
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
  const shown = document.querySelector(
    `[${DOCUMENT_ATTRIBUTE}="${anchor.document_id}"] :is([${TRANSLATION_ATTRIBUTE}="${anchor.block_number}"], [${LINES_ATTRIBUTE}="${anchor.block_number}"]) .${POINTED_CLASS}`,
  );
  if (shown !== null) {
    const place = document.createRange();
    place.selectNodeContents(shown);
    bringIntoView(place);
  }
}

export function hideAnchor(): void {
  CSS.highlights.delete(ANCHOR_HIGHLIGHT);
}
