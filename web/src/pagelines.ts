import { isDownward, type Box } from "./areas";
import type { Block, PagePlace } from "./client";
import { pagePlaces } from "./places";

// Where the text of a PDF's document lies on its pages: the lines of each page, where each
// character of a line lies along it, the part of a line's box that holds a stretch of its
// text, and the line and the word under a point.

// One place of a block on a page, as a rule a line, and the stretch of the block's text it
// holds. A line taken whole stands for all its characters at once: a table's, a picture's, or
// one place that holds several lines of text.
export type Line = {
  blockNumber: number;
  place: PagePlace;
  start: number;
  end: number;
  downward: boolean;
  whole: boolean;
};

// Where the characters of a line lie along it, and what that was read from: the words' ink on
// the drawn page, the characters' widths, or, for text written without spaces, an even share.
export type Cut = { edges: number[]; by: "ink" | "widths" | "evenly" };

// A box on a page.
export type PageBox = { page: number; box: Box };

// A stretch of one block's text, as code points from start to end.
export type Span = { blockNumber: number; start: number; end: number };

// A point on a page, as fractions of its width and height.
export type Point = { x: number; y: number };

// Blocks whose one place is not a line of text but the block's whole box.
const BOXED_KINDS = new Set<Block["kind"]>(["table", "figure"]);
// One place stands for several lines where it is this many times as deep, across its run, as
// the lines of its page are.
const SEVERAL_LINES = 2.5;

const SPACE = /\s/u;
// Characters of scripts written without spaces between words: each is a word of its own, and
// the characters of a line of them are as wide as one another.
const UNSPACED = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
// What a word of a spaced script is made of; any other character stands alone.
const WORDLIKE = /[\p{L}\p{N}\p{M}\p{Pc}'’-]/u;

// A line of a spaced script is broken between words. The engine that reads a scan gives where
// a line begins and ends in the text a few characters off at times, inside a word; such a break
// is settled on a space this many characters away at most.
const SETTLING_REACH = 10;

// The width of a character as a serif face of the browser sets it, at this size: the print of
// a page is not known, but its letters differ in width much as any face's do.
const MEASURING_SIZE = 100;
const MEASURING_FONT = `${MEASURING_SIZE}px serif`;
// The size of the print, as a part of the depth of its line's box: a box holds the letters from
// the tops of the tallest to the feet of the lowest, about the size the print is set at.
const SIZE_PER_DEPTH = 1;

// Reading the words of a line off the drawn page: a pixel is ink where it is this much darker
// than the paper, the paper being as light as this share of the line's pixels are at most. The
// soft edges of scanned letters fill the narrow spaces of a tight line, so where the spaces are
// not found among the gaps of the ink, they are sought again among those of its darkest part.
const PAPER_SHARE = 0.9;
const INK_CONTRASTS = [110, 130];
// The spaces between a line's words are found among its runs of pixels with no ink, each near
// where the widths of the words put it: a gap is taken for a space at a cost of its distance
// from there, in the line's mean characters, and of this much more the narrower it is than
// this part of a mean character, as a gap between letters printed apart is. The gaps are
// sought first among those as wide as a space is set, then, for a line set tight, among
// narrower ones too, each then no further from where it is expected than a few characters.
const NARROW_COST = 3;
const SPACE_WIDTH = 0.5;
// A gap in a line's ink is a space for counting its words where it is this part of the line's
// depth wide at least: a space is about a quarter of the size of the print, the gap between two
// letters much less.
const LEAST_PRINTED_SPACE = 0.15;
const SEARCHES = [
  { least: 0.3, farthest: Infinity },
  { least: 0.12, farthest: 1.5 },
];

function median(values: number[]): number | undefined {
  const sorted = [...values].sort((first, second) => first - second);
  return sorted[Math.floor(sorted.length / 2)];
}

function depthOf(place: PagePlace, downward: boolean): number {
  return downward ? place.width : place.height;
}

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
    width = measurer?.measureText(character).width ?? MEASURING_SIZE / 2;
    widths.set(character, width);
  }
  return width;
}

