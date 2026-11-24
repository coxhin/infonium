import type { Anchor, Anchors, Block } from "./client";
import type { Marked } from "./marking";
import type { Span } from "./pagetext";
import {
  linkOf,
  selectorOf,
  TRANSLATION_ELEMENT,
  type FramedPage,
  type HolderPlace,
  type Holders,
} from "./pagewriter";

// The reader's highlights in the page a web article was read from, and the passages the reader
// selects there.

// A highlight is drawn in colours of its own, the same on any page, light or dark: its words
// dark on green, and those of the one open in the notebook on a deeper green; one with a
// comment is underlined. Where its words are not drawn, its element is tinted whole, more
// quietly, in a green that lets the page's own colours through. Each look is a highlight of
// its own in the page's registry, so that where two overlap the one that says more is drawn:
// the open one over the others, exact words over a tint, and the passage pointed at, whose
// priority is higher still, over them all.
const HIGHLIGHT_PREFIX = "infonium-marked";
type Extent = "words" | "whole";
type Look = { extent: Extent; opened: boolean; commented: boolean };
const LOOKS: Look[] = (["words", "whole"] as const).flatMap((extent) =>
  [false, true].flatMap((opened) =>
    [false, true].map((commented) => ({ extent, opened, commented })),
  ),
);
const WORDS_INK = "#1f2328";
const WORDS_BACKGROUND = "#c3ecbf";
const OPENED_WORDS_BACKGROUND = "#7fd486";
const WORDS_UNDERLINE = "#1b5e20";
const WHOLE_BACKGROUND = "rgb(76 175 80 / 0.2)";
const OPENED_WHOLE_BACKGROUND = "rgb(76 175 80 / 0.42)";
const WHOLE_UNDERLINE = "#43a047";
const UNDERLINE_THICKNESS = "2px";
const IMPORTANT = " !important";

function nameOf({ extent, opened, commented }: Look): string {
  const open = opened ? "opened" : "kept";
  const noted = commented ? "commented" : "bare";
  return `${HIGHLIGHT_PREFIX}-${extent}-${open}-${noted}`;
}

// Exact words over a tint, the open highlight over the others, a comment over none: each
// weighs more than both after it together.
function priorityOf({ extent, opened, commented }: Look): number {
  return (extent === "words" ? 4 : 0) + (opened ? 2 : 0) + (commented ? 1 : 0);
}

// How a highlight looks, as declarations that a highlight of the registry and a rule of the
// page's stylesheet both take; in a rule they stand above the page's own.
function declarationsOf({ extent, opened, commented }: Look, importance: string): string {
  const words = extent === "words";
  const background = words
    ? opened
      ? OPENED_WORDS_BACKGROUND
      : WORDS_BACKGROUND
    : opened
      ? OPENED_WHOLE_BACKGROUND
      : WHOLE_BACKGROUND;
  const underline = words ? `solid ${WORDS_UNDERLINE}` : `dotted ${WHOLE_UNDERLINE}`;
  return [
    `background-color: ${background}${importance};`,
    ...(words ? [`color: ${WORDS_INK}${importance};`] : []),
    ...(commented
      ? [`text-decoration: underline ${underline} ${UNDERLINE_THICKNESS}${importance};`]
      : []),
  ].join(" ");
}

const MARKS_STYLE = LOOKS.map(
  (look) => `::highlight(${nameOf(look)}) { ${declarationsOf(look, "")} }`,
).join("\n");

// A highlight as drawn: where in the page, in which element, and where its passage begins in
// the document, by which the one that begins last is told among several under a press.
type Drawn = { id: string; holder: Element; range: Range; begins: Anchor };

// The text an element stands for, and where that text begins in its block's text, counted in
// code points: a block's whole text, or a cell's.
type HeldText = { text: string; offset: number; length: number };

const SEPARATE = "/";
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
      cells.set(`${rowNumber}${SEPARATE}${cellNumber}`, { text, offset, length });
      offset += length + cellSeparator;
    });
    offset += rowSeparator - cellSeparator;
  });
  cellTextsOf.set(block, cells);
  return cells;
}

function heldTextOf(block: Block, place: HolderPlace): HeldText | undefined {
  if (place.cell === undefined) {
    return block.kind === "table"
      ? undefined
      : { text: block.text, offset: 0, length: codePointLength(block.text) };
  }
  return block.kind === "table"
    ? cellsOfTable(block).get(`${place.cell.row}${SEPARATE}${place.cell.cell}`)
    : undefined;
}

