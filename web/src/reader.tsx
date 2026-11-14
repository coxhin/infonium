import { createElement, Fragment, type CSSProperties, type ReactNode } from "react";
import type { Anchor, Block, ItemReading, Mark, Style } from "./client";

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

export function Reader({ reading }: { reading: ItemReading }) {
  const blocks = reading.document.blocks;
  return (
    <article className="reader" {...{ [DOCUMENT_ATTRIBUTE]: reading.document.id }}>
      <header>
        <a href={reading.entry.url} target="_blank" rel="noreferrer noopener">
          {new URL(reading.entry.url).hostname} ↗
        </a>
        {blocks[0]?.kind !== "heading" && <h1>{reading.entry.title}</h1>}
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

function scrollerOf(node: Node): Element {
  for (let ancestor = node.parentElement; ancestor !== null; ancestor = ancestor.parentElement) {
    const overflow = getComputedStyle(ancestor).overflowY;
    if (overflow === "auto" || overflow === "scroll") {
      return ancestor;
    }
  }
  return document.scrollingElement ?? document.documentElement;
}

// Puts the passage in the middle of whatever scrolls it, or at the top when it is taller than
// the view. The jump is instant: an animated scroll aims at where the passage was when it
// started, and misses once images on the way load and push the passage down.
function bringIntoView(passage: Range): void {
  const scroller = scrollerOf(passage.startContainer);
  const view =
    scroller === document.scrollingElement
      ? new DOMRect(0, 0, innerWidth, innerHeight)
      : scroller.getBoundingClientRect();
  const place = passage.getBoundingClientRect();
  scroller.scrollBy({
    top: place.top - view.top - Math.max(0, (view.height - place.height) / 2),
  });
}

// Highlights an anchor's passage, and refuses when the page does not show the document the
// anchor points into or when the text there is no longer the quoted text.
export function showAnchor(anchor: Anchor): void {
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
  bringIntoView(passage);
}

export function hideAnchor(): void {
  CSS.highlights.delete(ANCHOR_HIGHLIGHT);
}
