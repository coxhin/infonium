import { FORMULA_ATTRIBUTE } from "./formula";
import "./glowing.css";

// How a passage pointed at is shown where it stands: a glow is drawn over it in the order it is
// read, as a highlighter is drawn along a line, stays for a moment and fades away, so that the
// reader's eye is led to the passage and along it, and the page is left as it was. Words glow
// by the browser's highlights: here in the reader's window, and in the frame of a kept page
// (pagewriter.ts) through a glow of its own. Boxes on a PDF's page glow by elements that are
// drawn and fade by themselves (pageglow.tsx), over the same times and curves.

// How long the glow takes to be drawn over the passage, how long from its start it stays whole,
// and how long it then takes to fade, in milliseconds.
export const GLOW_DRAWING = 450;
export const GLOW_WHOLE = 1150;
export const GLOW_FADE = 1250;
export const GLOW_LASTS = GLOW_WHOLE + GLOW_FADE;

// The fade begins slowly and ends slowly, so that the glow neither drops at once nor lingers as
// a faint stain: the curve strengthAt follows, as CSS names it.
export const FADE_EASING = "cubic-bezier(0.37, 0, 0.63, 1)";

// A reader who asks for less motion sees the glow come up whole at once; it still fades, as a
// change of colour that moves nothing.
const STILL = matchMedia("(prefers-reduced-motion: reduce)");

// How much of the passage the glow has been drawn over, at a time since it began: quickly at
// first and slowing to its end, as a stroke of the hand.
function drawnBy(elapsed: number): number {
  const along = Math.min(Math.max(elapsed / GLOW_DRAWING, 0), 1);
  return 1 - (1 - along) ** 3;
}

// When the glow reaches a part of the passage, in milliseconds since it began.
export function drawingReaches(part: number): number {
  return GLOW_DRAWING * (1 - Math.cbrt(1 - Math.min(Math.max(part, 0), 1)));
}

// How strong the glow is at a time since it began, from whole to none.
function strengthAt(elapsed: number): number {
  const fading = Math.min(Math.max((elapsed - GLOW_WHOLE) / GLOW_FADE, 0), 1);
  return (1 + Math.cos(Math.PI * fading)) / 2;
}

// A highlight's colour cannot move by itself, so a glow is drawn in one of these highlights at
// each moment, from the faintest to the whole: steps so close that the fade looks smooth.
const LEVELS = 24;

function levelName(prefix: string, level: number): string {
  return `${prefix}-${level}`;
}

// The rules that draw a glow's highlights, named after the prefix, with the declarations of
// each for its strength, from more than none to whole.
export function levelRules(prefix: string, declarations: (strength: number) => string): string {
  return Array.from({ length: LEVELS }, (_, index) => {
    const level = index + 1;
    return `::highlight(${levelName(prefix, level)}) { ${declarations(level / LEVELS)} }`;
  }).join("\n");
}

// The glow lies above the reader's highlights (textmarks.ts, framehighlights.ts), so that it
// stays the stronger where the two lie over each other.
const GLOW_PRIORITY = 10;

// One stretch of a passage's text as the glow is drawn over it: the text node, or a formula
// whole, the offsets in a text node it runs between, how wide it is drawn, and how much of the
// passage lies before it.
type Piece = {
  node: Node;
  start: number;
  end: number;
  width: number;
  before: number;
};

// One range of a passage, with its pieces.
type Stroke = { range: Range; pieces: Piece[] };

// Nodes are told by their type, not their class, as those of a kept page's frame are made by
// the frame's own classes.
function isText(node: Node): node is Text {
  return node.nodeType === Node.TEXT_NODE;
}

function isFormula(node: Node): node is Element {
  return node.nodeType === Node.ELEMENT_NODE && (node as Element).hasAttribute(FORMULA_ATTRIBUTE);
}

// The node after the walker's current one and all it holds, or none at the end of the walk.
function nextOutside(walker: TreeWalker): Node | null {
  while (walker.parentNode() !== null) {
    const after = walker.nextSibling();
    if (after !== null) {
      return after;
    }
  }
  return null;
}