function widthOfText(characters: string[]): number {
  return characters.reduce((sum, character) => sum + widthOf(character), 0);
}

function isWordlike(character: string | undefined): boolean {
  return character !== undefined && WORDLIKE.test(character) && !UNSPACED.test(character);
}

// Where each character of a run lies when the run fills the extent given: in proportion to the
// characters' widths. The result holds the edge before each character and after the last.
function spread(characters: string[], from: number, to: number): number[] {
  const runs = characters.map(widthOf);
  const total = runs.reduce((sum, width) => sum + width, 0);
  const edges = [from];
  let reached = 0;
  runs.forEach((width, index) => {
    reached += width;
    edges.push(
      total > 0
        ? from + ((to - from) * reached) / total
        : from + ((to - from) * (index + 1)) / runs.length,
    );
  });
  return edges;
}

// A word of a line: where its characters begin and end, as offsets into the line.
type Word = { start: number; end: number };

function wordsOf(characters: string[]): Word[] {
  const words: Word[] = [];
  characters.forEach((character, index) => {
    if (SPACE.test(character)) {
      return;
    }
    const last = words.at(-1);
    if (last !== undefined && last.end === index) {
      last.end = index + 1;
    } else {
      words.push({ start: index, end: index + 1 });
    }
  });
  return words;
}

// How much ink each column across a line's box holds on its drawn page, for each darkness
// that ink is taken at, the box's length along the line and its depth across it, in pixels.
type Ink = { run: number; depth: number; profiles: Uint16Array[] };

function inkOf(canvas: HTMLCanvasElement, place: PagePlace, downward: boolean): Ink | undefined {
  const context = canvas.getContext("2d");
  const left = Math.max(0, Math.floor(place.left * canvas.width));
  const top = Math.max(0, Math.floor(place.top * canvas.height));
  const width = Math.min(canvas.width - left, Math.ceil(place.width * canvas.width));
  const height = Math.min(canvas.height - top, Math.ceil(place.height * canvas.height));
  if (context === null || width <= 0 || height <= 0) {
    return undefined;
  }
  const pixels = context.getImageData(left, top, width, height).data;
  // How light each pixel is, from 0 to 255, and how many pixels are of each lightness.
  const lightness = new Uint8Array(width * height);
  const counts = new Uint32Array(256);
  for (let at = 0; at < lightness.length; at += 1) {
    const light = Math.round(
      ((pixels[at * 4] ?? 0) + (pixels[at * 4 + 1] ?? 0) + (pixels[at * 4 + 2] ?? 0)) / 3,
    );
    lightness[at] = light;
    counts[light] = (counts[light] ?? 0) + 1;
  }
  let darkest = 0;
  while (darkest < 255 && counts[darkest] === 0) {
    darkest += 1;
  }
  let paper = 0;
  for (
    let below = 0;
    paper < 255 && below + (counts[paper] ?? 0) <= PAPER_SHARE * lightness.length;
    paper += 1
  ) {
    below += counts[paper] ?? 0;
  }
  const run = downward ? height : width;
  const profiles = INK_CONTRASTS.filter((contrast) => paper - darkest >= contrast).map(
    (contrast) => {
      const inkBelow = paper - contrast;
      const profile = new Uint16Array(run);
      for (let y = 0; y < height; y += 1) {
        for (let x = 0; x < width; x += 1) {
          const along = downward ? y : x;
          if ((lightness[y * width + x] ?? 255) < inkBelow) {
            profile[along] = (profile[along] ?? 0) + 1;
          }
        }
      }
      return profile;
    },
  );
  return profiles.length === 0 ? undefined : { run, depth: downward ? width : height, profiles };
}

