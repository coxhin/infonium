import { useEffect, useLayoutEffect, useRef, type MouseEvent, type RefObject } from "react";
import { BLOCK_ATTRIBUTE } from "./blocks";
import { pointOffset, unitOffset, unitOffsets } from "./blocktext";
import type { Anchor, Anchors, Block, TranslatedBlock } from "./client";
import { NO_MARKING, type Marked, type Marking, type Selected } from "./marking";
import { SELECTION_ACTIONS_CLASS } from "./selectionactions";
import type { Setting } from "./setting";
import {
  characterAt,
  offsetAt,
  piecesOf,
  rangeIn,
  textOf,
  type TextPiece,
} from "./textpieces";
import {
  LINE_END_ATTRIBUTE,
  LINE_START_ATTRIBUTE,
  LINES_ATTRIBUTE,
  SAID_CLASS,
  SPOKEN_CLASS,
} from "./transcript";
import { overlaps, SEGMENT_GAP, TRANSLATION_ATTRIBUTE, type TextRange } from "./translated";
import "./textmarks.css";

// The reader's highlights in the text of a document: drawn over the words wherever they are on
// show, pressed there, and the passage the reader selects there handed over. Highlights are
// drawn through the browser's registry of highlights, never into the page, so that drawing
// them redraws no block, and a copy or a print of the text carries nothing of them.

// What the text view shows of its document: its blocks, the translations on show, the setting,
// whether a recording's transcript is shown as lines, and the blocks a translation that stopped
// left in their source, whose drawing that changes.
export type TextOnShow = {
  documentId: string;
  blocks: Block[];
  translations: ReadonlyMap<number, TranslatedBlock>;
  setting: Setting;
  asLines: boolean;
  leftOut: ReadonlySet<number>;
};

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

// How long a selection made by keys or by a finger's handles must stay unchanged to count as
// made: the browser says when a selection changes, never when the reader has done changing it.
const SELECTION_SETTLES_MS = 300;

const LINE_SELECTOR = `[${LINE_START_ATTRIBUTE}]`;
const WORDS_SELECTOR = [
  `[${BLOCK_ATTRIBUTE}]`,
  `[${TRANSLATION_ATTRIBUTE}]`,
  `${LINE_SELECTOR} .${SAID_CLASS}`,
  `${LINE_SELECTOR} .${SPOKEN_CLASS}`,
].join(", ");

// A stretch of an element's text, in units, that stands for a range of a block's text, in code
// points: a translated segment, or a line's translation.
type Spanned = { from: number; to: number; source: TextRange };

// Words of a block on show in one element, and how a stretch of them is taken back to the
// block's text. Exact words are the block's own, from a code point offset on, and are taken
// character by character; a translation is taken by whole segments, or whole lines.
type Words =
  | { kind: "exact"; number: number; element: Element; start: number; text: string }
  | { kind: "translated"; number: number; element: Element; spans: Spanned[] };

function numberIn(element: Element, attribute: string): number {
  return Number(element.getAttribute(attribute));
}

function segmentSpans(translated: TranslatedBlock): Spanned[] {
  let from = 0;
  return translated.segments.map((segment) => {
    const span = { from, to: from + segment.text.length, source: segment };
    from = span.to + SEGMENT_GAP.length;
    return span;
  });
}

// The words an element of the text view holds, or none for an element that holds no words of
// the document.
function wordsOf(element: Element, show: TextOnShow): Words | undefined {
  if (element.hasAttribute(BLOCK_ATTRIBUTE)) {
    const number = numberIn(element, BLOCK_ATTRIBUTE);
    const text = show.blocks[number]?.text;
    return text === undefined ? undefined : { kind: "exact", number, element, start: 0, text };
  }
  if (element.hasAttribute(TRANSLATION_ATTRIBUTE)) {
    const number = numberIn(element, TRANSLATION_ATTRIBUTE);
    const translated = show.translations.get(number);
    // The first element is the translation's form; the control beside it holds no words.
    const form = element.firstElementChild;
    return translated === undefined || form === null
      ? undefined
      : { kind: "translated", number, element: form, spans: segmentSpans(translated) };
  }
  const line = element.closest(LINE_SELECTOR);
  const group = element.closest(`[${LINES_ATTRIBUTE}]`);
  if (line === null || group === null) {
    return undefined;
  }
  const number = numberIn(group, LINES_ATTRIBUTE);
  const source = {
    start: numberIn(line, LINE_START_ATTRIBUTE),
    end: numberIn(line, LINE_END_ATTRIBUTE),
  };
  const text = textOf(element);
  return element.classList.contains(SAID_CLASS)
    ? { kind: "exact", number, element, start: source.start, text }
    : { kind: "translated", number, element, spans: [{ from: 0, to: text.length, source }] };
}

