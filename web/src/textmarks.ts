import { useEffect, useLayoutEffect, useRef, type MouseEvent, type RefObject } from "react";
import { BLOCK_ATTRIBUTE } from "./blocks";
import { unitOffset, unitOffsets } from "./blocktext";
import type { Anchor } from "./client";
import { lastBegun, NO_MARKING, type Marked, type Marking } from "./marking";
import { characterAt, offsetAt, piecesOf, rangeIn, textOf } from "./textpieces";
import {
  LINE_SELECTOR,
  numberIn,
  segmentSpans,
  sourceOf,
  wordsOf,
  WORDS_SELECTOR,
  type Spanned,
  type TextOnShow,
} from "./textselection";
import {
  LINE_END_ATTRIBUTE,
  LINE_START_ATTRIBUTE,
  LINES_ATTRIBUTE,
  SAID_CLASS,
  SPOKEN_CLASS,
} from "./transcript";
import { overlaps, TRANSLATION_ATTRIBUTE, type TextRange } from "./translated";
import "./textmarks.css";

// The reader's highlights in the text of a document: drawn over the words wherever they are on
// show, and pressed there. Highlights are drawn through the browser's registry of highlights,
// never into the page, so that drawing them redraws no block, and a copy or a print of the
// text carries nothing of them.

// The layers of the drawing, each a highlight of the registry, from the lowest: the words of
// every highlight; the translated segments or lines that overlap one, where only the
// translation is on show; words two highlights share; the highlights that have a comment; the
// opened one. The passage pointed at lies above them all (reader.tsx).
const MARKED = "text-marked";
const MARKED_QUIETLY = "text-marked-quietly";
const MARKED_TWICE = "text-marked-twice";
const COMMENTED = "text-commented";
const OPENED = "text-opened";
const LAYERS = [MARKED, MARKED_QUIETLY, MARKED_TWICE, COMMENTED, OPENED] as const;
type Layer = (typeof LAYERS)[number];
const PRIORITIES: Record<Layer, number> = {
  [MARKED]: 1,
  [MARKED_QUIETLY]: 1,
  [MARKED_TWICE]: 2,
  [COMMENTED]: 3,
  [OPENED]: 4,
};

// A press opens a highlight only once it is seen to be a single press: the first press of a
// double or triple press, which selects a word or a line, comes with nothing selected yet. The
// browser does not say how long it waits for a second press, so this is a short wait that
// takes in a quick double press; a slower one opens the highlight as well.
const PRESS_SETTLES_MS = 250;
// What CSS takes a "normal" line height to be, as a share of the font's size.
const NORMAL_LINE_HEIGHT = 1.2;

// A press's place comes in whole pixels, which may lie up to one pixel outside the edge of
// what the browser itself found under the press.
const PRESS_ROUNDING = 1;

function contains(rect: DOMRect, x: number, y: number): boolean {
  return (
    rect.left - PRESS_ROUNDING <= x &&
    x <= rect.right + PRESS_ROUNDING &&
    rect.top - PRESS_ROUNDING <= y &&
    y <= rect.bottom + PRESS_ROUNDING
  );
}

// The rectangles of a range, each stretched to the height of the line it stands on in an
// element, so that a press in the space between two lines of words lies on one of them.
function lineRects(range: Range, element: Element): DOMRect[] {
  const style = getComputedStyle(element);
  const set = parseFloat(style.lineHeight);
  const height = Number.isNaN(set) ? parseFloat(style.fontSize) * NORMAL_LINE_HEIGHT : set;
  return [...range.getClientRects()].map((rect) => {
    const added = Math.max(0, (height - rect.height) / 2);
    return new DOMRect(rect.left, rect.top - added, rect.width, rect.height + 2 * added);
  });
}

function spanAt(spans: Spanned[], offset: number): Spanned | undefined {
  return spans.find((span) => span.from <= offset && offset < span.to);
}

