import type { Block, Mark, PagePlace, Segment, TranslatedBlock } from "./client";

// A rectangle on a page, as fractions of the page's width and height from its top left corner.
export type Box = { left: number; top: number; width: number; height: number };

// A fraction of a page as a length in its style.
export function percent(fraction: number): string {
  return `${fraction * 100}%`;
}

// Where a part of a block's translation is set on a page: the lines of the source it stands
// for, which follow one another in one column, the rectangle around them, and how the source's
// text runs there.
export type Area = {
  page: number;
  box: Box;
  lines: Box[];
  vertical: boolean;
  typeset: Typeset;
  // The line spacing of the source, as a part of the size of its text, and the room the lines
  // take with their leading: the box grown by half a leading before the first line and after
  // the last, short of where another block's lines begin.
  spacing: number;
  room: Box;
  // The share of the block's source text that the area's lines hold.
  characters: number;
};

// How the source's text runs: the height of a horizontal line, or the width of a vertical one,
// as a fraction of the page's height or width, and the distance from one line to the next in
// the same measure, where it is known.
export type Typeset = { size: number; pitch: number | undefined };

// A part of a translation's text, with its marks counted in that part.
export type Piece = { text: string; marks: Mark[] };

// A line much taller than a block's smallest, as a first letter set over several lines or a
// line holding a sum with its limits, does not tell the size of the text.
const TALL_LINE = 1.5;
// A line taller than this many times its width runs downwards.
const DOWNWARD = 2;
// Lines whose boxes overlap by no more than this, as a part of the smaller box, are side by side.
const SLIVER = 0.1;
// A line belongs to the line before it when it begins no higher than this part of that line's
// height above it.
const RISE = 0.5;
// A step from one line to the next is a line's pitch when it lies between these, as parts of
// a line's size; any other is a gap between paragraphs or a turn of column.
const LEAST_STEP = 0.5;
const MOST_STEP = 3;
// A block whose lines are within this part of its kind's size on its page is set in that size.
// Boxes in whole points put a 10 point text anywhere from 9 to 11 points, a tenth either way;
// a footnote of 8 points under 10 point paragraphs is a fifth smaller and keeps its own size.
const SAME_TYPE = 0.15;
const PLAIN_SPACING = 1.25;
// The line spacing of the source is kept between these, as a part of the size of its text.
const SPACING_BOUNDS = [1, 2.4] as const;

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

