import type { Block } from "./client";
import type { HolderPlace } from "./pagewriter";

// The words of an element of the framed page that stands for a block, or for a cell of a table
// block, and their correspondence with the text it stands for: which character of the text
// each character of the element is, and back. The two hold the same words, but not character
// for character: white space differs as the page's source had it, and the page may hold words
// the extractor left out. A passage pointed at, a highlight drawn and a selection handed over
// all go through this one correspondence, so that the same range lands on the same words.

// What the extractor leaves out of a block's text that the page holds: words struck out, code
// for the browser, what the page's own markup hides, and the translation infonium writes after
// a block's source.
const UNREAD_ELEMENTS = "del, s, strike, script, style, noscript, template, infonium-translation";
const HIDDEN_BY_STYLE = /display\s*:\s*none|visibility\s*:\s*hidden/i;
const STYLE_ATTRIBUTE = "style";
// The quotation marks of an inline quotation, which the browser draws without text and the
// extractor writes into the text as these.
const OPENING_QUOTATION_MARK = "“";
const CLOSING_QUOTATION_MARK = "”";
const INLINE_QUOTATION = "q";
const SPACE = /\s/u;
const FORMAT_CHARACTER = /\p{Cf}/u;
const COMPARISON_FORM = "NFKD";
// How typesetting may write the same character, as citations compare text.
const PLAIN_PUNCTUATION: ReadonlyMap<string, string> = new Map([
  ["“", '"'],
  ["”", '"'],
  ["„", '"'],
  ["«", '"'],
  ["»", '"'],
  ["‘", "'"],
  ["’", "'"],
  ["‚", "'"],
  ["‐", "-"],
  ["‑", "-"],
  ["‒", "-"],
  ["–", "-"],
  ["—", "-"],
  ["―", "-"],
]);
// Where the two texts part, this many characters of one, found again in the other, say where
// they meet again.
const REJOINING_LENGTH = 8;
// How far ahead a place where the two texts meet again is looked for, in characters.
const REJOINING_REACH = 4000;
// A character of the block's text that is white space, which no character of the page is.
const UNCOMPARED = -2;
// A character of either text that the other does not hold.
const UNMATCHED = -1;
// An element that clips what it holds to this many pixels or fewer across shows none of it, as
// the text a page writes for screen readers alone.
const CLIPPED_SIZE = 1;
const SHOWN_OVERFLOW = "visible";

const forms = new Map<string, string>();

// A character as text is compared: nothing for white space and invisible format characters,
// otherwise its decomposed form in lower case, with its quotation marks and dashes plain.
function comparableForm(character: string): string {
  const known = forms.get(character);
  if (known !== undefined) {
    return known;
  }
  const form =
    SPACE.test(character) || FORMAT_CHARACTER.test(character)
      ? ""
      : [...character.normalize(COMPARISON_FORM)]
          .map((part) => PLAIN_PUNCTUATION.get(part) ?? part)
          .join("")
          .toLowerCase();
  forms.set(character, form);
  return form;
}

// Whether an element draws what it holds where the reader sees it: not hidden, see-through or
// clipped to nothing, itself or by any element around it up to the one that stands for the
// block. Each element is judged once.
function drawsWithin(element: Element, holder: Element, known: Map<Element, boolean>): boolean {
  const judged = known.get(element);
  if (judged !== undefined) {
    return judged;
  }
  const view = element.ownerDocument.defaultView;
  const style = view?.getComputedStyle(element);
  const box = element.getBoundingClientRect();
  const clipped =
    style !== undefined &&
    (style.overflowX !== SHOWN_OVERFLOW || style.overflowY !== SHOWN_OVERFLOW) &&
    (box.width <= CLIPPED_SIZE || box.height <= CLIPPED_SIZE);
  const draws =
    element.checkVisibility({ visibilityProperty: true, opacityProperty: true }) &&
    !clipped &&
    (element === holder ||
      element.parentElement === null ||
      drawsWithin(element.parentElement, holder, known));
  known.set(element, draws);
  return draws;
}

// The characters of an element as the extractor reads them, in the page's order: each a place
// in the page where it begins, its length there, and whether the reader sees it. A quotation
// mark of an inline quotation has no length, and stands at the edge of its element.
type ShownText = {
  characters: string[];
  nodes: Node[];
  offsets: number[];
  lengths: number[];
  drawn: boolean[];
};

