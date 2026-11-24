import { isDownward, type Box } from "./areas";
import type { Anchor, Anchors, Block, PagePlace } from "./client";
import type { Marked } from "./marking";
import { pagePlaces } from "./places";

// What marking on the pages of a PDF needs of its document: the lines of each page, the part
// of a line's box that holds a stretch of its characters, the word under a point, and the
// anchors of a stretch of the document.

// One place of a block on a page, as a rule a line, with its block's text as code points. A
// line is cut across its run in proportion to the widths of its characters, unless it is taken
// whole: a table's, a picture's, or one place that holds several lines of text.
export type Line = {
  blockNumber: number;
  place: PagePlace;
  characters: string[];
  downward: boolean;
  whole: boolean;
};

// The lines of a document: by page, in the order of the document, and by block; and the text
// of each block as code points, as offsets count them.
export type Lines = {
  onPages: Map<number, Line[]>;
  ofBlocks: Map<number, Line[]>;
  characters: string[][];
};

// Blocks whose one place is not a line of text but the block's whole box.
const BOXED_KINDS = new Set<Block["kind"]>(["table", "figure"]);
// One place stands for several lines where it is this many times as deep, across its run, as
// the lines of its page are.
const SEVERAL_LINES = 2.5;

function median(values: number[]): number | undefined {
  const sorted = [...values].sort((first, second) => first - second);
  return sorted[Math.floor(sorted.length / 2)];
}

function depthOf(place: PagePlace, downward: boolean): number {
  return downward ? place.width : place.height;
}

export function linesOf(blocks: Block[]): Lines {
  const onPages = new Map<number, Line[]>();
  const ofBlocks = new Map<number, Line[]>();
  const characters = blocks.map((block) => [...block.text]);
  blocks.forEach((block, blockNumber) => {
    const places = pagePlaces(block.places);
    const downward = isDownward(places);
    const whole = BOXED_KINDS.has(block.kind);
    const text = characters[blockNumber] ?? [];
    const lines = [...places]
      .sort((first, second) => first.start - second.start)
      .map((place) => ({ blockNumber, place, characters: text, downward, whole }));
    ofBlocks.set(blockNumber, lines);
    for (const line of lines) {
      onPages.set(line.place.page, [...(onPages.get(line.place.page) ?? []), line]);
    }
  });
  // A block that has one place for all its text is a line only where that place is no deeper
  // than the lines of its page: a block read as one box, as a table is, is taken whole.
  onPages.forEach((lines) => {
    const depth = median(lines.map((line) => depthOf(line.place, line.downward))) ?? 0;
    for (const line of lines) {
      const own = ofBlocks.get(line.blockNumber) ?? [];
      if (own.length === 1 && depthOf(line.place, line.downward) > SEVERAL_LINES * depth) {
        line.whole = true;
      }
    }
  });
  return { onPages, ofBlocks, characters };
}

// The width of a character as a serif face of the browser sets it: the print of a page is not
// known, but its letters differ in width much as any face's do, an "i" a third of an "m".
const MEASURING_FONT = "100px serif";
const widths = new Map<string, number>();
let measurer: CanvasRenderingContext2D | null | undefined;

function widthOf(character: string): number {
  let width = widths.get(character);
  if (width === undefined) {
    if (measurer === undefined) {
      measurer = document.createElement("canvas").getContext("2d");
      if (measurer !== null) {
        measurer.font = MEASURING_FONT;
      }
    }
    width = measurer?.measureText(character).width ?? 1;
    widths.set(character, width);
  }
  return width;
}

// Where each character of a line begins along its run, as a part of the run, and where the
// last ends: worked out once a line, when it is first needed.
const edgesKept = new WeakMap<Line, number[]>();

function edgesOf(line: Line): number[] {
  let edges = edgesKept.get(line);
  if (edges === undefined) {
    const { start, end } = line.place;
    const count = end - start;
    const runs = line.characters.slice(start, end).map(widthOf);
    const total = runs.reduce((sum, width) => sum + width, 0);
    edges = [0];
    let reached = 0;
    for (const width of runs) {
      reached += width;
      edges.push(total > 0 ? reached / total : edges.length / count);
    }
    while (edges.length <= count) {
      edges.push(1);
    }
    edgesKept.set(line, edges);
  }
  return edges;
}

// The part of a line's box that holds the characters from start to end of its block's text:
// cut across the run in proportion to the characters' widths, down the line where the text
// runs downwards. A line taken whole is covered whole by any part of it.
export function coveredBox(line: Line, start: number, end: number): Box | undefined {
  const { place } = line;
  const from = Math.max(start, place.start);
  const to = Math.min(end, place.end);
  if (from >= to) {
    return undefined;
  }
  const count = place.end - place.start;
  if (line.whole || count === 0) {
    return { left: place.left, top: place.top, width: place.width, height: place.height };
  }
  const edges = edgesOf(line);
  const head = edges[from - place.start] ?? 0;
  const tail = edges[to - place.start] ?? 1;
  return line.downward
    ? {
        left: place.left,
        top: place.top + head * place.height,
        width: place.width,
        height: (tail - head) * place.height,
      }
    : {
        left: place.left + head * place.width,
        top: place.top,
        width: (tail - head) * place.width,
        height: place.height,
      };
}