// The text nodes and formulas a range runs through, in their order, with the part of each it
// holds. A formula is drawn whole, as the reader's text takes one whole (textpieces.ts).
function piecesOf(range: Range): Omit<Piece, "width" | "before">[] {
  const root = range.commonAncestorContainer;
  if (isText(root)) {
    return [{ node: root, start: range.startOffset, end: range.endOffset }];
  }
  const pieces: Omit<Piece, "width" | "before">[] = [];
  const walker = (root.ownerDocument ?? document).createTreeWalker(
    root,
    NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT,
  );
  let node = walker.nextNode();
  while (node !== null) {
    const formula = isFormula(node);
    if (formula && range.intersectsNode(node)) {
      pieces.push({ node, start: 0, end: 1 });
    }
    // What lies outside the range, and what a formula holds, is passed over whole.
    if (formula || !range.intersectsNode(node)) {
      node = walker.nextSibling() ?? nextOutside(walker);
      continue;
    }
    if (isText(node)) {
      const start = node === range.startContainer ? range.startOffset : 0;
      const end = node === range.endContainer ? range.endOffset : node.length;
      if (start < end) {
        pieces.push({ node, start, end });
      }
    }
    node = walker.nextNode();
  }
  return pieces;
}

// How wide a piece is drawn, over all the lines it runs over.
function widthOf({ node, start, end }: Omit<Piece, "width" | "before">): number {
  if (isFormula(node)) {
    return node.getBoundingClientRect().width;
  }
  const range = (node.ownerDocument ?? document).createRange();
  range.setStart(node, start);
  range.setEnd(node, end);
  return [...range.getClientRects()].reduce((sum, rectangle) => sum + rectangle.width, 0);
}

// The strokes of a passage: the glow is drawn along them at an even pace over the page, so each
// piece takes the share of the drawing its width does. Where nothing is laid out, each
// character takes an even share instead.
function strokesOf(ranges: Range[]): Stroke[] {
  const found = ranges.map((range) => ({ range, pieces: piecesOf(range) }));
  const all = found.flatMap((stroke) => stroke.pieces);
  const widths = all.map(widthOf);
  const laidOut = widths.some((width) => width > 0);
  let before = 0;
  let index = 0;
  return found.map(({ range, pieces }) => ({
    range,
    pieces: pieces.map((piece) => {
      const width = laidOut ? (widths[index] ?? 0) : piece.end - piece.start;
      index += 1;
      const placed = { ...piece, width, before };
      before += width;
      return placed;
    }),
  }));
}

// The part of each stroke the glow has been drawn over, when it has come as far as reach, in
// the measure of the pieces' widths.
function drawnParts(strokes: Stroke[], reach: number): Range[] {
  return strokes.flatMap(({ range, pieces }) => {
    const first = pieces[0];
    const last = pieces.at(-1);
    if (first === undefined || last === undefined || reach >= last.before + last.width) {
      return [range];
    }
    if (reach <= first.before) {
      return [];
    }
    const piece = pieces.find((each) => reach < each.before + each.width) ?? last;
    const part = range.cloneRange();
    const along = piece.width > 0 ? (reach - piece.before) / piece.width : 1;
    if (isFormula(piece.node)) {
      if (along < 1 / 2) {
        part.setEndBefore(piece.node);
      } else {
        part.setEndAfter(piece.node);
      }
    } else {
      part.setEnd(piece.node, piece.start + Math.round(along * (piece.end - piece.start)));
    }
    return part.collapsed ? [] : [part];
  });
}

// The browser's highlights of one window, in which a glow is drawn.
export type HighlightsOf = {
  registry: HighlightRegistry;
  Highlight: typeof Highlight;
};