function shownText(element: Element): ShownText {
  const shown: ShownText = { characters: [], nodes: [], offsets: [], lengths: [], drawn: [] };
  const known = new Map<Element, boolean>();
  function add(character: string, node: Node, offset: number, length: number, drawn: boolean) {
    shown.characters.push(character);
    shown.nodes.push(node);
    shown.offsets.push(offset);
    shown.lengths.push(length);
    shown.drawn.push(drawn);
  }
  function walk(parent: Element) {
    for (const child of parent.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        const drawn = drawsWithin(parent, element, known);
        let offset = 0;
        for (const character of child.textContent ?? "") {
          add(character, child, offset, character.length, drawn);
          offset += character.length;
        }
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        const inner = child as Element;
        if (
          inner.matches(UNREAD_ELEMENTS) ||
          HIDDEN_BY_STYLE.test(inner.getAttribute(STYLE_ATTRIBUTE) ?? "")
        ) {
          continue;
        }
        const quotation = inner.localName === INLINE_QUOTATION;
        const drawn = quotation && drawsWithin(inner, element, known);
        if (quotation) {
          add(OPENING_QUOTATION_MARK, inner, 0, 0, drawn);
        }
        walk(inner);
        if (quotation) {
          add(CLOSING_QUOTATION_MARK, inner, inner.childNodes.length, 0, drawn);
        }
      }
    }
  }
  walk(element);
  return shown;
}

// Some of a list of characters in their compared form, written one after another, and for each
// unit of that form the place in the list of the character it comes from.
function compared(characters: string[], chosen: number[]): { text: string; origins: number[] } {
  const units: string[] = [];
  const origins: number[] = [];
  for (const index of chosen) {
    for (const unit of comparableForm(characters[index] ?? "")) {
      units.push(unit);
      origins.push(index);
    }
  }
  return { text: units.join(""), origins };
}

// How many units are passed over to reach a place where the texts meet again, if it is in reach.
function skipTo(found: number, from: number): number {
  return found === -1 || found - from > REJOINING_REACH ? Infinity : found - from;
}

// Pairs the units of two compared texts that hold the same words, in order: equal units pair
// as long as they follow each other; where they part, the text that holds more at that place
// is passed over to where a stretch of the other is found again, the nearer of the two ways,
// or one unit of each where neither meets the other again.
function pairedUnits(page: string, block: string): number[] {
  const pairs = new Array<number>(page.length).fill(UNMATCHED);
  let inPage = 0;
  let inBlock = 0;
  while (inPage < page.length && inBlock < block.length) {
    if (page[inPage] === block[inBlock]) {
      pairs[inPage] = inBlock;
      inPage += 1;
      inBlock += 1;
      continue;
    }
    const blockAhead = block.slice(inBlock, inBlock + REJOINING_LENGTH);
    const pageAhead = page.slice(inPage, inPage + REJOINING_LENGTH);
    const inPageAgain = page.indexOf(blockAhead, inPage);
    const inBlockAgain = block.indexOf(pageAhead, inBlock);
    const pageSkip = skipTo(inPageAgain, inPage);
    const blockSkip = skipTo(inBlockAgain, inBlock);
    if (pageSkip === Infinity && blockSkip === Infinity) {
      inPage += 1;
      inBlock += 1;
    } else if (pageSkip <= blockSkip) {
      inPage = inPageAgain;
    } else {
      inBlock = inBlockAgain;
    }
  }
  return pairs;
}

// For each character of the page, the code point of the text it is, pairing the text with some
// of the page's characters; and how many units of the text found a character.
function pairing(
  shown: ShownText,
  chosen: number[],
  block: { text: string; origins: number[] },
): { inText: number[]; matched: number } {
  const page = compared(shown.characters, chosen);
  const pairs = pairedUnits(page.text, block.text);
  const inText = new Array<number>(shown.characters.length).fill(UNMATCHED);
  let matched = 0;
  pairs.forEach((unit, pageUnit) => {
    const character = page.origins[pageUnit];
    const codePoint = block.origins[unit];
    if (unit === UNMATCHED || character === undefined || codePoint === undefined) {
      return;
    }
    matched += 1;
    if (inText[character] === UNMATCHED) {
      inText[character] = codePoint;
    }
  });
  return { inText, matched };
}

// A stretch of a text, from a character's offset to the offset after the last, in code points.
export type Span = { start: number; end: number };

function isWords(character: string): boolean {
  return character.trim() !== "";
}

// The stretch of a text but for white space at either end; none where it is all white space.
export function trimmedSpan(text: string): Span | undefined {
  const characters = [...text];
  const start = characters.findIndex(isWords);
  if (start === -1) {
    return undefined;
  }
  let end = characters.length;
  while (end > start && !isWords(characters[end - 1] ?? "")) {
    end -= 1;
  }
  return { start, end };
}

// The text an element stands for, and where that text begins in its block's text, counted in
// code points: a block's whole text, or a cell's.
export type HeldText = { text: string; offset: number; length: number };

const CELL_KEY_SEPARATOR = "/";
const cellTextsOf = new WeakMap<Block, Map<string, HeldText>>();

function codePointLength(text: string): number {
  return [...text].length;
}

// A table's cells, each with its place in the table's text, as the cell attribute names it.
function cellsOfTable(block: Block & { kind: "table" }): Map<string, HeldText> {
  const known = cellTextsOf.get(block);
  if (known !== undefined) {
    return known;
  }
  const cells = new Map<string, HeldText>();
  const cellSeparator = codePointLength(block.cell_separator);
  const rowSeparator = codePointLength(block.row_separator);
  let offset = 0;
  block.text.split(block.row_separator).forEach((row, rowNumber) => {
    row.split(block.cell_separator).forEach((text, cellNumber) => {
      const length = codePointLength(text);
      cells.set(`${rowNumber}${CELL_KEY_SEPARATOR}${cellNumber}`, { text, offset, length });
      offset += length + cellSeparator;
    });
    offset += rowSeparator - cellSeparator;
  });
  cellTextsOf.set(block, cells);
  return cells;
}