// The range of a block's text whose drawing lies under a point of the window, where the words
// there are drawn with the highlights: a character or a formula of the block's own words, with
// the line it stands on; a segment of a translation, with its lines; or a line of a
// recording's translation, by its whole box. A translation counts only where it is on show
// alone, since beside its source it is not drawn.
function pressedRange(
  article: Element,
  x: number,
  y: number,
  show: TextOnShow,
): { number: number; source: TextRange } | undefined {
  const caret = document.caretPositionFromPoint(x, y);
  const node = caret?.offsetNode ?? null;
  const element = node instanceof Element ? node : (node?.parentElement ?? null);
  const holder = element?.closest(WORDS_SELECTOR) ?? null;
  if (caret === null || node === null || holder === null || !article.contains(holder)) {
    return undefined;
  }
  const words = wordsOf(holder, show);
  if (words === undefined || (words.kind === "translated" && show.setting !== "translation")) {
    return undefined;
  }
  if (words.kind === "translated" && words.element.classList.contains(SPOKEN_CLASS)) {
    const line = words.spans[0];
    return line !== undefined && contains(words.element.getBoundingClientRect(), x, y)
      ? { number: words.number, source: line.source }
      : undefined;
  }
  const pieces = piecesOf(words.element);
  const at = offsetAt(pieces, node, caret.offset, "start");
  // The caret stands between two characters; the one under the point is either.
  for (const offset of [at, at - 1]) {
    const character = offset < 0 ? undefined : characterAt(pieces, offset);
    const stretch =
      character === undefined || words.kind === "exact"
        ? character
        : spanAt(words.spans, character.start);
    if (stretch === undefined) {
      continue;
    }
    const start = "source" in stretch ? stretch.from : stretch.start;
    const end = "source" in stretch ? stretch.to : stretch.end;
    const rects = lineRects(rangeIn(words.element, start, end), words.element);
    if (rects.some((rect) => contains(rect, x, y))) {
      const source = sourceOf(words, start, end);
      return source === undefined ? undefined : { number: words.number, source };
    }
  }
  return undefined;
}

// Whether an anchor's quote is the text of its block at its range: the highlights come from
// the service, and one that does not fit the document on show is not drawn.
function fits(anchor: Anchor, show: TextOnShow): boolean {
  if (anchor.document_id !== show.documentId) {
    return false;
  }
  const text = show.blocks[anchor.block_number]?.text;
  if (text === undefined) {
    return false;
  }
  const offsets = unitOffsets(text);
  return (
    anchor.start < anchor.end &&
    anchor.end < offsets.length &&
    text.slice(unitOffset(offsets, anchor.start), unitOffset(offsets, anchor.end)) === anchor.quote
  );
}

// Where the elements of the text view are, by block number, found once for a drawing.
type Elements = {
  sources: Map<number, Element>;
  translations: Map<number, Element>;
  lines: Map<number, Element>;
};

function elementsOf(article: Element): Elements {
  const byNumber = (attribute: string) =>
    new Map(
      [...article.querySelectorAll(`[${attribute}]`)].map(
        (element) => [numberIn(element, attribute), element] as const,
      ),
    );
  return {
    sources: byNumber(BLOCK_ATTRIBUTE),
    translations: byNumber(TRANSLATION_ATTRIBUTE),
    lines: byNumber(LINES_ATTRIBUTE),
  };
}

// The ranges of the page that draw a range of a block's text: its own words where they are on
// show, character for character; else, where only its translation is, the segments or lines
// that overlap it, which may hold more than it.
type Drawn = { exact: Range[]; quiet: Range[] };

function drawnRanges(
  number: number,
  source: TextRange,
  show: TextOnShow,
  elements: Elements,
): Drawn {
  const drawn: Drawn = { exact: [], quiet: [] };
  const text = show.blocks[number]?.text ?? "";
  if (show.asLines) {
    const group = elements.lines.get(number);
    for (const line of group?.querySelectorAll(LINE_SELECTOR) ?? []) {
      const range = {
        start: numberIn(line, LINE_START_ATTRIBUTE),
        end: numberIn(line, LINE_END_ATTRIBUTE),
      };
      if (!overlaps(range, source)) {
        continue;
      }
      const said = line.querySelector(`.${SAID_CLASS}`);
      const spoken = line.querySelector(`.${SPOKEN_CLASS}`);
      if (said !== null) {
        const offsets = unitOffsets(textOf(said));
        drawn.exact.push(
          rangeIn(
            said,
            unitOffset(offsets, Math.max(source.start, range.start) - range.start),
            unitOffset(offsets, Math.min(source.end, range.end) - range.start),
          ),
        );
      } else if (spoken !== null) {
        const whole = document.createRange();
        whole.selectNodeContents(spoken);
        drawn.quiet.push(whole);
      }
    }
    return drawn;
  }
  const translated = show.translations.get(number);
  const form = elements.translations.get(number)?.firstElementChild ?? null;
  if (show.setting === "translation" && translated !== undefined && form !== null) {
    for (const span of segmentSpans(translated)) {
      if (overlaps(span.source, source) && span.from < span.to) {
        drawn.quiet.push(rangeIn(form, span.from, span.to));
      }
    }
    return drawn;
  }
  const element = elements.sources.get(number);
  if (element !== undefined) {
    const offsets = unitOffsets(text);
    drawn.exact.push(
      rangeIn(element, unitOffset(offsets, source.start), unitOffset(offsets, source.end)),
    );
  }
  return drawn;
}