export function overlap(first: Box, second: Box): number {
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

export function isDownward(places: PagePlace[]): boolean {
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

function spacingOf(typeset: Typeset): number {
  return typeset.pitch === undefined || typeset.size === 0
    ? PLAIN_SPACING
    : Math.min(SPACING_BOUNDS[1], Math.max(SPACING_BOUNDS[0], typeset.pitch / typeset.size));
}

// The room of lines with their leading: grown across the lines by half a leading on each side,
// and on a side where another block's lines stand, no further than halfway to them, so that
// the rooms of two blocks never meet; where the other's lines already reach the box, not at
// all on that side.
function roomOf(box: Box, leading: number, downward: boolean, others: Box[]): Box {
  // Measured along the run of lines: across the page for lines written downwards, down it
  // otherwise. A vertical run goes right to left, but before and after are only sides here.
  const start = downward ? box.left : box.top;
  const end = downward ? box.left + box.width : box.top + box.height;
  let low = start - leading / 2;
  let high = end + leading / 2;
  for (const other of others) {
    const level = downward
      ? other.top < box.top + box.height && box.top < other.top + other.height
      : other.left < box.left + box.width && box.left < other.left + other.width;
    if (!level) {
      continue;
    }
    const otherStart = downward ? other.left : other.top;
    const otherEnd = downward ? other.left + other.width : other.top + other.height;
    if (otherStart + otherEnd >= start + end) {
      high = Math.min(high, Math.max(end, (otherStart + end) / 2));
    } else {
      low = Math.max(low, Math.min(start, (otherEnd + start) / 2));
    }
  }
  return downward
    ? { ...box, left: low, width: high - low }
    : { ...box, top: low, height: high - low };
}

function areaOf(lines: PagePlace[], downward: boolean, typeset: Typeset, others: Box[]): Area {
  const boxes = lines.map(({ left, top, width, height }) => ({ left, top, width, height }));
  const box = union(boxes);
  const spacing = spacingOf(typeset);
  return {
    page: lines[0]?.page ?? 0,
    box,
    lines: boxes,
    vertical: downward,
    typeset,
    spacing,
    room: roomOf(box, (spacing - 1) * typeset.size, downward, others),
    characters: lines.reduce((sum, line) => sum + (line.end - line.start), 0),
  };
}

// The runs of one block's lines: its lines in the order of its text, a new run wherever the
// next line does not go on from the last in the same column, or would make the run cover lines
// of other blocks.
function runsOf(places: PagePlace[], othersOn: (page: number) => Box[]): PagePlace[][] {
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
  return runs;
}

// The areas of one block, one a run of its lines, each set as the block's text runs on its
// page.
export function areasOf(
  places: PagePlace[],
  othersOn: (page: number) => Box[],
  typesetOn: (page: number) => Typeset,
): Area[] {
  const downward = isDownward(places);
  return runsOf(places, othersOn).map((run) => {
    const page = run[0]?.page ?? 0;
    return areaOf(run, downward, typesetOn(page), othersOn(page));
  });
}

// The sizes and the steps between lines that a set of lines shows, measured against its
// smallest line: a line much taller than that is left out, and so is a step that is no line's
// pitch beside it. Each step is measured three ways, from a line's top, its middle and its foot
// to the next one's, and the pitch is the median of them all. From the tops a step is not moved
// by a line boxed deeper below its letters, as one with a lowered letter; from the feet not by
// one boxed taller above them, as one with a raised letter or a formula; from the middles by
// either, or by a box a point off in whole points at its top or its foot, only half as much.
// Of the other measures tried over two papers, each way alone and the first middle to the last
// over the steps each lost some of five blocks whose print is known: two body paragraphs with
// a tall line, which read 10 points top to top where their print's pitch is 12, and 11.5 so,
// and three captions of three lines, whose middles stand 9.5 and 10.5 points apart and which
// read 10. Tops and feet pooled kept the five, but took a body paragraph for smaller print and
// two captions for the body's, which this one does not.
type Measures = { sizes: number[]; pitches: number[] };

function measuresOf(places: PagePlace[]): Measures {
  const lines = [...places].sort((first, second) => first.start - second.start);
  const downward = isDownward(lines);
  const extent = (line: PagePlace) => (downward ? line.width : line.height);
  const ordinary = Math.min(...lines.map(extent));
  const sizes = lines.map(extent).filter((size) => size <= TALL_LINE * ordinary);
  const pitches: number[] = [];
  lines.slice(1).forEach((line, index) => {
    const last = lines[index];
    if (last === undefined || !goesOn(last, line, downward)) {
      return;
    }
    const steps = downward
      ? [0, 0.5, 1].map(
          (part) => last.left + last.width * part - (line.left + line.width * part),
        )
      : [0, 0.5, 1].map((part) => line.top + line.height * part - (last.top + last.height * part));
    for (const step of steps) {
      if (step > LEAST_STEP * ordinary && step < MOST_STEP * ordinary) {
        pitches.push(step);
      }
    }
  });
  return { sizes, pitches };
}

// The lines of a block that tell the size of its text: those that hold no formula, as a line
// with a sum and its limits stands taller than the text around it. A block that is all
// formula is told by all its lines.
function textLinesOf(block: Block, places: PagePlace[]): PagePlace[] {
  const formulas = ("marks" in block ? block.marks : []).filter((mark) => mark.kind === "formula");
  const plain = places.filter(
    (place) => !formulas.some((mark) => mark.start < place.end && place.start < mark.end),
  );
  return plain.length > 0 ? plain : places;
}

function typesetOf(measures: Measures): Typeset | undefined {
  const size = median(measures.sizes);
  return size === undefined ? undefined : { size, pitch: median(measures.pitches) };
}

// Where every block's lines stand on each page, so that each block's areas keep off the
// others', and how each type of text runs on each page. A type is a kind of block, and for a
// heading its level too. The engine gives the boxes of lines in whole points, so the boxes of
// one paragraph, or of one page's paragraphs, may run a point or two taller or shorter than
// those of the next in the same print; what all of a document's lines of a type show together
// is its print, and a page keeps its own only where it clearly differs. The body print is what
// all the document's paragraphs show together, or its list items where it has no paragraph,
// or all its text, tables and listings aside, where it has neither. The runs of three lines or
// more of every block, translated or not, tell the columns of each page.
export type Layout = {
  boxes: Map<number, { blockNumber: number; box: Box }[]>;
  kinds: Map<string, Typeset>;
  body: Typeset | undefined;
  runs: Map<number, Box[]>;
};

// Paragraphs and list items are the body text: their print is told by how far apart their
// lines are, against the body print's pitch, since the boxes of their lines are taller wherever
// a line holds a formula or a raised letter.
const BODY_KINDS = new Set<Block["kind"]>(["paragraph", "list_item"]);

export function isBody(kind: Block["kind"]): boolean {
  return BODY_KINDS.has(kind);
}

// A run of this many lines or more is one a column is told by.
const COLUMN_RUN = 3;
const PARAGRAPH: Block["kind"] = "paragraph";
const LIST_ITEM: Block["kind"] = "list_item";
// The measures of all the text of a document are gathered under this key beside its types'.
const ALL_TEXT = "*";

function typeOf(block: Block): string {
  return block.kind === "heading" ? `${block.kind}${block.level}` : block.kind;
}

const KEY_PARTS = "|";

function kindOnPage(page: number, block: Block): string {
  return `${page}${KEY_PARTS}${typeOf(block)}`;
}

function paragraphsOn(page: number): string {
  return `${page}${KEY_PARTS}${PARAGRAPH}`;
}

function gathered(into: Map<string, Measures>, key: string, measures: Measures): void {
  const kept = into.get(key) ?? { sizes: [], pitches: [] };
  into.set(key, {
    sizes: [...kept.sizes, ...measures.sizes],
    pitches: [...kept.pitches, ...measures.pitches],
  });
}

function pagePlacesOf(block: Block): PagePlace[] {
  return block.places.filter((place) => place.kind === "page");
}

function othersOf(layoutBoxes: Layout["boxes"], page: number, blockNumber: number): Box[] {
  return (layoutBoxes.get(page) ?? [])
    .filter((other) => other.blockNumber !== blockNumber)
    .map((other) => other.box);
}

const NO_MEASURES: Measures = { sizes: [], pitches: [] };

export function layoutOf(blocks: Block[]): Layout {
  const boxes = new Map<number, { blockNumber: number; box: Box }[]>();
  const measured = new Map<string, Measures>();
  const throughout = new Map<string, Measures>();
  const bodyKeys = new Set<string>();
  blocks.forEach((block, blockNumber) => {
    const byPage = new Map<number, PagePlace[]>();
    for (const place of pagePlacesOf(block)) {
      const { left, top, width, height } = place;
      boxes.set(place.page, [
        ...(boxes.get(place.page) ?? []),
        { blockNumber, box: { left, top, width, height } },
      ]);
      byPage.set(place.page, [...(byPage.get(place.page) ?? []), place]);
    }
    byPage.forEach((places, page) => {
      const own = measuresOf(textLinesOf(block, places));
      gathered(measured, kindOnPage(page, block), own);
      gathered(throughout, typeOf(block), own);
      if (!KEPT_AS_PAGE.has(block.kind)) {
        gathered(throughout, ALL_TEXT, own);
      }
      if (isBody(block.kind)) {
        bodyKeys.add(kindOnPage(page, block));
      }
    });
  });
  const body = [PARAGRAPH, LIST_ITEM, ALL_TEXT]
    .map((type) => typesetOf(throughout.get(type) ?? NO_MEASURES))
    .find((typeset) => typeset !== undefined);
  const bodyPrint = pitchedOf(body);
  const kinds = new Map<string, Typeset>();
  measured.forEach((measures, key) => {
    const onPage = typesetOf(measures);
    if (bodyPrint !== undefined && bodyKeys.has(key)) {
      // A block of body text is set by its own pitch, or where it has none by its page's
      // paragraphs': of body text, only the print of a page's paragraphs is kept.
      if (key.endsWith(`${KEY_PARTS}${PARAGRAPH}`)) {
        kinds.set(
          key,
          onPage?.pitch === undefined
            ? bodyPrint
            : printOfPitch(onPage.pitch, Math.min(...measures.sizes), bodyPrint),
        );
      }
      return;
    }
    const type = key.slice(key.indexOf(KEY_PARTS) + 1);
    const whole = typesetOf(throughout.get(type) ?? NO_MEASURES);
    // A page keeps its own size only where its lines of the type show a pitch: the box of a
    // lone line, a heading's above all, tells its print too loosely to set a page apart.
    const own =
      onPage !== undefined &&
      onPage.pitch !== undefined &&
      whole !== undefined &&
      Math.abs(onPage.size / whole.size - 1) > SAME_TYPE;
    const typeset = own || whole === undefined ? onPage : whole;
    if (typeset !== undefined) {
      kinds.set(key, typeset);
    }
  });
  const runs = new Map<number, Box[]>();
  blocks.forEach((block, blockNumber) => {
    const places = pagePlacesOf(block);
    if (isDownward(places)) {
      return;
    }
    for (const run of runsOf(places, (page) => othersOf(boxes, page, blockNumber))) {
      const page = run[0]?.page;
      if (page !== undefined && run.length >= COLUMN_RUN) {
        runs.set(page, [...(runs.get(page) ?? []), union(run)]);
      }
    }
  });
  return { boxes, kinds, body, runs };
}

const NO_TYPESET: Typeset = { size: 0, pitch: undefined };

// A typeset whose pitch is known.
type PitchedTypeset = { size: number; pitch: number };

function pitchedOf(typeset: Typeset | undefined): PitchedTypeset | undefined {
  return typeset?.pitch === undefined ? undefined : { size: typeset.size, pitch: typeset.pitch };
}

// The print of body text whose lines stand this far apart: the body print where the pitch is
// the body print's, and elsewhere the body print's size in the proportion of the two pitches,
// as a footnote's lines are closer by as much as its letters are smaller. A pitch clearly wider
// is taken for larger letters only where the smallest line is larger too: the lines of a
// paragraph that holds a formula, or that the engine boxed unevenly, stand further apart than
// its print's, 24 points for a paragraph of the 12 point pitch in one paper.
function printOfPitch(pitch: number, smallest: number, body: PitchedTypeset): PitchedTypeset {
  const ratio = pitch / body.pitch;
  const told = { size: body.size * ratio, pitch };
  if (ratio < 1 - SAME_TYPE) {
    return told;
  }
  if (ratio > 1 + SAME_TYPE && smallest > body.size * (1 + SAME_TYPE)) {
    return told;
  }
  return body;
}

// A block of body text is set by its own pitch, and one of a single line, which has none, in
// what its page's paragraphs take.
function bodyTypeset(
  own: Measures,
  paragraphs: Typeset | undefined,
  body: PitchedTypeset,
): Typeset {
  const ownTypeset = typesetOf(own);
  if (ownTypeset?.pitch === undefined) {
    return paragraphs ?? body;
  }
  return printOfPitch(ownTypeset.pitch, Math.min(...own.sizes), body);
}

// How the body text of a page is set in the source: the body print's size and pitch, as
// fractions of the page's height, and the leading of the page's paragraphs, or of the
// document's where the page has none.
export type BodyPrint = { size: number; pitch: number; spacing: number };

export function bodyPrintOn(layout: Layout, page: number): BodyPrint {
  const body = layout.body ?? NO_TYPESET;
  const spacing = spacingOf(layout.kinds.get(paragraphsOn(page)) ?? body);
  return { size: body.size, pitch: body.pitch ?? body.size * spacingOf(body), spacing };
}

// A block is set in the type of its kind, unless its lines show it clearly larger or smaller.
// Where the block and its kind both have a pitch, the pitch tells the size, as the steps
// between lines are surer than boxes in whole points: a footnote among paragraphs is smaller
// by as much as its lines are closer, and a block is larger only where its smallest line is
// too. A block of one line has no pitch, and the box of one line tells its print too loosely,
// from 9 to 17 points for one 12 point heading, so it takes its kind's type.
function blockTypeset(own: Measures, kind: Typeset | undefined): Typeset {
  const ownTypeset = typesetOf(own);
  if (kind === undefined) {
    return ownTypeset ?? NO_TYPESET;
  }
  if (ownTypeset?.pitch === undefined || kind.pitch === undefined) {
    return kind;
  }
  const smallest = Math.min(...own.sizes);
  const told = (kind.size * ownTypeset.pitch) / kind.pitch;
  if (told > kind.size * (1 + SAME_TYPE) && smallest > kind.size * (1 + SAME_TYPE)) {
    return { size: ownTypeset.size, pitch: ownTypeset.pitch };
  }
  if (told < kind.size * (1 - SAME_TYPE)) {
    return { size: told, pitch: ownTypeset.pitch };
  }
  return kind;
}

// Marks that open or close a phrase, before and after which a line is not broken in Chinese
// and Japanese.
const NO_BREAK_BEFORE = new Set([..."，。、；：！？）」』》〉】〕”’…—·.,;:!?)]}%"]);
const NO_BREAK_AFTER = new Set([..."（「『《〈【〔“‘([{"]);
const SPACE = /\s/u;
// Characters of scripts written without spaces between words.
const UNSPACED = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;

const LETTER = /\p{L}/u;
// A character of a script written without spaces takes the room of about this many Latin
// letters on a line.
const UNSPACED_WIDTH = 2;

// Whether texts are written in a script without spaces between words: more of their lines are
// Han, kana or hangul than not, each such character taking the room of two other letters. A
// translation into Chinese that keeps terms of art in English can hold more Latin letters than
// Chinese characters, 14,057 to 10,668 in one paper, and still reads as Chinese.
export function writtenUnspaced(texts: Iterable<string>): boolean {
  let unspaced = 0;
  let spaced = 0;
  for (const text of texts) {
    for (const character of text) {
      if (UNSPACED.test(character)) {
        unspaced += UNSPACED_WIDTH;
      } else if (LETTER.test(character)) {
        spaced += 1;
      }
    }
  }
  return unspaced > spaced;
}

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
  kind: Block["kind"];
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

// Where a block's translation is set on the pages, and what part of it each area holds,
// parted by the share of the source's text each area's lines hold.
export function placedOf(
  block: Block,
  blockNumber: number,
  translated: TranslatedBlock,
  layout: Layout,
  gap: string,
): AreaText[] {
  const places = pagePlacesOf(block);
  if (KEPT_AS_PAGE.has(block.kind) || places.length === 0) {
    return [];
  }
  const othersOn = (page: number) => othersOf(layout.boxes, page, blockNumber);
  const bodyPrint = pitchedOf(layout.body);
  const typesetOn = (page: number) => {
    const own = measuresOf(textLinesOf(block, places.filter((place) => place.page === page)));
    return isBody(block.kind) && bodyPrint !== undefined
      ? bodyTypeset(own, layout.kinds.get(paragraphsOn(page)), bodyPrint)
      : blockTypeset(own, layout.kinds.get(kindOnPage(page, block)));
  };
  const areas = areasOf(places, othersOn, typesetOn);
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
    kind: block.kind,
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

// Where the part of a block's translation each area holds begins in the whole of it, in code
// points: the parts are the whole in order, but for parts of spaces alone, which are given
// empty and begin nowhere.
export function pieceStarts(whole: string, areas: AreaText[]): (number | undefined)[] {
  const characters = [...whole];
  let reached = 0;
  return areas.map(({ piece }) => {
    const length = [...piece.text].length;
    if (length === 0) {
      return undefined;
    }
    for (let at = reached; at + length <= characters.length; at += 1) {
      if (characters.slice(at, at + length).join("") === piece.text) {
        reached = at + length;
        return at;
      }
    }
    return undefined;
  });
}