export function heldTextOf(block: Block, place: HolderPlace): HeldText | undefined {
  if (place.cell === undefined) {
    return block.kind === "table"
      ? undefined
      : { text: block.text, offset: 0, length: codePointLength(block.text) };
  }
  return block.kind === "table"
    ? cellsOfTable(block).get(`${place.cell.row}${CELL_KEY_SEPARATOR}${place.cell.cell}`)
    : undefined;
}

// The correspondence between an element of the page and the text it stands for. Where the page
// holds a stretch the text lacks, the characters the reader does not see are passed over before
// those the reader sees: the text is paired with the seen characters alone, unless pairing it
// with all of them finds more of it, as where the extractor read words the page hides.
export class FrameWords {
  readonly element: Element;
  readonly text: string;
  readonly shown: ShownText;
  // For each character of the element, the code point of the text it is.
  readonly inText: number[];
  // For each code point of the text, the character of the element it is.
  readonly inPage: number[];

  constructor(element: Element, text: string) {
    this.element = element;
    this.text = text;
    this.shown = shownText(element);
    const codePoints = [...text];
    const block = compared(
      codePoints,
      codePoints.map((_, index) => index),
    );
    const every = this.shown.characters.map((_, index) => index);
    const seen = every.filter((index) => this.shown.drawn[index] === true);
    const whole = pairing(this.shown, every, block);
    const chosen =
      seen.length === every.length ? whole : pairing(this.shown, seen, block);
    this.inText = (chosen.matched >= whole.matched ? chosen : whole).inText;
    this.inPage = codePoints.map((character) =>
      comparableForm(character) === "" ? UNCOMPARED : UNMATCHED,
    );
    this.inText.forEach((codePoint, character) => {
      if (codePoint >= 0 && this.inPage[codePoint] === UNMATCHED) {
        this.inPage[codePoint] = character;
      }
    });
  }

  // The words of a stretch of the text in the element: from where its first character that is
  // not white space begins to where its last ends. None where the element does not hold either
  // of the two, or where the reader sees none of the words between them.
  wordsOf(span: Span): Range | null {
    let first = span.start;
    while (first < span.end && this.inPage[first] === UNCOMPARED) {
      first += 1;
    }
    let last = span.end - 1;
    while (last >= first && this.inPage[last] === UNCOMPARED) {
      last -= 1;
    }
    const begins = this.inPage[first] ?? UNMATCHED;
    const ends = this.inPage[last] ?? UNMATCHED;
    if (last < first || begins < 0 || ends < begins) {
      return null;
    }
    if (!this.shown.drawn.slice(begins, ends + 1).some((drawn) => drawn)) {
      return null;
    }
    const range = this.element.ownerDocument.createRange();
    range.setStart(this.nodeAt(begins), this.offsetAt(begins));
    range.setEnd(this.nodeAt(ends), this.offsetAt(ends) + (this.shown.lengths[ends] ?? 0));
    return range;
  }

  // The stretch of the text that the characters of the element a selection covers are: from
  // the first of them the text holds to the last. None where it holds none.
  spanOf(selection: Range): Span | null {
    const count = this.shown.characters.length;
    // The first character that begins inside the selection, and the last that ends inside it.
    const first = this.firstWhere(
      (index) => selection.comparePoint(this.nodeAt(index), this.offsetAt(index)) >= 0,
    );
    const last =
      this.firstWhere(
        (index) =>
          selection.comparePoint(
            this.nodeAt(index),
            this.offsetAt(index) + (this.shown.lengths[index] ?? 0),
          ) > 0,
      ) - 1;
    let start: number | undefined;
    let end: number | undefined;
    for (let index = first; index <= last && index < count; index += 1) {
      const codePoint = this.inText[index] ?? UNMATCHED;
      if (codePoint >= 0) {
        start = codePoint;
        break;
      }
    }
    for (let index = last; index >= first && index >= 0; index -= 1) {
      const codePoint = this.inText[index] ?? UNMATCHED;
      if (codePoint >= 0) {
        end = codePoint + 1;
        break;
      }
    }
    return start === undefined || end === undefined || end <= start ? null : { start, end };
  }

  private nodeAt(index: number): Node {
    return this.shown.nodes[index] ?? this.element;
  }

  private offsetAt(index: number): number {
    return this.shown.offsets[index] ?? 0;
  }

  // The first character for which a test holds that, once it holds, holds for every character
  // after; the count of characters where it holds for none.
  private firstWhere(test: (index: number) => boolean): number {
    let low = 0;
    let high = this.shown.characters.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (test(middle)) {
        high = middle;
      } else {
        low = middle + 1;
      }
    }
    return low;
  }
}