// A glow of one window: one passage at a time, drawn in the highlights its rules name after a
// prefix (levelRules), at every frame the window draws from its start until it has faded.
export class Glow {
  private readonly highlights: HighlightsOf;
  private highlight: Highlight | undefined;
  private strokes: Stroke[] = [];
  private prefix = "";
  private began = 0;
  private atOnce = false;
  private drawn = 0;
  private level = 0;
  private frame: number | undefined;

  constructor(highlights: HighlightsOf) {
    this.highlights = highlights;
  }

  // Draws a glow over the ranges, in the highlights named after the prefix, and ends the glow
  // before it.
  begin(ranges: Range[], prefix: string): void {
    this.end();
    if (ranges.length === 0) {
      return;
    }
    const highlight = new this.highlights.Highlight();
    highlight.priority = GLOW_PRIORITY;
    this.highlight = highlight;
    this.prefix = prefix;
    this.strokes = strokesOf(ranges);
    this.began = performance.now();
    this.atOnce = STILL.matches;
    this.drawn = -1;
    this.frame = requestAnimationFrame(this.step);
  }

  // Puts the glow on show over these ranges instead, as far as it has been drawn and has
  // faded: the words it lay over drawn anew, as a translation's text is when its links arrive,
  // or more of them come on show. A glow that has faded is not lit again.
  relight(ranges: Range[]): void {
    if (this.highlight === undefined) {
      return;
    }
    this.strokes = strokesOf(ranges);
    this.drawn = -1;
    this.draw(performance.now() - this.began);
  }

  // Ends the glow on show, if one is.
  end(): void {
    if (this.frame !== undefined) {
      cancelAnimationFrame(this.frame);
      this.frame = undefined;
    }
    if (this.level > 0) {
      this.highlights.registry.delete(levelName(this.prefix, this.level));
    }
    this.level = 0;
    this.highlight = undefined;
    this.strokes = [];
  }

  private readonly step = (now: number): void => {
    const elapsed = now - this.began;
    if (elapsed >= GLOW_LASTS) {
      this.frame = undefined;
      this.end();
      return;
    }
    this.draw(elapsed);
    this.frame = requestAnimationFrame(this.step);
  };

  // Draws the glow as it is at a time since it began: over the part of the passage it has come
  // to, in the highlight of its strength.
  private draw(elapsed: number): void {
    const highlight = this.highlight;
    if (highlight === undefined) {
      return;
    }
    const drawn = this.atOnce ? 1 : drawnBy(elapsed);
    if (drawn !== this.drawn) {
      this.drawn = drawn;
      const last = this.strokes.at(-1)?.pieces.at(-1);
      const length = last === undefined ? 0 : last.before + last.width;
      highlight.clear();
      for (const range of drawnParts(this.strokes, drawn * length)) {
        highlight.add(range);
      }
    }
    const level = Math.round(strengthAt(elapsed) * LEVELS);
    if (level !== this.level) {
      const registry = this.highlights.registry;
      if (this.level > 0) {
        registry.delete(levelName(this.prefix, this.level));
      }
      if (level > 0) {
        registry.set(levelName(this.prefix, level), highlight);
      }
      this.level = level;
    }
  }
}

// The reader's own glow, drawn in its window over the colour glowing.css gives.
const GLOW_PREFIX = "anchor";
const readerGlow = new Glow({ registry: CSS.highlights, Highlight });
const rules = new CSSStyleSheet();
rules.replaceSync(
  levelRules(GLOW_PREFIX, (strength) => {
    const share = `${(strength * 100).toFixed(1)}%`;
    return `background-color: color-mix(in srgb, var(--glow) ${share}, transparent); color: inherit;`;
  }),
);
document.adoptedStyleSheets = [...document.adoptedStyleSheets, rules];

// Ends the glow on show, if one is.
export function endGlow(): void {
  readerGlow.end();
}

// Makes the words of the ranges glow, and ends the glow before it.
export function glowOver(ranges: Range[]): void {
  readerGlow.begin(ranges, GLOW_PREFIX);
}

// Puts the glow on show over these ranges instead, as far as it has come.
export function relight(ranges: Range[]): void {
  readerGlow.relight(ranges);
}
