import type { Block, Mark, PagePlace, Segment, TranslatedBlock } from "./client";

// A rectangle on a page, as fractions of the page's width and height from its top left corner.
export type Box = { left: number; top: number; width: number; height: number };

// Where a part of a block's translation is set on a page: the lines of the source it stands
// for, which follow one another in one column, the rectangle around them, and how the source's
// text runs there. The size is the height of a horizontal line, or the width of a vertical
// one, as a fraction of the page's height or width; the pitch is the distance from one line to
// the next in the same measure, where the area has more than one line.
export type Area = {
  page: number;
  box: Box;
  lines: Box[];
  vertical: boolean;
  size: number;
  pitch: number | undefined;
  // The share of the block's source text that the area's lines hold.
  characters: number;
};

// A part of a translation's text, with its marks counted in that part.
export type Piece = { text: string; marks: Mark[] };

// A line much taller than the block's others, as a first letter set over several lines, does
// not tell the size of the text.
const TALL_LINE = 1.8;
// A line taller than this many times its width runs downwards.
const DOWNWARD = 2;
// Lines whose boxes overlap by no more than this, as a part of the smaller box, are side by side.
const SLIVER = 0.1;
// A line belongs to the line before it when it begins no higher than this part of that line's
// height above it.
const RISE = 0.5;

function median(values: number[]): number | undefined {
  const sorted = [...values].sort((first, second) => first - second);
  return sorted[Math.floor(sorted.length / 2)];
}

function union(boxes: Box[]): Box {
  const left = Math.min(...boxes.map((box) => box.left));
  const top = Math.min(...boxes.map((box) => box.top));
  const right = Math.max(...boxes.map((box) => box.left + box.width));
  const bottom = Math.max(...boxes.map((box) => box.top + box.height));
  return { left, top, width: right - left, height: bottom - top };
}

function overlap(first: Box, second: Box): number {
  const width =
    Math.min(first.left + first.width, second.left + second.width) -
    Math.max(first.left, second.left);
  const height =
    Math.min(first.top + first.height, second.top + second.height) -
    Math.max(first.top, second.top);
  return width > 0 && height > 0 ? width * height : 0;
}

// Whether a box covers any part of the boxes of other blocks beyond a sliver of them.
function covers(box: Box, others: Box[]): boolean {
  return others.some((other) => overlap(box, other) > SLIVER * other.width * other.height);
}

function isDownward(places: PagePlace[]): boolean {
  const downward = places.filter((place) => place.height > DOWNWARD * place.width);
  return places.length > 0 && downward.length * 2 > places.length;
}

// Whether the next line goes on from the last in the same column: below it with the columns
// overlapping, or beside it on the same line; for lines written downwards, to its left.
function goesOn(last: PagePlace, next: PagePlace, downward: boolean): boolean {
  if (last.page !== next.page) {
    return false;
  }
  if (downward) {
    const sameRow = next.top < last.top + last.height && last.top < next.top + next.height;
    return sameRow && next.left + next.width <= last.left + last.width + RISE * last.width;
  }
  const sideBySide =
    Math.abs(next.top - last.top) < RISE * last.height && next.left >= last.left;
  const sameColumn =
    next.left < last.left + last.width &&
    last.left < next.left + next.width &&
    next.top > last.top - RISE * last.height;
  return sideBySide || sameColumn;
}

function areaOf(lines: PagePlace[], downward: boolean): Area {
  const boxes = lines.map(({ left, top, width, height }) => ({ left, top, width, height }));
  const extent = (box: Box) => (downward ? box.width : box.height);
  const ordinary = median(boxes.map(extent)) ?? 0;
  const sizes = boxes.map(extent).filter((size) => size <= TALL_LINE * ordinary);
  const starts = boxes.map((box) => (downward ? -(box.left + box.width) : box.top));
  const steps = starts.slice(1).map((start, index) => start - (starts[index] ?? start));
  const pitch = median(steps.filter((step) => step > 0.5 * ordinary));
  return {
    page: lines[0]?.page ?? 0,
    box: union(boxes),
    lines: boxes,
    vertical: downward,
    size: median(sizes) ?? ordinary,
    pitch,
    characters: lines.reduce((sum, line) => sum + (line.end - line.start), 0),
  };
}

