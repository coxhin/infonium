import { createElement, Fragment, type CSSProperties, type ReactNode } from "react";
import { cellTexts, cutAtMarks, STYLE_ELEMENTS } from "./blocktext";
import { pictureLink, type Block, type Mark } from "./client";
import { Formula } from "./formula";

const BULLET = "•";
export const BLOCK_ATTRIBUTE = "data-block";

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
      // A formula is set from its text, below, before any other mark wraps it.
      return content;
  }
}

// A text with its marks drawn: links, styles, and formulas set as formulas. A text that one
// formula covers whole is set as a formula of its own, apart from the lines around it.
export function MarkedText({ text, marks }: { text: string; marks: Mark[] }) {
  const display =
    marks.length === 1 &&
    marks[0]?.kind === "formula" &&
    marks[0].start === 0 &&
    marks[0].end === [...text].length;
  return cutAtMarks(text, marks).map((piece, index) => {
    const formula = piece.marks.some((mark) => mark.kind === "formula");
    const drawn: ReactNode = formula ? (
      <Formula latex={piece.text} display={display} />
    ) : (
      piece.text
    );
    return <Fragment key={index}>{piece.marks.reduce(marked, drawn)}</Fragment>;
  });
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
  const rows = cellTexts(block, text);
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

// A figure's picture: the one the library keeps of it, where it keeps one, drawn at its size
// so that the text does not move when it arrives; or else the picture at its address on the
// web, asked for without a referrer. A figure with neither has no picture to draw, and one
// that has no words either is not drawn at all.
function FigureForm({
  block,
  documentId,
  text,
  content,
  marking,
  hidden,
}: {
  block: Block & { kind: "figure" };
  documentId: string;
  text: string;
  content: ReactNode;
  marking: Marking;
  hidden: boolean;
}) {
  const picture = block.picture;
  const drawn =
    picture !== null ? (
      <img
        src={pictureLink(documentId, picture.name)}
        width={picture.width}
        height={picture.height}
        alt=""
        loading="lazy"
      />
    ) : block.source !== "" ? (
      <img src={block.source} alt="" loading="lazy" referrerPolicy="no-referrer" />
    ) : null;
  if (drawn === null && text === "") {
    return null;
  }
  return (
    <figure hidden={hidden}>
      {drawn}
      {text !== "" && <figcaption {...marking}>{content}</figcaption>}
    </figure>
  );
}

// A block's form around words: a heading at its level, a list item with its marker and depth,
// a quotation, a table, a figure with its picture. The words are the block's own or their
// translation, so a source and its translation share one form. The text is what a table is
// split from by the block's separators and what tells a figure whether it has words; the
// content is how those words are drawn. The document is the one the block is of, whose
// pictures a figure is drawn with. A form that is hidden keeps its words in the page.
export function BlockForm({
  block,
  documentId,
  text,
  content,
  marking,
  hidden = false,
}: {
  block: Block;
  documentId: string;
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
        <FigureForm
          block={block}
          documentId={documentId}
          text={text}
          content={content}
          marking={marking}
          hidden={hidden}
        />
      );
  }
}

// A block of a document in the words it was written in.
export function SourceBlock({
  block,
  documentId,
  number,
  hidden,
}: {
  block: Block;
  documentId: string;
  number: number;
  hidden: boolean;
}) {
  const marks = "marks" in block ? block.marks : [];
  return (
    <BlockForm
      block={block}
      documentId={documentId}
      text={block.text}
      content={<MarkedText text={block.text} marks={marks} />}
      marking={numbered(number)}
      hidden={hidden}
    />
  );
}
