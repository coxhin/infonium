import { createElement, Fragment, type CSSProperties, type ReactNode } from "react";
import type { Anchor, Block, ItemDocument, ItemEntry, Mark, Style } from "./client";
import { originLabel } from "./entry";

const STYLE_ELEMENTS: Record<Style, string> = {
  strong: "strong",
  emphasis: "em",
  underline: "u",
  code: "code",
  subscript: "sub",
  superscript: "sup",
};
const BULLET = "•";
const DOCUMENT_ATTRIBUTE = "data-document";
const BLOCK_ATTRIBUTE = "data-block";
const ANCHOR_HIGHLIGHT = "anchor";

// The protocol counts offsets in Unicode code points. Strings and DOM ranges count UTF-16
// units, and the two part ways after any character outside the Basic Multilingual Plane.
// The result gives, for each code point offset of the text and for its end, the unit offset.
function unitOffsets(text: string): number[] {
  const offsets: number[] = [];
  let units = 0;
  for (const character of text) {
    offsets.push(units);
    units += character.length;
  }
  offsets.push(units);
  return offsets;
}

function unitOffset(offsets: number[], offset: number): number {
  const units = offsets[offset];
  if (units === undefined) {
    throw new Error(`offset ${offset} lies outside the text`);
  }
  return units;
}

type Segment = { text: string; marks: Mark[] };

// Cuts the text wherever a mark starts or ends, so each segment lies wholly inside or outside
// every mark and the segments together are the block's text, character for character.
function segmentsOf(text: string, marks: Mark[]): Segment[] {
  const offsets = unitOffsets(text);
  const spans = marks.map((mark) => ({
    mark,
    start: unitOffset(offsets, mark.start),
    end: unitOffset(offsets, mark.end),
  }));
  const cuts = [
    ...new Set([0, text.length, ...spans.flatMap((span) => [span.start, span.end])]),
  ].sort((first, second) => first - second);
  return cuts.slice(0, -1).map((start, index) => {
    const end = cuts[index + 1] ?? text.length;
    return {
      text: text.slice(start, end),
      marks: spans.filter((span) => span.start <= start && end <= span.end).map((span) => span.mark),
    };
  });
}

function marked(content: ReactNode, mark: Mark): ReactNode {
  return mark.kind === "link" ? (
    <a href={mark.target} target="_blank" rel="noreferrer noopener">
      {content}
    </a>
  ) : (
    createElement(STYLE_ELEMENTS[mark.style], null, content)
  );
}

function MarkedText({ text, marks }: { text: string; marks: Mark[] }) {
  return segmentsOf(text, marks).map((segment, index) => (
    <Fragment key={index}>{segment.marks.reduce(marked, segment.text as ReactNode)}</Fragment>
  ));
}

// The element that carries a block's number holds exactly that block's text. Anchors are
// resolved against this, so nothing else may put text inside such an element.
function numbered(number: number): Record<string, number> {
  return { [BLOCK_ATTRIBUTE]: number };
}

function TableView({ block, number }: { block: Block & { kind: "table" }; number: number }) {
  const rows = block.text.split(block.row_separator).map((row) => row.split(block.cell_separator));
  return (
    <div className="table-scroll">
      <table {...numbered(number)}>
        <tbody>
          {rows.map((cells, rowIndex) => (
            <tr key={rowIndex}>
              {cells.map((cell, cellIndex) => (
                <td key={cellIndex}>
                  {cell}
                  {cellIndex < cells.length - 1 && block.cell_separator}
                  {cellIndex === cells.length - 1 &&
                    rowIndex < rows.length - 1 &&
                    block.row_separator}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BlockView({ block, number }: { block: Block; number: number }) {
  switch (block.kind) {
    case "heading":
      return createElement(
        `h${block.level}`,
        numbered(number),
        <MarkedText text={block.text} marks={block.marks} />,
      );
    case "paragraph":
      return (
        <p {...numbered(number)}>
          <MarkedText text={block.text} marks={block.marks} />
        </p>
      );
    case "list_item":
      return (
        <div
          className="list-item"
          {...numbered(number)}
          data-marker={block.continues ? "" : block.ordered ? `${block.position}.` : BULLET}
          style={{ "--depth": block.depth } as CSSProperties}
        >
          <MarkedText text={block.text} marks={block.marks} />
        </div>
      );
    case "quotation":
      return (
        <blockquote {...numbered(number)}>
          <MarkedText text={block.text} marks={block.marks} />
        </blockquote>
      );
    case "code":
      return <pre {...numbered(number)}>{block.text}</pre>;
    case "table":
      return <TableView block={block} number={number} />;
    case "figure":
      return (
        <figure>
          <img src={block.source} alt="" loading="lazy" referrerPolicy="no-referrer" />
          {block.text !== "" && <figcaption {...numbered(number)}>{block.text}</figcaption>}
        </figure>
      );
  }
}

export function Reader({
  entry,
  title,
  itemDocument,
}: {
  entry: ItemEntry;
  title: string;
  itemDocument: ItemDocument;
}) {
  const blocks = itemDocument.blocks;
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
      {blocks.map((block, number) => (
        <BlockView key={number} block={block} number={number} />
      ))}
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

export function scrolls(overflow: string): boolean {
  return overflow === "auto" || overflow === "scroll";
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

// Highlights an anchor's passage, and refuses when the page does not hold the document the
// anchor points into or when the text there is no longer the quoted text.
export function highlightAnchor(anchor: Anchor): Range {
  const blockElement = document.querySelector(
    `[${DOCUMENT_ATTRIBUTE}="${anchor.document_id}"] [${BLOCK_ATTRIBUTE}="${anchor.block_number}"]`,
  );
  if (blockElement === null) {
    throw new Error("这段引文所在的原文不在当前页面上");
  }
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

// Highlights an anchor's passage and brings it into view, for text that is on show.
export function showAnchor(anchor: Anchor): void {
  bringIntoView(highlightAnchor(anchor));
}

export function hideAnchor(): void {
  CSS.highlights.delete(ANCHOR_HIGHLIGHT);
}