// How many words a line's ink shows: one more than its gaps as wide as a space is at least,
// counted where the ink is taken at its lightest.
function printedWords(ink: Ink): number | undefined {
  const profile = ink.profiles[0];
  if (profile === undefined) {
    return undefined;
  }
  const inked = [...profile.keys()].filter((at) => (profile[at] ?? 0) > 0);
  const first = inked[0];
  const last = inked.at(-1);
  if (first === undefined || last === undefined) {
    return undefined;
  }
  let words = 1;
  for (let at = first; at <= last; at += 1) {
    if (profile[at] === 0) {
      let end = at;
      while (end <= last && profile[end] === 0) {
        end += 1;
      }
      if (end - at >= LEAST_PRINTED_SPACE * ink.depth) {
        words += 1;
      }
      at = end;
    }
  }
  return words;
}

// Where the words of a line stand on its drawn page, in pixels along the run from the start of
// the line's box: between the spaces found among the gaps in its ink.
function inkedWords(
  ink: Ink,
  widths: number[],
  space: number,
  characters: number,
): [number, number][] | undefined {
  if (widths.length === 0) {
    return undefined;
  }
  for (const profile of ink.profiles) {
    const words = wordsInProfile(profile, widths, space, ink.run / characters);
    if (words !== undefined) {
      return words;
    }
  }
  return undefined;
}

// Where the words stand along a line, from how much ink each column across it holds.
function wordsInProfile(
  profile: Uint16Array,
  widths: number[],
  space: number,
  mean: number,
): [number, number][] | undefined {
  const inked = [...profile.keys()].filter((at) => (profile[at] ?? 0) > 0);
  const first = inked[0];
  const last = inked.at(-1);
  if (first === undefined || last === undefined) {
    return undefined;
  }
  // Every run of columns without ink inside the line may be the space between two words.
  const gaps: [number, number][] = [];
  for (let at = first; at <= last; at += 1) {
    if (profile[at] === 0) {
      let end = at;
      while (end <= last && profile[end] === 0) {
        end += 1;
      }
      gaps.push([at, end]);
      at = end;
    }
  }
  const expected = expectedSpaces(widths, space, first, last);
  let chosen: [number, number][] | undefined = widths.length === 1 ? [] : undefined;
  for (const search of SEARCHES) {
    chosen ??= spacesAmong(
      gaps.filter((gap) => gap[1] - gap[0] >= search.least * mean),
      expected,
      mean,
      search.farthest,
    );
  }
  if (chosen === undefined) {
    return undefined;
  }
  const bounds = [first, ...chosen.flat(), last + 1];
  const words: [number, number][] = [];
  for (let index = 0; index < widths.length; index += 1) {
    words.push([bounds[2 * index] ?? first, bounds[2 * index + 1] ?? last + 1]);
  }
  return words;
}

// Where the middle of each space between the words of a line is expected, in pixels along the
// run: the words at their widths and the spaces between them, stretched over the line's ink.
function expectedSpaces(widths: number[], space: number, first: number, last: number): number[] {
  const total = widths.reduce((sum, width) => sum + width, 0) + space * (widths.length - 1);
  const scale = total > 0 ? (last + 1 - first) / total : 0;
  const expected: number[] = [];
  let reached = 0;
  widths.slice(0, -1).forEach((width) => {
    reached += width;
    expected.push(first + (reached + space / 2) * scale);
    reached += space;
  });
  return expected;
}