function isWords(character: string): boolean {
  return character.trim() !== "";
}

// The stretch of a text but for white space at either end; none where it is all white space.
function trimmedSpan(text: string): Span | undefined {
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

function contentsOf(element: Element): Range {
  const range = element.ownerDocument.createRange();
  range.selectNodeContents(element);
  return range;
}

// The part of a selection that lies in an element.
function partIn(selection: Range, element: Element): Range {
  const part = contentsOf(element);
  if (selection.compareBoundaryPoints(Range.START_TO_START, part) > 0) {
    part.setStart(selection.startContainer, selection.startOffset);
  }
  if (selection.compareBoundaryPoints(Range.END_TO_END, part) < 0) {
    part.setEnd(selection.endContainer, selection.endOffset);
  }
  return part;
}

function holdsWords(range: Range): boolean {
  return range.toString().trim() !== "";
}

function isLater(anchor: Anchor, than: Anchor): boolean {
  return (
    anchor.block_number > than.block_number ||
    (anchor.block_number === than.block_number && anchor.start > than.start)
  );
}

function contains(rectangle: DOMRect, x: number, y: number): boolean {
  return (
    rectangle.left <= x && x <= rectangle.right && rectangle.top <= y && y <= rectangle.bottom
  );
}

// The elements that stand for blocks or cells that a selection reaches into, in the page's
// order.
function holdersIn(holders: Holders, selection: Range): Element[] {
  const common = selection.commonAncestorContainer;
  const root = common.nodeType === Node.ELEMENT_NODE ? (common as Element) : common.parentElement;
  if (root === null) {
    return [];
  }
  const around = root.closest(holders.selector);
  if (around !== null) {
    return [around];
  }
  return [...root.querySelectorAll(holders.selector)].filter((element) =>
    selection.intersectsNode(element),
  );
}

// The reader's highlights as drawn in one page, and what the reader selects in it.
export class PageMarks {
  readonly framed: FramedPage;
  // The rules that tint elements whole where the page's browser has no highlight registry.
  readonly wholeRules: CSSStyleSheet;
  drawn: Drawn[] = [];

  constructor(framed: FramedPage) {
    this.framed = framed;
    const view = framed.view;
    const style = new view.CSSStyleSheet();
    style.replaceSync(MARKS_STYLE);
    this.wholeRules = new view.CSSStyleSheet();
    // Before infonium's other sheets, so that the outline of the passage pointed at, which
    // the last of them draws, is drawn over a tint.
    const page = framed.document;
    page.adoptedStyleSheets = [style, this.wholeRules, ...page.adoptedStyleSheets];
  }

  // Draws the highlights, each anchor in the elements of its block, and returns how many of
  // them run through a block that has no element in the page.
  draw(holders: Holders, blocks: Block[], marked: Marked[], opened: string | undefined): number {
    const framed = this.framed;
    const registry = framed.marksWords();
    const ranges = new Map<string, Range[]>();
    const tinted = new Map<string, { look: Look; selectors: string[] }>();
    const drawn: Drawn[] = [];
    let undrawn = 0;
    for (const highlight of marked) {
      let whole = true;
      for (const anchor of highlight.anchors) {
        const block = blocks[anchor.block_number];
        const elements = holders.ofBlock.get(anchor.block_number) ?? [];
        if (block === undefined || elements.length === 0) {
          whole = false;
          continue;
        }
        for (const element of elements) {
          const place = holders.places.get(element);
          const held = place === undefined ? undefined : heldTextOf(block, place);
          if (held === undefined || !element.isConnected) {
            continue;
          }
          const span = {
            start: Math.max(anchor.start - held.offset, 0),
            end: Math.min(anchor.end - held.offset, held.length),
          };
          if (span.end <= span.start) {
            continue;
          }
          const words =
            registry && framed.shownIn(element) !== "translation"
              ? framed.textOf(element, held.text).wordsOf(span)
              : null;
          const look: Look = {
            extent: words === null ? "whole" : "words",
            opened: highlight.id === opened,
            commented: highlight.commented,
          };
          const range = words ?? contentsOf(element);
          const name = nameOf(look);
          if (registry) {
            ranges.set(name, [...(ranges.get(name) ?? []), range]);
          } else {
            const known = tinted.get(name) ?? { look, selectors: [] };
            known.selectors.push(selectorOf(holders.pageBlocks, element));
            tinted.set(name, known);
          }
          drawn.push({ id: highlight.id, holder: element, range, begins: highlight.anchors[0] });
        }
      }
      if (!whole) {
        undrawn += 1;
      }
    }
    if (registry) {
      const view = framed.view;
      for (const look of LOOKS) {
        const highlight = new view.Highlight(...(ranges.get(nameOf(look)) ?? []));
        highlight.priority = priorityOf(look);
        view.CSS.highlights.set(nameOf(look), highlight);
      }
    }
    this.wholeRules.replaceSync(
      [...tinted.values()]
        .map(
          ({ look, selectors }) =>
            `${selectors.join(", ")} { ${declarationsOf(look, IMPORTANT)} }`,
        )
        .join("\n"),
    );
    this.drawn = drawn;
    return undrawn;
  }

  erase(): void {
    if (this.framed.marksWords()) {
      for (const look of LOOKS) {
        this.framed.view.CSS.highlights.delete(nameOf(look));
      }
    }
    this.wholeRules.replaceSync("");
    this.drawn = [];
  }

  // The highlight a press of the page fell on, where it selects nothing and opens no link: of
  // those drawn under it, the one whose passage begins last.
  pressed(holders: Holders, event: MouseEvent): string | undefined {
    if (linkOf(event) !== null || this.framed.document.getSelection()?.isCollapsed === false) {
      return undefined;
    }
    const target = event.target;
    const holder =
      target instanceof this.framed.view.Element ? target.closest(holders.selector) : null;
    let found: Drawn | undefined;
    for (const drawn of this.drawn) {
      if (
        drawn.holder === holder &&
        (found === undefined || isLater(drawn.begins, found.begins)) &&
        [...drawn.range.getClientRects()].some((rectangle) =>
          contains(rectangle, event.clientX, event.clientY),
        )
      ) {
        found = drawn;
      }
    }
    return found?.id;
  }

  // The passage a selection is in the document: for each block whose elements it covers words
  // of, the stretch of the block's text those words are, in the order of the document. None
  // where it covers no words of any.
  selected(
    holders: Holders,
    blocks: Block[],
    documentId: string,
    selection: Range,
  ): Anchors | undefined {
    const spans = new Map<number, Span>();
    for (const element of holdersIn(holders, selection)) {
      const place = holders.places.get(element);
      const block = place === undefined ? undefined : blocks[place.number];
      const held =
        block === undefined || place === undefined ? undefined : heldTextOf(block, place);
      const span = held === undefined ? undefined : this.selectedIn(element, held.text, selection);
      if (place === undefined || held === undefined || span === undefined) {
        continue;
      }
      const known = spans.get(place.number);
      const start = span.start + held.offset;
      const end = span.end + held.offset;
      spans.set(place.number, {
        start: Math.min(start, known?.start ?? start),
        end: Math.max(end, known?.end ?? end),
      });
    }
    const anchors = [...spans.entries()]
      .sort(([first], [second]) => first - second)
      .map(([number, { start, end }]) => ({
        document_id: documentId,
        block_number: number,
        start,
        end,
        quote: [...(blocks[number]?.text ?? "")].slice(start, end).join(""),
      }));
    const [first, ...rest] = anchors;
    return first === undefined ? undefined : [first, ...rest];
  }

  // The stretch of an element's text that a selection covers words of. Where the element shows
  // the text's translation in its place, or the selection covers words of the translation
  // shown after the text and none of the text, it is the whole text.
  selectedIn(element: Element, text: string, selection: Range): Span | undefined {
    const part = partIn(selection, element);
    if (!holdsWords(part)) {
      return undefined;
    }
    const shown = this.framed.shownIn(element);
    if (shown === "translation") {
      return trimmedSpan(text);
    }
    const span = this.framed.textOf(element, text).spanOf(part);
    if (span !== null) {
      return span;
    }
    const translation = shown === "both" ? element.querySelector(TRANSLATION_ELEMENT) : null;
    return translation !== null && holdsWords(partIn(part, translation))
      ? trimmedSpan(text)
      : undefined;
  }
}