// The areas of one block: its lines in the order of its text, a new area wherever the next
// line does not go on from the last in the same column, or would make the area cover lines of
// other blocks. The boxes of the other blocks are those on each page.
export function areasOf(places: PagePlace[], othersOn: (page: number) => Box[]): Area[] {
  const lines = [...places].sort((first, second) => first.start - second.start);
  const downward = isDownward(lines);
  const runs: PagePlace[][] = [];
  for (const line of lines) {
    const run = runs.at(-1);
    const last = run?.at(-1);
    if (
      run !== undefined &&
      last !== undefined &&
      goesOn(last, line, downward) &&
      !covers(union([...run, line]), othersOn(line.page))
    ) {
      run.push(line);
    } else {
      runs.push([line]);
    }
  }
  // The size and the spacing of a block's text are those of all its lines, so that an area of
  // one line, whose box may be a little taller or shorter than most, is set like the rest.
  const areas = runs.map((run) => areaOf(run, downward));
  const ordinary = median(areas.flatMap((area) => area.lines.map((line) => (downward ? line.width : line.height)))) ?? 0;
  const sizes = areas
    .flatMap((area) => area.lines.map((line) => (downward ? line.width : line.height)))
    .filter((size) => size <= TALL_LINE * ordinary);
  const size = median(sizes) ?? ordinary;
  const pitch = median(areas.flatMap((area) => (area.pitch === undefined ? [] : [area.pitch])));
  return areas.map((area) => ({ ...area, size, pitch }));
}

// Marks that open or close a phrase, before and after which a line is not broken in Chinese
// and Japanese.
const NO_BREAK_BEFORE = new Set([..."，。、；：！？）」』》〉】〕”’…—·.,;:!?)]}%"]);
const NO_BREAK_AFTER = new Set([..."（「『《〈【〔“‘([{"]);
const SPACE = /\s/u;
// Characters of scripts written without spaces between words.
const UNSPACED = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;

// Whether a line may be broken before the character at this place of the text: after a space
// in a spaced script, or between two characters of an unspaced one where no mark forbids it.
function mayBreakBefore(characters: string[], at: number): boolean {
  const before = characters[at - 1];
  const after = characters[at];
  if (before === undefined || after === undefined) {
    return false;
  }
  if (SPACE.test(before)) {
    return true;
  }
  const unspaced = UNSPACED.test(before) || UNSPACED.test(after);
  return (
    unspaced && !SPACE.test(after) && !NO_BREAK_BEFORE.has(after) && !NO_BREAK_AFTER.has(before)
  );
}

// The places a formula mark covers may not be broken.
function insideFormula(marks: Mark[], at: number): boolean {
  return marks.some((mark) => mark.kind === "formula" && mark.start < at && at < mark.end);
}

function piece(characters: string[], marks: Mark[], start: number, end: number): Piece {
  return {
    text: characters.slice(start, end).join(""),
    marks: marks
      .filter((mark) => mark.start < end && start < mark.end)
      .map((mark) => ({
        ...mark,
        start: Math.max(mark.start, start) - start,
        end: Math.min(mark.end, end) - start,
      })),
  };
}