// The gaps that are the spaces between a line's words: one for each space, in order, each near
// where its space is expected and as wide as a space is, as a gap between two letters printed
// apart is narrower. Where too narrow a gap is all there is for a space, as where two words
// touch, none.
function spacesAmong(
  gaps: [number, number][],
  expected: number[],
  mean: number,
  farthest: number,
): [number, number][] | undefined {
  // A space is as wide as the narrowest of the widest gaps, one for each space, as a line set
  // even spreads its room over its spaces alike; a gap narrower than that is less likely one.
  const widest = gaps.map((gap) => gap[1] - gap[0]).sort((one, other) => other - one);
  const spaceWidth = Math.max(SPACE_WIDTH * mean, widest[expected.length - 1] ?? 0);
  const cost = (space: number, gap: [number, number]) =>
    Math.abs((gap[0] + gap[1]) / 2 - (expected[space] ?? 0)) / mean +
    NARROW_COST * Math.max(0, 1 - (gap[1] - gap[0]) / spaceWidth);
  // For each space and each gap: the least cost of setting the spaces up to this one with this
  // one on that gap, and the gap the space before it is then set on.
  const steps: { cost: number; from: number }[][] = [];
  expected.forEach((_, space) => {
    const before = steps[space - 1];
    let best = { cost: space === 0 ? 0 : Infinity, at: -1 };
    steps.push(
      gaps.map((gap, index) => {
        const step = { cost: best.cost + cost(space, gap), from: best.at };
        const here = before?.[index]?.cost ?? Infinity;
        if (here < best.cost) {
          best = { cost: here, at: index };
        }
        return step;
      }),
    );
  });
  const lastSteps = steps.at(-1) ?? [];
  let at = lastSteps.reduce(
    (found, step, index) => (step.cost < (lastSteps[found]?.cost ?? Infinity) ? index : found),
    0,
  );
  if (!Number.isFinite(lastSteps[at]?.cost ?? Infinity)) {
    return undefined;
  }
  const chosen: [number, number][] = [];
  for (let space = expected.length - 1; space >= 0; space -= 1) {
    const gap = gaps[at];
    if (
      gap === undefined ||
      Math.abs((gap[0] + gap[1]) / 2 - (expected[space] ?? 0)) > farthest * mean
    ) {
      return undefined;
    }
    chosen.unshift(gap);
    at = steps[space]?.[at]?.from ?? -1;
  }
  return chosen;
}

// Moves each break between two lines of a block that the engine put inside a word to a space
// before or after it, within reach, where no character near is of a script written without
// spaces: to the one that gives the line before it as many words as its print shows, and then
// the line after it, where that is known, and otherwise to the nearer.
function settleBreaks(
  lines: Line[],
  characters: string[],
  printed: (line: Line) => number | undefined,
): void {
  lines.slice(1).forEach((line, index) => {
    const before = lines[index];
    const at = line.place.start;
    if (before === undefined || before.place.end !== at) {
      return;
    }
    before.end = at;
    line.start = at;
    const near = characters.slice(Math.max(0, at - SETTLING_REACH), at + SETTLING_REACH);
    if (
      SPACE.test(characters[at] ?? " ") ||
      SPACE.test(characters[at - 1] ?? " ") ||
      near.some((character) => UNSPACED.test(character))
    ) {
      return;
    }
    const candidates: { settled: number; distance: number }[] = [];
    for (let space = at; space < at + SETTLING_REACH; space += 1) {
      if (SPACE.test(characters[space] ?? "")) {
        candidates.push({ settled: space + 1, distance: space + 1 - at });
        break;
      }
    }
    for (let space = at - 1; space >= at - SETTLING_REACH; space -= 1) {
      if (SPACE.test(characters[space] ?? "")) {
        candidates.push({ settled: space + 1, distance: at - space - 1 });
        break;
      }
    }
    const fitting = candidates.filter(
      ({ settled }) => settled > before.start && settled < line.place.end,
    );
    const printedBefore = fitting.length > 1 ? printed(before) : undefined;
    const printedAfter = fitting.length > 1 ? printed(line) : undefined;
    const score = (settled: number) =>
      (wordsOf(characters.slice(before.start, settled)).length === printedBefore ? 2 : 0) +
      (wordsOf(characters.slice(settled, line.place.end)).length === printedAfter ? 1 : 0);
    const chosen = fitting.sort(
      (one, other) => score(other.settled) - score(one.settled) || one.distance - other.distance,
    )[0];
    if (chosen !== undefined) {
      before.end = chosen.settled;
      line.start = chosen.settled;
    }
  });
}