// A box on a page, standing for what covers it.
export type PageBox = { page: number; box: Box };

export function coveredBoxes(
  lines: Lines,
  blockNumber: number,
  start: number,
  end: number,
): PageBox[] {
  return (lines.ofBlocks.get(blockNumber) ?? []).flatMap((line) => {
    const box = coveredBox(line, start, end);
    return box === undefined ? [] : [{ page: line.place.page, box }];
  });
}

// How a highlight is drawn: plainly, with a mark that it holds a comment, or apart as the one
// open in the notebook.
export type MarkState = "plain" | "commented" | "opened";

const STATE_WEIGHT: Record<MarkState, number> = { plain: 0, commented: 1, opened: 2 };

export function strongerState(first: MarkState, second: MarkState): MarkState {
  return STATE_WEIGHT[second] > STATE_WEIGHT[first] ? second : first;
}

// A highlight as it is drawn on one page.
export type PageMark = { key: string; id: string; state: MarkState; boxes: Box[] };

function stateOf(marked: Marked, opened: string | undefined): MarkState {
  return marked.id === opened ? "opened" : marked.commented ? "commented" : "plain";
}

// What tells two drawings of a highlight apart: its state and its anchors' ranges.
function keyOf(marked: Marked, state: MarkState): string {
  const ranges = marked.anchors.map(
    (anchor) => `${anchor.block_number}:${anchor.start}-${anchor.end}`,
  );
  return `${marked.id}|${state}|${ranges.join(",")}`;
}

// The highlights on each page, in the order of the document.
export function pageMarksOf(
  lines: Lines,
  marked: Marked[],
  opened: string | undefined,
): Map<number, PageMark[]> {
  const onPages = new Map<number, PageMark[]>();
  for (const each of marked) {
    const state = stateOf(each, opened);
    const key = keyOf(each, state);
    const byPage = new Map<number, Box[]>();
    for (const anchor of each.anchors) {
      for (const { page, box } of coveredBoxes(
        lines,
        anchor.block_number,
        anchor.start,
        anchor.end,
      )) {
        byPage.set(page, [...(byPage.get(page) ?? []), box]);
      }
    }
    byPage.forEach((boxes, page) => {
      onPages.set(page, [...(onPages.get(page) ?? []), { key, id: each.id, state, boxes }]);
    });
  }
  return onPages;
}

// The blocks the highlights run through, each in the strongest state of those that do.
export function markedBlocksOf(
  marked: Marked[],
  opened: string | undefined,
): Map<number, MarkState> {
  const blocks = new Map<number, MarkState>();
  for (const each of marked) {
    const state = stateOf(each, opened);
    for (const anchor of each.anchors) {
      blocks.set(
        anchor.block_number,
        strongerState(blocks.get(anchor.block_number) ?? "plain", state),
      );
    }
  }
  return blocks;
}

// Of the highlights given, the one that begins last in the document.
export function beginningLast(marked: Marked[]): Marked | undefined {
  return marked.reduce<Marked | undefined>((last, each) => {
    if (last === undefined) {
      return each;
    }
    const [one, other] = [each.anchors[0], last.anchors[0]];
    const later =
      one.block_number > other.block_number ||
      (one.block_number === other.block_number && one.start >= other.start);
    return later ? each : last;
  }, undefined);
}

// A point on a page, as fractions of its width and height.
export type Point = { x: number; y: number };

function contains(box: Box, point: Point, slack: number): boolean {
  return (
    point.x >= box.left - slack &&
    point.x <= box.left + box.width + slack &&
    point.y >= box.top - slack &&
    point.y <= box.top + box.height + slack
  );
}

// The highlights whose drawing on a page holds the point.
export function marksAt(marks: PageMark[], point: Point): string[] {
  return marks
    .filter((mark) => mark.boxes.some((box) => contains(box, point, 0)))
    .map((mark) => mark.id);
}

// The line under a point, the smallest where several hold it; a little beside a line is on it,
// as the boxes of lines hug their letters.
export function lineAt(lines: Line[], point: Point, slack: number): Line | undefined {
  const holding = lines.filter((line) => contains(line.place, point, slack));
  return holding.sort(
    (first, second) =>
      first.place.width * first.place.height - second.place.width * second.place.height,
  )[0];
}

