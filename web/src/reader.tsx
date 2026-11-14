import { createElement, Fragment, type CSSProperties, type ReactNode } from "react";
import type { Block, ItemReading, Mark, Style } from "./client";

const STYLE_ELEMENTS: Record<Style, string> = {
  strong: "strong",
  emphasis: "em",
  underline: "u",
  code: "code",
  subscript: "sub",
  superscript: "sup",
};
const BULLET = "•";

type Segment = { text: string; marks: Mark[] };

// Cuts the text wherever a mark starts or ends, so each segment lies wholly inside or outside
// every mark and the segments together are the block's text, character for character.
function segmentsOf(text: string, marks: Mark[]): Segment[] {
  const cuts = [
    ...new Set([0, text.length, ...marks.flatMap((mark) => [mark.start, mark.end])]),
  ].sort((first, second) => first - second);
  return cuts.slice(0, -1).map((start, index) => {
    const end = cuts[index + 1] ?? text.length;
    return {
      text: text.slice(start, end),
      marks: marks.filter((mark) => mark.start <= start && end <= mark.end),
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

// A list item's marker counts the ordered items before it at its depth; any block that is
// not a list item ends the lists.
function listMarkers(blocks: Block[]): string[] {
  let counts: number[] = [];
  return blocks.map((block) => {
    if (block.kind !== "list_item") {
      counts = [];
      return "";
    }
    counts = counts.slice(0, block.depth + 1);
    counts[block.depth] = (counts[block.depth] ?? 0) + 1;
    return block.ordered ? `${counts[block.depth]}.` : BULLET;
  });
}

function TableView({ block, number }: { block: Block & { kind: "table" }; number: number }) {
  const rows = block.text.split(block.row_separator).map((row) => row.split(block.cell_separator));
  return (
    <div className="table-scroll">
      <table data-block={number}>
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

function BlockView({ block, number, marker }: { block: Block; number: number; marker: string }) {
  switch (block.kind) {
    case "heading":
      return createElement(
        `h${block.level}`,
        { "data-block": number },
        <MarkedText text={block.text} marks={block.marks} />,
      );
    case "paragraph":
      return (
        <p data-block={number}>
          <MarkedText text={block.text} marks={block.marks} />
        </p>
      );
    case "list_item":
      return (
        <div
          className="list-item"
          data-block={number}
          data-marker={marker}
          style={{ "--depth": block.depth } as CSSProperties}
        >
          <MarkedText text={block.text} marks={block.marks} />
        </div>
      );
    case "quote":
      return (
        <blockquote data-block={number}>
          <MarkedText text={block.text} marks={block.marks} />
        </blockquote>
      );
    case "code":
      return <pre data-block={number}>{block.text}</pre>;
    case "table":
      return <TableView block={block} number={number} />;
    case "figure":
      return (
        <figure>
          <img src={block.source} alt="" loading="lazy" referrerPolicy="no-referrer" />
          {block.text !== "" && <figcaption data-block={number}>{block.text}</figcaption>}
        </figure>
      );
  }
}

export function Reader({ reading }: { reading: ItemReading }) {
  const blocks = reading.document.blocks;
  const markers = listMarkers(blocks);
  return (
    <article className="reader">
      <header>
        <a href={reading.entry.url} target="_blank" rel="noreferrer noopener">
          {new URL(reading.entry.url).hostname} ↗
        </a>
        {blocks[0]?.kind !== "heading" && <h1>{reading.entry.title}</h1>}
      </header>
      {blocks.map((block, number) => (
        <BlockView key={number} block={block} number={number} marker={markers[number] ?? ""} />
      ))}
    </article>
  );
}
