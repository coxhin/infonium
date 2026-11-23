import type { Block, Mark, Style } from "./client";

// What the reader's own text and a web page written into both need of a block's text: its
// offsets, its pieces between the ends of its marks, the element of each style, and the cells
// of a table.

// The element that draws each style of a mark.
export const STYLE_ELEMENTS: Record<Style, string> = {
  strong: "strong",
  emphasis: "em",
  underline: "u",
  code: "code",
  subscript: "sub",
  superscript: "sup",
};

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
    throw new Error("这段文字的位置超出了原文的长度，原文可能已经变化。");
  }
  return units;
}

export type Piece = { text: string; marks: Mark[] };

// Cuts the text wherever a mark starts or ends, so each piece lies wholly inside or outside
// every mark and the pieces together are the text, character for character.
export function cutAtMarks(text: string, marks: Mark[]): Piece[] {
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

// A table's text, the block's own or its translation, as rows of cells.
export function cellTexts(block: Block & { kind: "table" }, text: string): string[][] {
  return text.split(block.row_separator).map((row) => row.split(block.cell_separator));
}
