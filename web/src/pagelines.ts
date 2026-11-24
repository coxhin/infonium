import { isDownward, type Box } from "./areas";
import type { Block, PagePlace } from "./client";
import { inkedWords, printedWords, readInk, type Ink } from "./pageink";
import { pagePlaces } from "./places";

// Where the text of a PDF's document lies on its pages: the lines of each page and the stretch
// of text each holds, where each character of a line lies along it, the part of a line's box
// that holds a stretch of its text, and the line and the word under a point.

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
// A line may also be broken after a hyphen, inside a word.
const HYPHENS = new Set(["-", "‐", "‑"]);
// Characters of scripts written without spaces between words: each is a word of its own, and
// the characters of a line of them are as wide as one another.
const UNSPACED = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
// What a word of a spaced script is made of; any other character stands alone.
const WORDLIKE = /[\p{L}\p{N}\p{M}\p{Pc}'’-]/u;

// The engine that reads a scan gives where a line begins and ends in the text a few characters
// off at times, inside a word or a word away, and splits some blocks into lines of even length.
// A break is moved, to a place this many characters away at most, only where the print proves
// it: both lines it bounds then hold as many words as their print shows.
const BREAK_REACH = 15;

// The width of a character as a serif face of the browser sets it, at this size: the print of
// a page is not known, but its letters differ in width much as any face's do.
const MEASURING_SIZE = 100;
const MEASURING_FONT = `${MEASURING_SIZE}px serif`;

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

// A word of a line: where its characters begin and end, as offsets into the line. A word
// broken after a hyphen at a line's end is a word on each line.
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

// Where a line may begin: at the start of a word, or after a hyphen inside one.
function mayBreakBefore(characters: string[], at: number): boolean {
  const before = characters[at - 1];
  const after = characters[at];
  return (
    before !== undefined &&
    after !== undefined &&
    !SPACE.test(after) &&
    (SPACE.test(before) || HYPHENS.has(before))
  );
}

// Each word fills the extent its ink takes, its characters in proportion to their widths; the
// spaces between two words fill the gap between them.
function inkedEdges(
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

// Where the breaks between a block's lines fall, each moved only on the proof of the print.
// For every way of setting each break at a place it may fall within reach of where the engine
// put it, the one that gives the most lines as many words as their print shows, and of those
// the one whose words are the most like their print's in length, then the one that moves the
// breaks least; then a break stays where the engine put it unless both lines it bounds hold as
// many words as their print shows, until no more break goes back.
function brokenByPrint(
  lines: Line[],
  characters: string[],
  printed: (number[] | undefined)[],
): number[] {
  const engine = lines.slice(1).map((line) => line.place.start);
  const start = lines[0]?.place.start ?? 0;
  const end = lines.at(-1)?.place.end ?? characters.length;
  const contiguous = lines
    .slice(1)
    .every((line, index) => lines[index]?.place.end === line.place.start);
  const near = (at: number) =>
    characters
      .slice(Math.max(0, at - BREAK_REACH), at + BREAK_REACH)
      .some((character) => UNSPACED.test(character));
  const candidates = engine.map((at) => {
    const found = [at];
    if (contiguous && !near(at)) {
      for (let offset = at - BREAK_REACH; offset <= at + BREAK_REACH; offset += 1) {
        if (offset !== at && offset > start && offset < end && mayBreakBefore(characters, offset)) {
          found.push(offset);
        }
      }
    }
    return found;
  });
  // The lengths of the words of a stretch of the text, kept for each stretch asked about.
  const kept = new Map<string, number[]>();
  const wordsIn = (from: number, to: number) => {
    const key = `${from}:${to}`;
    let found = kept.get(key);
    if (found === undefined) {
      found = wordsOf(characters.slice(from, to)).map((word) =>
        widthOfText(characters.slice(from + word.start, from + word.end)),
      );
      kept.set(key, found);
    }
    return found;
  };
  const fits = (line: number, from: number, to: number) =>
    from < to && printed[line] !== undefined && printed[line]?.length === wordsIn(from, to).length;
  // How far the lengths of a line's words in its print are from those of the words of its
  // text, each as a part of all: of two settings that both give a line its count of words, the
  // one whose words are the print's words is the nearer.
  const misfit = (line: number, from: number, to: number) => {
    const print = printed[line];
    if (print === undefined || from >= to) {
      return 0;
    }
    const text = wordsIn(from, to);
    if (print.length !== text.length) {
      return 0;
    }
    const printTotal = print.reduce((sum, width) => sum + width, 0) || 1;
    const textTotal = text.reduce((sum, width) => sum + width, 0) || 1;
    return print.reduce(
      (sum, width, index) => sum + Math.abs(width / printTotal - (text[index] ?? 0) / textTotal),
      0,
    );
  };
  // The best setting of the breaks up to each one, ending at each of its places: how many lines
  // fit, how far their words are from their print's, how far the breaks moved, and the place
  // of the break before.
  type Step = { fitting: number; misfit: number; moved: number; from: number };
  const better = (one: Step, other: Step) =>
    one.fitting > other.fitting ||
    (one.fitting === other.fitting &&
      (one.misfit < other.misfit || (one.misfit === other.misfit && one.moved < other.moved)));
  const steps: Step[][] = [];
  candidates.forEach((places, index) => {
    const before = steps[index - 1];
    const fromPlaces = index === 0 ? [start] : (candidates[index - 1] ?? []);
    steps.push(
      places.map((place) => {
        let best: Step = { fitting: -1, misfit: Infinity, moved: Infinity, from: 0 };
        fromPlaces.forEach((from, fromIndex) => {
          const previous = before?.[fromIndex] ?? { fitting: 0, misfit: 0, moved: 0, from: 0 };
          const step = {
            fitting: previous.fitting + (fits(index, from, place) ? 1 : 0),
            misfit: previous.misfit + misfit(index, from, place),
            moved: previous.moved + Math.abs(place - (engine[index] ?? place)),
            from: fromIndex,
          };
          if (better(step, best)) {
            best = step;
          }
        });
        return best;
      }),
    );
  });
  const lastPlaces = candidates.at(-1) ?? [];
  const lastSteps = steps.at(-1) ?? [];
  let at = 0;
  let best: Step = { fitting: -1, misfit: Infinity, moved: Infinity, from: 0 };
  lastPlaces.forEach((place, index) => {
    const step = lastSteps[index];
    if (step === undefined) {
      return;
    }
    const whole = {
      ...step,
      fitting: step.fitting + (fits(engine.length, place, end) ? 1 : 0),
      misfit: step.misfit + misfit(engine.length, place, end),
    };
    if (better(whole, best)) {
      best = whole;
      at = index;
    }
  });
  const breaks: number[] = new Array<number>(engine.length).fill(0);
  for (let index = engine.length - 1; index >= 0; index -= 1) {
    breaks[index] = candidates[index]?.[at] ?? engine[index] ?? 0;
    at = steps[index]?.[at]?.from ?? 0;
  }
  // A break the print does not prove for both its lines goes back to the engine's place.
  const bounds = () => [start, ...breaks, end];
  let changed = true;
  while (changed) {
    changed = false;
    const edges = bounds();
    breaks.forEach((place, index) => {
      const engineAt = engine[index] ?? place;
      if (
        place !== engineAt &&
        !(
          fits(index, edges[index] ?? start, place) &&
          fits(index + 1, place, edges[index + 2] ?? end)
        )
      ) {
        breaks[index] = engineAt;
        changed = true;
      }
    });
  }
  return breaks;
}

// The text of a PDF's document as it lies on its pages, and where on a line each character
// lies: read off the drawn page where its words can be told apart there, and otherwise by the
// widths of the characters. A page's lines are read and cut when its drawing arrives; those
// who draw a page are told when the lines on it are cut anew.
export class PageText {
  readonly onPages = new Map<number, Line[]>();
  readonly ofBlocks = new Map<number, Line[]>();
  // The text of each block as code points, as offsets count them.
  readonly characters: string[][];
  // The formulas of each block, which a selection takes whole or not at all.
  private readonly formulas: Map<number, { start: number; end: number }[]>;
  private readonly cuts = new WeakMap<Line, Cut>();
  // The ink of the lines of the pages drawn now.
  private readonly inks = new Map<Line, Ink>();
  // The drawings of the pages drawn now, and those of them that were read.
  private readonly drawings = new Map<number, HTMLCanvasElement>();
  private readonly readDrawings = new Set<number>();
  // How many times the lines on each page were cut anew, and who is told.
  private readonly versions = new Map<number, number>();
  private readonly listeners = new Set<() => void>();

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

  readonly subscribe = (notify: () => void): (() => void) => {
    this.listeners.add(notify);
    return () => {
      this.listeners.delete(notify);
    };
  };

  // How many times the lines on a page were cut anew: a drawing of its highlights is drawn
  // again when this changes.
  versionOf(page: number): number {
    return this.versions.get(page) ?? 0;
  }

  // Tells where a page is drawn, or that its drawing is gone. A drawing is read when it is
  // needed: its lines read off it, the breaks of the blocks on it set again by their print,
  // and the lines of those blocks cut anew, on this page and on the others they run over.
  drawnOn(page: number, canvas: HTMLCanvasElement | undefined): void {
    if (canvas === undefined) {
      this.drawings.delete(page);
    } else {
      this.drawings.set(page, canvas);
    }
    const lines = this.onPages.get(page) ?? [];
    // A drawing given up takes its reading with it.
    if (lines.some((line) => this.inks.has(line))) {
      for (const line of lines) {
        this.inks.delete(line);
      }
      this.cutAnew(lines);
    }
    this.readDrawings.delete(page);
  }

  // Reads a page's drawing, where it has one not yet read: as it arrives on a page that holds
  // highlights, and on the first press on any other.
  read(page: number): void {
    const canvas = this.drawings.get(page);
    if (canvas === undefined || this.readDrawings.has(page)) {
      return;
    }
    this.readDrawings.add(page);
    const lines = (this.onPages.get(page) ?? []).filter((line) => !line.whole);
    const inks = readInk(
      canvas,
      lines.map((line) => ({ box: line.place, downward: line.downward })),
    );
    lines.forEach((line, index) => {
      const ink = inks[index];
      if (ink === undefined) {
        this.inks.delete(line);
      } else {
        this.inks.set(line, ink);
      }
    });
    this.cutAnew(this.onPages.get(page) ?? []);
  }

  private cutAnew(lines: Line[]): void {
    const touched = new Set<number>();
    for (const blockNumber of new Set(lines.map((line) => line.blockNumber))) {
      const own = this.ofBlocks.get(blockNumber) ?? [];
      this.breakByPrint(own, this.textOf(blockNumber));
      for (const line of own) {
        this.cuts.set(line, this.workedCut(line));
        touched.add(line.place.page);
      }
    }
    for (const touchedPage of touched) {
      this.versions.set(touchedPage, this.versionOf(touchedPage) + 1);
    }
    this.listeners.forEach((notify) => notify());
  }

  private breakByPrint(own: Line[], characters: string[]): void {
    if (own.length < 2 || own.some((line) => line.whole)) {
      return;
    }
    const printed = own.map((line) => {
      const ink = this.inks.get(line);
      return ink === undefined ? undefined : printedWords(ink, line.place.end - line.place.start);
    });
    const breaks = brokenByPrint(own, characters, printed);
    own.forEach((line, index) => {
      line.start = index === 0 ? line.place.start : (breaks[index - 1] ?? line.place.start);
      line.end = index === own.length - 1 ? line.place.end : (breaks[index] ?? line.place.end);
    });
  }

  // How a line is cut: where each character begins along its run, as a part of the run, and
  // where the last ends, and what that was read from.
  cutOf(line: Line): Cut {
    let cut = this.cuts.get(line);
    if (cut === undefined) {
      cut = this.workedCut(line);
      this.cuts.set(line, cut);
    }
    return cut;
  }

  private workedCut(line: Line): Cut {
    const characters = this.textOf(line.blockNumber).slice(line.start, line.end);
    if (characters.some((character) => UNSPACED.test(character))) {
      const edges = characters.map((_, index) => index / characters.length).concat(1);
      return { edges, by: "evenly" };
    }
    const ink = this.inks.get(line);
    const words = wordsOf(characters);
    if (ink === undefined) {
      return { edges: spread(characters, 0, 1), by: "widths" };
    }
    // A piece of a word the engine left at the edge of a line whose print shows one word less
    // is printed on the next line or the one before: it takes no room on this one.
    const text = this.textOf(line.blockNumber);
    const printed = printedWords(ink, characters.length)?.length;
    const leading =
      isWordlike(text[line.start - 1]) && isWordlike(text[line.start]) && words[0]?.start === 0;
    const trailing =
      isWordlike(text[line.end - 1]) &&
      isWordlike(text[line.end]) &&
      words.at(-1)?.end === characters.length;
    let left = 0;
    let right = words.length;
    if (printed !== undefined && words.length > printed) {
      if (leading && right - left > printed) {
        left += 1;
      }
      if (trailing && right - left > printed) {
        right -= 1;
      }
    }
    const printedText = words.slice(left, right);
    const inked = inkedWords(
      ink,
      printedText.map((word) => widthOfText(characters.slice(word.start, word.end))),
      widthOf(" "),
      characters.length,
    );
    if (inked === undefined) {
      return { edges: spread(characters, 0, 1), by: "widths" };
    }
    const first = inked[0]?.[0] ?? 0;
    const last = inked.at(-1)?.[1] ?? ink.run;
    const extents: [number, number][] = [
      ...words.slice(0, left).map((): [number, number] => [first, first]),
      ...inked,
      ...words.slice(right).map((): [number, number] => [last, last]),
    ];
    return { edges: inkedEdges(characters, words, extents, ink.run), by: "ink" };
  }

  // The part of a line's box that holds the characters from start to end of its block's text,
  // cut across the run, down the line where the text runs downwards. A line taken whole is
  // covered whole by any part of it.
  coveredBox(line: Line, start: number, end: number): Box | undefined {
    const { place } = line;
    const from = Math.max(start, line.start);
    const to = Math.min(end, line.end);
    if (from >= to) {
      return undefined;
    }
    if (line.whole || line.end === line.start) {
      return { left: place.left, top: place.top, width: place.width, height: place.height };
    }
    const { edges } = this.cutOf(line);
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
    const { place } = line;
    const count = line.end - line.start;
    if (line.whole || count === 0) {
      return { blockNumber: line.blockNumber, start: line.start, end: line.end };
    }
    const along = line.downward
      ? (point.y - place.top) / place.height
      : (point.x - place.left) / place.width;
    const { edges } = this.cutOf(line);
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