// The text of a PDF's document as it lies on its pages, and where on a line each character
// lies: read off the drawn page where its words can be told apart there, and otherwise by the
// widths of the characters, a justified line's spare room spread over its spaces.
export class PageText {
  readonly onPages = new Map<number, Line[]>();
  readonly ofBlocks = new Map<number, Line[]>();
  // The text of each block as code points, as offsets count them.
  readonly characters: string[][];
  // The formulas of each block, which a selection takes whole or not at all.
  private readonly formulas: Map<number, { start: number; end: number }[]>;
  private readonly drawings = new Map<number, HTMLCanvasElement>();
  private readonly edgesKept = new WeakMap<Line, Cut>();
  // The ink of the lines of the pages drawn, read when first needed.
  private readonly inks = new Map<Line, Ink | undefined>();
  // The blocks whose lines are set by their print as their pages are drawn now.
  private readonly aligned = new Set<number>();

  constructor(blocks: Block[]) {
    this.characters = blocks.map((block) => [...block.text]);
    this.formulas = new Map(
      blocks.map((block, blockNumber) => [
        blockNumber,
        ("marks" in block ? block.marks : []).filter((mark) => mark.kind === "formula"),
      ]),
    );
    blocks.forEach((block, blockNumber) => {
      const places = pagePlaces(block.places);
      const downward = isDownward(places);
      const whole = BOXED_KINDS.has(block.kind);
      const lines = [...places]
        .sort((first, second) => first.start - second.start)
        .map((place) => ({
          blockNumber,
          place,
          start: place.start,
          end: place.end,
          downward,
          whole,
        }));
      settleBreaks(lines, this.characters[blockNumber] ?? [], () => undefined);
      this.ofBlocks.set(blockNumber, lines);
      for (const line of lines) {
        this.onPages.set(line.place.page, [...(this.onPages.get(line.place.page) ?? []), line]);
      }
    });
    // A block that has one place for all its text is a line only where that place is no
    // deeper than the lines of its page: a block read as one box, as a table is, is taken
    // whole.
    this.onPages.forEach((lines) => {
      const depth = median(lines.map((line) => depthOf(line.place, line.downward))) ?? 0;
      for (const line of lines) {
        const own = this.ofBlocks.get(line.blockNumber) ?? [];
        if (own.length === 1 && depthOf(line.place, line.downward) > SEVERAL_LINES * depth) {
          line.whole = true;
        }
      }
    });
  }

  textOf(blockNumber: number): string[] {
    return this.characters[blockNumber] ?? [];
  }

  // Tells where a page is drawn, or that its drawing is gone, so that its lines are read off
  // the drawing anew: where they break, and where their words stand.
  drawnOn(page: number, canvas: HTMLCanvasElement | undefined): void {
    if (canvas === undefined) {
      this.drawings.delete(page);
    } else {
      this.drawings.set(page, canvas);
    }
    const lines = this.onPages.get(page) ?? [];
    for (const line of lines) {
      this.inks.delete(line);
    }
    for (const blockNumber of new Set(lines.map((line) => line.blockNumber))) {
      this.aligned.delete(blockNumber);
      for (const line of this.ofBlocks.get(blockNumber) ?? []) {
        this.edgesKept.delete(line);
      }
    }
  }

  // Sets where the lines of a block begin and end in its text by their print, once its pages
  // are drawn, when a line of it is first cut: where the words the print shows on each line
  // add up to the block's words, each line holds as many as it shows; otherwise each break the
  // engine put inside a word is settled on a space.
  private align(blockNumber: number): void {
    if (this.aligned.has(blockNumber)) {
      return;
    }
    this.aligned.add(blockNumber);
    const own = this.ofBlocks.get(blockNumber) ?? [];
    const characters = this.textOf(blockNumber);
    const printed = (line: Line) => {
      const ink = this.inkOfLine(line);
      return ink === undefined ? undefined : printedWords(ink);
    };
    const first = own[0];
    const last = own.at(-1);
    const spaced = !characters.some((character) => UNSPACED.test(character));
    const counts =
      own.length > 1 && spaced && !own.some((line) => line.whole) ? own.map(printed) : [];
    const words = wordsOf(characters);
    const total = counts.reduce<number | undefined>(
      (sum, count) => (sum === undefined || count === undefined ? undefined : sum + count),
      0,
    );
    if (first !== undefined && last !== undefined && counts.length > 0 && total === words.length) {
      let taken = 0;
      own.forEach((line, index) => {
        line.start = index === 0 ? first.place.start : (words[taken]?.start ?? line.start);
        taken += counts[index] ?? 0;
        line.end = line === last ? last.place.end : (words[taken]?.start ?? line.end);
      });
    } else {
      for (const line of own) {
        line.start = line.place.start;
        line.end = line.place.end;
      }
      settleBreaks(own, characters, printed);
    }
    for (const line of own) {
      this.edgesKept.delete(line);
    }
  }