// Parts a text, with its marks, by the shares given, each cut at the place nearest its share
// where a reader would break a line. The parts together are the text.
export function partedByShares(text: string, marks: Mark[], shares: number[]): Piece[] {
  const characters = [...text];
  const total = shares.reduce((sum, share) => sum + share, 0);
  const cuts: number[] = [];
  let reached = 0;
  for (const share of shares.slice(0, -1)) {
    reached += total > 0 ? (share / total) * characters.length : 0;
    const wanted = Math.round(reached);
    const floor = cuts.at(-1) ?? 0;
    let cut = wanted;
    for (let distance = 0; distance <= characters.length; distance += 1) {
      const candidates = [wanted - distance, wanted + distance].filter(
        (at) => at > floor && at < characters.length,
      );
      const found = candidates.find(
        (at) => mayBreakBefore(characters, at) && !insideFormula(marks, at),
      );
      if (found !== undefined) {
        cut = found;
        break;
      }
    }
    cuts.push(Math.max(floor, Math.min(cut, characters.length)));
  }
  const bounds = [0, ...cuts, characters.length];
  return bounds.slice(0, -1).map((start, index) => {
    const end = bounds[index + 1] ?? characters.length;
    const part = piece(characters, marks, start, end);
    return { text: part.text.trim() === "" ? "" : part.text, marks: part.marks };
  });
}

// A part of a translation set in one area of a page. The first area of a list item begins with
// the item's marker, which the area covers on the page.
export type AreaText = {
  key: string;
  blockNumber: number;
  area: Area;
  piece: Piece;
  marker: string | undefined;
};

const BULLET = "•";

function markerOf(block: Block): string | undefined {
  if (block.kind !== "list_item" || block.continues) {
    return undefined;
  }
  return block.ordered ? `${block.position}.` : BULLET;
}

// Blocks whose translation is not set on the pages: a table, whose cells have no places of
// their own, and a listing, which is never translated.
const KEPT_AS_PAGE = new Set<Block["kind"]>(["table", "code"]);

// The boxes of every block on each page, by page, so that each block's areas keep off the
// others' lines.
export function boxesByPage(blocks: Block[]): Map<number, { blockNumber: number; box: Box }[]> {
  const byPage = new Map<number, { blockNumber: number; box: Box }[]>();
  blocks.forEach((block, blockNumber) => {
    for (const place of block.places) {
      if (place.kind === "page") {
        const { left, top, width, height } = place;
        const onPage = byPage.get(place.page) ?? [];
        onPage.push({ blockNumber, box: { left, top, width, height } });
        byPage.set(place.page, onPage);
      }
    }
  });
  return byPage;
}

// Where a block's translation is set on the pages, and what part of it each area holds,
// parted by the share of the source's text each area's lines hold.
export function placedOf(
  block: Block,
  blockNumber: number,
  translated: TranslatedBlock,
  byPage: Map<number, { blockNumber: number; box: Box }[]>,
  gap: string,
): AreaText[] {
  const places = block.places.filter((place) => place.kind === "page");
  if (KEPT_AS_PAGE.has(block.kind) || places.length === 0) {
    return [];
  }
  const othersOn = (page: number) =>
    (byPage.get(page) ?? [])
      .filter((other) => other.blockNumber !== blockNumber)
      .map((other) => other.box);
  const areas = areasOf(places, othersOn);
  const whole = translatedText(translated, gap);
  const pieces = partedByShares(
    whole.text,
    whole.marks,
    areas.map((area) => area.characters),
  );
  return areas.map((area, index) => ({
    key: `${blockNumber}:${index}`,
    blockNumber,
    area,
    piece: pieces[index] ?? { text: "", marks: [] },
    marker: index === 0 ? markerOf(block) : undefined,
  }));
}

// The text of a block's translation, its segments joined as the text view joins them.
export function translatedText(translated: TranslatedBlock, gap: string): Piece {
  let text = "";
  const marks: Mark[] = [];
  translated.segments.forEach((segment: Segment, index) => {
    if (index > 0) {
      text += gap;
    }
    const offset = [...text].length;
    marks.push(
      ...segment.marks.map((mark) => ({ ...mark, start: mark.start + offset, end: mark.end + offset })),
    );
    text += segment.text;
  });
  return { text, marks };
}