// The line nearest a point, distances taken in one measure across and down a page of the
// shape given (its height as a part of its width).
export function nearestLine(lines: Line[], point: Point, shape: number): Line | undefined {
  let nearest: Line | undefined;
  let least = Infinity;
  for (const line of lines) {
    const { left, top, width, height } = line.place;
    const across = Math.max(left - point.x, 0, point.x - (left + width));
    const down = Math.max(top - point.y, 0, point.y - (top + height)) * shape;
    const distance = Math.hypot(across, down);
    if (distance < least) {
      least = distance;
      nearest = line;
    }
  }
  return nearest;
}

// A stretch of one block's text, as code points from start to end.
export type Span = { blockNumber: number; start: number; end: number };

const SPACE = /\s/u;
// Characters of scripts written without spaces between words: each is a word of its own.
const UNSPACED = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
// What a word of a spaced script is made of; any other character stands alone.
const WORDLIKE = /[\p{L}\p{N}\p{M}\p{Pc}'’-]/u;

function isWordlike(character: string | undefined): boolean {
  return character !== undefined && WORDLIKE.test(character) && !UNSPACED.test(character);
}

// The word around a character: settled on a word's edges in a spaced script, the character
// alone in an unspaced one, and alone where it is a mark between words.
export function wordAround(characters: string[], at: number): { start: number; end: number } {
  if (!isWordlike(characters[at])) {
    return { start: at, end: Math.min(at + 1, characters.length) };
  }
  let start = at;
  let end = at + 1;
  while (isWordlike(characters[start - 1])) {
    start -= 1;
  }
  while (isWordlike(characters[end])) {
    end += 1;
  }
  return { start, end };
}

// The character of a line under a point, in proportion to the characters' widths across the
// line's box, and settled on the word around it; a line taken whole stands for all of it.
export function spanAt(line: Line, point: Point): Span {
  const { place } = line;
  const count = place.end - place.start;
  if (line.whole || count === 0) {
    return { blockNumber: line.blockNumber, start: place.start, end: place.end };
  }
  const along = line.downward
    ? (point.y - place.top) / place.height
    : (point.x - place.left) / place.width;
  const edges = edgesOf(line);
  let at = 0;
  while (at < count - 1 && (edges[at + 1] ?? 1) <= along) {
    at += 1;
  }
  return { blockNumber: line.blockNumber, ...wordAround(line.characters, place.start + at) };
}

export function wholeBlock(blockNumber: number, characters: string[]): Span {
  return { blockNumber, start: 0, end: characters.length };
}

// The stretch of the document from the first of two spans to the end of the last.
export type Stretch = {
  first: { blockNumber: number; offset: number };
  last: { blockNumber: number; offset: number };
};

function before(
  one: { blockNumber: number; offset: number },
  other: { blockNumber: number; offset: number },
): boolean {
  return (
    one.blockNumber < other.blockNumber ||
    (one.blockNumber === other.blockNumber && one.offset < other.offset)
  );
}

export function stretchOf(one: Span, other: Span): Stretch {
  const starts = [
    { blockNumber: one.blockNumber, offset: one.start },
    { blockNumber: other.blockNumber, offset: other.start },
  ];
  const ends = [
    { blockNumber: one.blockNumber, offset: one.end },
    { blockNumber: other.blockNumber, offset: other.end },
  ];
  const [firstStart, secondStart] = starts as [(typeof starts)[0], (typeof starts)[0]];
  const [firstEnd, secondEnd] = ends as [(typeof ends)[0], (typeof ends)[0]];
  return {
    first: before(secondStart, firstStart) ? secondStart : firstStart,
    last: before(firstEnd, secondEnd) ? secondEnd : firstEnd,
  };
}

// The spans of each block a stretch takes text of, in the order of the document, without white
// space at either end.
export function spansOf(stretch: Stretch, charactersOf: (blockNumber: number) => string[]): Span[] {
  const spans: Span[] = [];
  for (
    let blockNumber = stretch.first.blockNumber;
    blockNumber <= stretch.last.blockNumber;
    blockNumber += 1
  ) {
    const characters = charactersOf(blockNumber);
    let start = blockNumber === stretch.first.blockNumber ? stretch.first.offset : 0;
    let end = blockNumber === stretch.last.blockNumber ? stretch.last.offset : characters.length;
    while (start < end && SPACE.test(characters[start] ?? "")) {
      start += 1;
    }
    while (end > start && SPACE.test(characters[end - 1] ?? "")) {
      end -= 1;
    }
    if (start < end) {
      spans.push({ blockNumber, start, end });
    }
  }
  return spans;
}

export function anchorsOfSpans(
  spans: Span[],
  documentId: string,
  charactersOf: (blockNumber: number) => string[],
): Anchors | undefined {
  const anchors: Anchor[] = spans.map((span) => ({
    document_id: documentId,
    block_number: span.blockNumber,
    start: span.start,
    end: span.end,
    quote: charactersOf(span.blockNumber).slice(span.start, span.end).join(""),
  }));
  const [first, ...rest] = anchors;
  return first === undefined ? undefined : [first, ...rest];
}