// The stretches of each block that two highlights or more share.
function sharedStretches(marked: Marked[]): { number: number; source: TextRange }[] {
  const byBlock = new Map<number, { id: string; source: TextRange }[]>();
  for (const highlight of marked) {
    for (const anchor of highlight.anchors) {
      const list = byBlock.get(anchor.block_number) ?? [];
      list.push({ id: highlight.id, source: anchor });
      byBlock.set(anchor.block_number, list);
    }
  }
  const shared: { number: number; source: TextRange }[] = [];
  byBlock.forEach((list, number) => {
    list.forEach((one, index) => {
      for (const other of list.slice(index + 1)) {
        if (one.id !== other.id && overlaps(one.source, other.source)) {
          shared.push({
            number,
            source: {
              start: Math.max(one.source.start, other.source.start),
              end: Math.min(one.source.end, other.source.end),
            },
          });
        }
      }
    });
  });
  return shared;
}

// Draws the highlights in the text on show, a layer at a time.
function draw(article: Element, show: TextOnShow, marking: Marking): Map<Layer, Highlight> {
  const elements = elementsOf(article);
  const ranges = new Map<Layer, Range[]>(LAYERS.map((layer) => [layer, []]));
  const add = (layer: Layer, added: Range[]) => ranges.get(layer)?.push(...added);
  const marked = marking.marked
    .map((highlight) => ({
      ...highlight,
      anchors: highlight.anchors.filter((anchor) => fits(anchor, show)),
    }))
    .filter((highlight): highlight is Marked => highlight.anchors.length > 0);
  for (const highlight of marked) {
    for (const anchor of highlight.anchors) {
      const drawn = drawnRanges(anchor.block_number, anchor, show, elements);
      add(MARKED, drawn.exact);
      add(MARKED_QUIETLY, drawn.quiet);
      const all = [...drawn.exact, ...drawn.quiet];
      if (highlight.commented) {
        add(COMMENTED, all);
      }
      if (highlight.id === marking.opened) {
        add(OPENED, all);
      }
    }
  }
  for (const { number, source } of sharedStretches(marked)) {
    add(MARKED_TWICE, drawnRanges(number, source, show, elements).exact);
  }
  const drawn = new Map<Layer, Highlight>();
  ranges.forEach((layerRanges, layer) => {
    const highlight = new Highlight(...layerRanges);
    highlight.priority = PRIORITIES[layer];
    CSS.highlights.set(layer, highlight);
    drawn.set(layer, highlight);
  });
  return drawn;
}

// Takes away the layers this drawing set, and leaves any that another has set since.
function undraw(drawn: Map<Layer, Highlight>): void {
  drawn.forEach((highlight, layer) => {
    if (CSS.highlights.get(layer) === highlight) {
      CSS.highlights.delete(layer);
    }
  });
}

// The highlight under a press: of those drawn there, the one that begins last.
function pressedHighlight(
  article: Element,
  x: number,
  y: number,
  show: TextOnShow,
  marked: Marked[],
): string | undefined {
  const pressed = pressedRange(article, x, y, show);
  if (pressed === undefined) {
    return undefined;
  }
  return lastBegun(
    marked.filter((highlight) =>
      highlight.anchors.some(
        (anchor) =>
          anchor.block_number === pressed.number &&
          fits(anchor, show) &&
          overlaps(anchor, pressed.source),
      ),
    ),
  )?.id;
}


// Draws the highlights in the text an article shows, and gives the handler of a press, to be
// called before any other in the article, that tells a press on a highlight. A press on one
// is held back from what else it would do there at once, as playing from a recording's line,
// and opens it once it is seen to be a single press. Where nothing can be marked, none of it is
// done, and the page is left as it was.
export function useTextMarks(
  article: RefObject<HTMLElement | null>,
  show: TextOnShow,
  marking: Marking,
): (event: MouseEvent) => void {
  const markable = marking !== NO_MARKING;
  const latest = useRef({ show, marking });
  latest.current = { show, marking };
  const waiting = useRef<ReturnType<typeof setTimeout>>(undefined);

  useLayoutEffect(() => {
    const element = article.current;
    if (!markable || element === null) {
      return;
    }
    const drawn = draw(element, show, marking);
    return () => undraw(drawn);
  }, [markable, show, marking.marked, marking.opened]);

  // The next press, of a double press or of anything else, takes back a press that waits.
  useEffect(() => {
    if (!markable) {
      return;
    }
    const cancel = () => clearTimeout(waiting.current);
    addEventListener("pointerdown", cancel, true);
    return () => {
      cancel();
      removeEventListener("pointerdown", cancel, true);
    };
  }, [markable]);

  return (event: MouseEvent) => {
    const element = article.current;
    if (
      !markable ||
      element === null ||
      getSelection()?.isCollapsed === false ||
      (event.target instanceof Element && event.target.closest("a, button") !== null)
    ) {
      return;
    }
    const { show: shown, marking: current } = latest.current;
    const id = pressedHighlight(element, event.clientX, event.clientY, shown, current.marked);
    if (id === undefined) {
      return;
    }
    // A press on a highlight in a recording's lines is no request to play from the line.
    event.stopPropagation();
    if (event.detail <= 1) {
      waiting.current = setTimeout(() => current.onPress(id), PRESS_SETTLES_MS);
    }
  };
}