// The range of a block's text, in code points, that a stretch of words on show stands for.
function sourceOf(words: Words, from: number, to: number): TextRange | undefined {
  if (words.kind === "exact") {
    const offsets = unitOffsets(words.text);
    return {
      start: words.start + pointOffset(offsets, from, "start"),
      end: words.start + pointOffset(offsets, to, "end"),
    };
  }
  const touched = words.spans.filter((span) => span.from < to && from < span.to);
  const first = touched[0];
  const last = touched[touched.length - 1];
  return first === undefined || last === undefined
    ? undefined
    : { start: first.source.start, end: last.source.end };
}

const WHITE = /\s/u;

// The anchor of a range of a block's text, with no white space at either end; none if nothing
// else is left.
function anchorOf(
  show: TextOnShow,
  number: number,
  range: TextRange,
): Anchor | undefined {
  const text = show.blocks[number]?.text ?? "";
  const characters = Array.from(text);
  let { start, end } = range;
  while (start < end && WHITE.test(characters[start] ?? "")) {
    start += 1;
  }
  while (end > start && WHITE.test(characters[end - 1] ?? "")) {
    end -= 1;
  }
  return start === end
    ? undefined
    : {
        document_id: show.documentId,
        block_number: number,
        start,
        end,
        quote: characters.slice(start, end).join(""),
      };
}

// The passage a selection covers in the text: for each block, the range from the first to
// the last character of its text that the selection takes, in the order of the document.
function passageOf(article: Element, range: Range, show: TextOnShow): Anchor[] {
  const taken = new Map<number, TextRange>();
  for (const element of article.querySelectorAll(WORDS_SELECTOR)) {
    if (!range.intersectsNode(element)) {
      continue;
    }
    const words = wordsOf(element, show);
    if (words === undefined || !words.element.checkVisibility()) {
      continue;
    }
    const pieces = piecesOf(words.element);
    const from = offsetAt(pieces, range.startContainer, range.startOffset, "start");
    const to = offsetAt(pieces, range.endContainer, range.endOffset, "end");
    const source = from < to ? sourceOf(words, from, to) : undefined;
    if (source !== undefined) {
      const before = taken.get(words.number);
      taken.set(words.number, {
        start: Math.min(source.start, before?.start ?? source.start),
        end: Math.max(source.end, before?.end ?? source.end),
      });
    }
  }
  return [...taken.entries()]
    .sort(([first], [second]) => first - second)
    .flatMap(([number, source]) => anchorOf(show, number, source) ?? []);
}

function holdsWords(range: Range): boolean {
  return range.toString().trim() !== "" && range.getClientRects().length > 0;
}

// The last line of a selection, in the window: that of the last text the selection takes
// that has words on show, found walking back from the selection's end.
function lastLineOf(range: Range): DOMRect {
  const root = range.commonAncestorContainer;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const end = range.endContainer;
  let node: Node | null;
  if (end instanceof Text) {
    node = end;
  } else {
    // The last text before the end: the end's own node at the end of the walk's order, and
    // from there back.
    const after = end.childNodes[range.endOffset] ?? null;
    walker.currentNode = after ?? end;
    node = after === null ? lastTextIn(end) : walker.previousNode();
  }
  while (node !== null) {
    if (node instanceof Text && range.intersectsNode(node)) {
      const part = document.createRange();
      part.selectNodeContents(node);
      if (node === range.startContainer) {
        part.setStart(node, range.startOffset);
      }
      if (node === range.endContainer) {
        part.setEnd(node, range.endOffset);
      }
      if (holdsWords(part)) {
        const rects = part.getClientRects();
        return rects[rects.length - 1] ?? part.getBoundingClientRect();
      }
    }
    if (range.comparePoint(node, 0) < 0) {
      break;
    }
    walker.currentNode = node;
    node = walker.previousNode();
  }
  return range.getBoundingClientRect();
}

function lastTextIn(node: Node): Node | null {
  const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  let last: Node | null = null;
  for (let text = walker.nextNode(); text !== null; text = walker.nextNode()) {
    last = text;
  }
  if (last !== null) {
    return last;
  }
  walker.currentNode = node;
  return walker.previousNode();
}

function sameAnchors(first: Anchor[], second: Anchor[]): boolean {
  return (
    first.length === second.length &&
    first.every((anchor, index) => {
      const other = second[index];
      return (
        other !== undefined &&
        anchor.block_number === other.block_number &&
        anchor.start === other.start &&
        anchor.end === other.end
      );
    })
  );
}

function contains(rect: DOMRect, x: number, y: number): boolean {
  return rect.left <= x && x <= rect.right && rect.top <= y && y <= rect.bottom;
}

// The range of a block's text whose drawing lies under a point of the window, where the words
// there are drawn with the highlights: a character or a formula of words in the block's own
// words, a segment or a line of a translation where only the translation is on show.
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
  const pieces: TextPiece[] = piecesOf(words.element);
  const at = offsetAt(pieces, node, caret.offset, "start");
  // The caret stands between two characters; the one under the point is either.
  for (const offset of [at, at - 1]) {
    const character = offset < 0 ? undefined : characterAt(pieces, offset);
    if (character === undefined) {
      continue;
    }
    const rects = rangeIn(words.element, character.start, character.end).getClientRects();
    if ([...rects].some((rect) => contains(rect, x, y))) {
      const source = sourceOf(words, character.start, character.end);
      return source === undefined ? undefined : { number: words.number, source };
    }
  }
  return undefined;
}

