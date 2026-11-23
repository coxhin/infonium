import { createElement, Fragment, type CSSProperties, type ReactNode } from "react";
import type { Block, Mark, Style } from "./client";

const STYLE_ELEMENTS: Record<Style, string> = {
  strong: "strong",
  emphasis: "em",
  underline: "u",
  code: "code",
  subscript: "sub",
  superscript: "sup",
};
const BULLET = "•";
export const BLOCK_ATTRIBUTE = "data-block";

// The protocol counts offsets in Unicode code points. Strings and DOM ranges count UTF-16
// units, and the two part ways after any character outside the Basic Multilingual Plane.
// The result gives, for each code point offset of the text and for its end, the unit offset.
export function unitOffsets(text: string): number[] {
  const offsets: number[] = [];
  let units = 0;
  for (const character of text) {
    offsets.push(units);
    units += character.length;
  }
  offsets.push(units);
  return offsets;
}

export function unitOffset(offsets: number[], offset: number): number {
  const units = offsets[offset];
  if (units === undefined) {
    throw new Error(`offset ${offset} lies outside the text`);
  }
  return units;
}

type Piece = { text: string; marks: Mark[] };

// Cuts the text wherever a mark starts or ends, so each piece lies wholly inside or outside
// every mark and the pieces together are the text, character for character.
function piecesOf(text: string, marks: Mark[]): Piece[] {
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
  switch (mark.kind) {
    case "link":
      return (
        <a href={mark.target} target="_blank" rel="noreferrer noopener">
          {content}
        </a>
      );
    case "style":
      return createElement(STYLE_ELEMENTS[mark.style], null, content);
    case "formula":
      return <code className="formula">{content}</code>;
  }
}

export function MarkedText({ text, marks }: { text: string; marks: Mark[] }) {
  return piecesOf(text, marks).map((piece, index) => (
    <Fragment key={index}>{piece.marks.reduce(marked, piece.text as ReactNode)}</Fragment>
  ));
}

// What tells the element that holds a block's words apart from every other: the number of the
// block whose text it holds, or of the block whose translation it holds.
export type Marking = { className?: string } & Record<`data-${string}`, number>;

// The element that carries a block's number holds exactly that block's text. Anchors are
// resolved against this, so nothing else may put text inside such an element.
export function numbered(number: number): Marking {
  return { [BLOCK_ATTRIBUTE]: number };
}

function TableForm({
  block,
  text,
  marking,
  hidden,
}: {
  block: Block & { kind: "table" };
  text: string;
  marking: Marking;
  hidden: boolean;
}) {
  const rows = text.split(block.row_separator).map((row) => row.split(block.cell_separator));
  return (
    <div className="table-scroll" hidden={hidden}>
      <table {...marking}>
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

// A block's form around words: a heading at its level, a list item with its marker and depth,
// a quotation, a table, a figure with its picture. The words are the block's own or their
// translation, so a source and its translation share one form. The text is what a table is
// split from by the block's separators and what tells a figure whether it has words; the
// content is how those words are drawn. A form that is hidden keeps its words in the page.
export function BlockForm({
  block,
  text,
  content,
  marking,
  hidden = false,
}: {
  block: Block;
  text: string;
  content: ReactNode;
  marking: Marking;
  hidden?: boolean;
}) {
  switch (block.kind) {
    case "heading":
      return createElement(`h${block.level}`, { ...marking, hidden }, content);
    case "paragraph":
      return (
        <p {...marking} hidden={hidden}>
          {content}
        </p>
      );
    case "list_item":
      return (
        <div
          {...marking}
          className={[marking.className, "list-item"].filter(Boolean).join(" ")}
          hidden={hidden}
          data-marker={block.continues ? "" : block.ordered ? `${block.position}.` : BULLET}
          style={{ "--depth": block.depth } as CSSProperties}
        >
          {content}
        </div>
      );
    case "quotation":
      return (
        <blockquote {...marking} hidden={hidden}>
          {content}
        </blockquote>
      );
    case "code":
      return (
        <pre {...marking} hidden={hidden}>
          {text}
        </pre>
      );
    case "table":
      return <TableForm block={block} text={text} marking={marking} hidden={hidden} />;
    case "figure":
      return (
        <figure hidden={hidden}>
          <img src={block.source} alt="" loading="lazy" referrerPolicy="no-referrer" />
          {text !== "" && <figcaption {...marking}>{content}</figcaption>}
        </figure>
      );
  }
}

// A block in the words it was written in.
export function SourceBlock({
  block,
  number,
  hidden,
}: {
  block: Block;
  number: number;
  hidden: boolean;
}) {
  const marks = "marks" in block ? block.marks : [];
  return (
    <BlockForm
      block={block}
      text={block.text}
      content={<MarkedText text={block.text} marks={marks} />}
      marking={numbered(number)}
      hidden={hidden}
    />
  );
}