  private inkOfLine(line: Line): Ink | undefined {
    if (!this.inks.has(line)) {
      const canvas = this.drawings.get(line.place.page);
      this.inks.set(
        line,
        canvas === undefined || canvas.width === 0
          ? undefined
          : inkOf(canvas, line.place, line.downward),
      );
    }
    return this.inks.get(line);
  }

  // How a line is cut: where each character begins along its run, as a part of the run, and
  // where the last ends, and what that was read from.
  cutOf(line: Line): Cut {
    this.align(line.blockNumber);
    let cut = this.edgesKept.get(line);
    if (cut === undefined) {
      cut = this.workedCut(line);
      this.edgesKept.set(line, cut);
    }
    return cut;
  }

  private edgesOf(line: Line): number[] {
    return this.cutOf(line).edges;
  }

  private workedCut(line: Line): Cut {
    const { place, downward } = line;
    const characters = this.textOf(line.blockNumber).slice(line.start, line.end);
    if (characters.some((character) => UNSPACED.test(character))) {
      const edges = characters.map((_, index) => index / characters.length).concat(1);
      return { edges, by: "evenly" };
    }
    const canvas = this.drawings.get(place.page);
    if (canvas === undefined || canvas.width === 0) {
      return { edges: spread(characters, 0, 1), by: "widths" };
    }
    const words = wordsOf(characters);
    const ink = this.inkOfLine(line);
    const inked =
      ink === undefined
        ? undefined
        : inkedWords(
            ink,
            words.map((word) => widthOfText(characters.slice(word.start, word.end))),
            widthOf(" "),
            characters.length,
          );
    if (ink !== undefined && inked !== undefined) {
      return { edges: this.inkedEdges(characters, words, inked, ink.run), by: "ink" };
    }
    return { edges: this.justifiedEdges(characters, canvas, place, downward), by: "widths" };
  }

  // Each word fills the extent its ink takes, its characters in proportion to their widths;
  // the spaces between two words fill the gap between them.
  private inkedEdges(
    characters: string[],
    words: Word[],
    extents: [number, number][],
    run: number,
  ): number[] {
    const edges: number[] = new Array<number>(characters.length + 1).fill(0);
    let reached = 0;
    let at = 0;
    words.forEach((word, index) => {
      const [from, to] = extents[index] ?? [reached, reached];
      spread(characters.slice(at, word.start), reached, from).forEach((edge, offset) => {
        edges[at + offset] = edge / run;
      });
      spread(characters.slice(word.start, word.end), from, to).forEach((edge, offset) => {
        edges[word.start + offset] = edge / run;
      });
      reached = to;
      at = word.end;
    });
    spread(characters.slice(at), reached, run).forEach((edge, offset) => {
      edges[at + offset] = edge / run;
    });
    return edges;
  }

  // The characters at their widths for the size of the print, and what room the line has over
  // them spread over its spaces, as a justified line spreads it; a line that holds less room
  // than its characters take is set closer, all of them alike.
  private justifiedEdges(
    characters: string[],
    canvas: HTMLCanvasElement,
    place: PagePlace,
    downward: boolean,
  ): number[] {
    const run = downward ? place.height * canvas.height : place.width * canvas.width;
    const depth = downward ? place.width * canvas.width : place.height * canvas.height;
    const scale = (depth * SIZE_PER_DEPTH) / MEASURING_SIZE;
    const natural = characters.map((character) => widthOf(character) * scale);
    const taken = natural.reduce((sum, width) => sum + width, 0);
    const spaces = characters.filter((character) => SPACE.test(character)).length;
    if (taken >= run || spaces === 0 || run <= 0) {
      return spread(characters, 0, 1);
    }
    const added = (run - taken) / spaces;
    const edges = [0];
    let reached = 0;
    characters.forEach((character, index) => {
      reached += (natural[index] ?? 0) + (SPACE.test(character) ? added : 0);
      edges.push(reached / run);
    });
    return edges;
  }