function beginsAfter(first: Marked, second: Marked): boolean {
  const [one, other] = [first.anchors[0], second.anchors[0]];
  return one.block_number !== other.block_number
    ? one.block_number > other.block_number
    : one.start > other.start;
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
  return marked
    .filter((highlight) =>
      highlight.anchors.some(
        (anchor) =>
          anchor.block_number === pressed.number &&
          fits(anchor, show) &&
          overlaps(anchor, pressed.source),
      ),
    )
    .reduce<Marked | undefined>(
      (last, highlight) => (last === undefined || beginsAfter(highlight, last) ? highlight : last),
      undefined,
    )?.id;
}

// Draws the highlights in the text an article shows, hands over what the reader selects in it
// while it is on show, and gives the handler of a press, to be called before any other in the
// article, that tells a press on a highlight. Where nothing can be marked, none of it is done,
// and the page is left as it was.
export function useTextMarks(
  article: RefObject<HTMLElement | null>,
  show: TextOnShow,
  marking: Marking,
  onShow: boolean,
): (event: MouseEvent) => void {
  const markable = marking !== NO_MARKING;
  const latest = useRef({ show, marking });
  latest.current = { show, marking };

  useLayoutEffect(() => {
    const element = article.current;
    if (!markable || element === null) {
      return;
    }
    const drawn = draw(element, show, marking);
    return () => undraw(drawn);
  }, [markable, show, marking.marked, marking.opened]);

  useEffect(() => {
    const element = article.current;
    if (!markable || !onShow || element === null) {
      return;
    }
    let handed: Selected | undefined;
    let pressing = false;
    let settling: ReturnType<typeof setTimeout> | undefined;
    let measuring: number | undefined;

    const handOver = (selected: Selected | undefined) => {
      handed = selected;
      latest.current.marking.onSelect(selected);
    };
    const letGo = () => {
      if (handed !== undefined) {
        handOver(undefined);
      }
    };
    const clear = () => {
      getSelection()?.removeAllRanges();
      letGo();
    };
    const selectionNow = (): Range | undefined => {
      const selection = getSelection();
      return selection === null || selection.rangeCount === 0 || selection.isCollapsed
        ? undefined
        : selection.getRangeAt(0);
    };
    const settle = () => {
      clearTimeout(settling);
      const range = selectionNow();
      const anchors = range === undefined ? [] : passageOf(element, range, latest.current.show);
      const [first, ...rest] = anchors;
      if (range === undefined || first === undefined) {
        letGo();
        return;
      }
      if (handed !== undefined && sameAnchors(handed.anchors, anchors)) {
        return;
      }
      const passage: Anchors = [first, ...rest];
      handOver({ anchors: passage, at: lastLineOf(range), clear });
    };

    const isOnActions = (event: Event) =>
      event.target instanceof Element && event.target.closest(`.${SELECTION_ACTIONS_CLASS}`) !== null;
    const pressed = (event: PointerEvent) => {
      if (event.isPrimary && !isOnActions(event)) {
        pressing = true;
      }
    };
    const released = (event: PointerEvent) => {
      if (pressing && event.isPrimary) {
        pressing = false;
        settle();
      }
    };
    const changed = () => {
      if (pressing) {
        return;
      }
      clearTimeout(settling);
      if (selectionNow() === undefined) {
        letGo();
      } else {
        settling = setTimeout(settle, SELECTION_SETTLES_MS);
      }
    };
    // What is offered stands by the selection's end, which a scroll moves in the window.
    const scrolled = () => {
      if (handed === undefined || measuring !== undefined) {
        return;
      }
      measuring = requestAnimationFrame(() => {
        measuring = undefined;
        const range = selectionNow();
        if (handed !== undefined && range !== undefined) {
          handOver({ ...handed, at: lastLineOf(range) });
        }
      });
    };

    addEventListener("pointerdown", pressed, true);
    addEventListener("pointerup", released, true);
    addEventListener("pointercancel", released, true);
    document.addEventListener("selectionchange", changed);
    addEventListener("scroll", scrolled, { capture: true, passive: true });
    return () => {
      clearTimeout(settling);
      if (measuring !== undefined) {
        cancelAnimationFrame(measuring);
      }
      removeEventListener("pointerdown", pressed, true);
      removeEventListener("pointerup", released, true);
      removeEventListener("pointercancel", released, true);
      document.removeEventListener("selectionchange", changed);
      removeEventListener("scroll", scrolled, { capture: true });
      letGo();
    };
  }, [markable, onShow]);

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
    if (id !== undefined) {
      // A press on a highlight in a recording's lines is no request to play from the line.
      event.stopPropagation();
      current.onPress(id);
    }
  };
}