  // The part of a line's box that holds the characters from start to end of its block's text,
  // cut across the run, down the line where the text runs downwards. A line taken whole is
  // covered whole by any part of it.
  coveredBox(line: Line, start: number, end: number): Box | undefined {
    this.align(line.blockNumber);
    const { place } = line;
    const from = Math.max(start, line.start);
    const to = Math.min(end, line.end);
    if (from >= to) {
      return undefined;
    }
    if (line.whole || line.end === line.start) {
      return { left: place.left, top: place.top, width: place.width, height: place.height };
    }
    const edges = this.edgesOf(line);
    const head = edges[from - line.start] ?? 0;
    const tail = edges[to - line.start] ?? 1;
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

  coveredBoxes(span: Span): PageBox[] {
    return (this.ofBlocks.get(span.blockNumber) ?? []).flatMap((line) => {
      const box = this.coveredBox(line, span.start, span.end);
      return box === undefined ? [] : [{ page: line.place.page, box }];
    });
  }

  // The word around a character: settled on a word's edges in a spaced script, the character
  // alone in an unspaced one, alone where it is a mark between words, and a formula whole.
  wordAround(blockNumber: number, at: number): Span {
    const characters = this.textOf(blockNumber);
    let start = at;
    let end = Math.min(at + 1, characters.length);
    if (isWordlike(characters[at])) {
      while (isWordlike(characters[start - 1])) {
        start -= 1;
      }
      while (isWordlike(characters[end])) {
        end += 1;
      }
    }
    for (const formula of this.formulas.get(blockNumber) ?? []) {
      if (formula.start < end && start < formula.end) {
        start = Math.min(start, formula.start);
        end = Math.max(end, formula.end);
      }
    }
    return { blockNumber, start, end };
  }

  // The character of a line under a point, settled on the word around it; a line taken whole
  // stands for all of it.
  spanAt(line: Line, point: Point): Span {
    this.align(line.blockNumber);
    const { place } = line;
    const count = line.end - line.start;
    if (line.whole || count === 0) {
      return { blockNumber: line.blockNumber, start: line.start, end: line.end };
    }
    const along = line.downward
      ? (point.y - place.top) / place.height
      : (point.x - place.left) / place.width;
    const edges = this.edgesOf(line);
    let at = 0;
    while (at < count - 1 && (edges[at + 1] ?? 1) <= along) {
      at += 1;
    }
    return this.wordAround(line.blockNumber, line.start + at);
  }

  wholeBlock(blockNumber: number): Span {
    return { blockNumber, start: 0, end: this.textOf(blockNumber).length };
  }
}

// The line nearest a point on a page of the shape given (its height as a part of its width),
// distances taken in one measure across and down, and how far it is, in the line's depths. Of
// lines that hold the point, the smallest.
export function nearestLine(
  lines: Line[],
  point: Point,
  shape: number,
): { line: Line; depths: number } | undefined {
  let nearest: { line: Line; depths: number } | undefined;
  let least = Infinity;
  let leastArea = Infinity;
  for (const line of lines) {
    const { left, top, width, height } = line.place;
    const across = Math.max(left - point.x, 0, point.x - (left + width));
    const down = Math.max(top - point.y, 0, point.y - (top + height)) * shape;
    const distance = Math.hypot(across, down);
    const area = width * height;
    if (distance < least || (distance === least && area < leastArea)) {
      least = distance;
      leastArea = area;
      const depth = line.downward ? width : height * shape;
      nearest = { line, depths: depth > 0 ? distance / depth : Infinity };
    }
  }
  return nearest;
}
