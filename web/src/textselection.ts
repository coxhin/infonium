import { useEffect, useRef, type RefObject } from "react";
import { BLOCK_ATTRIBUTE } from "./blocks";
import { pointOffset, unitOffsets } from "./blocktext";
import type { Anchor, Anchors, Block, TranslatedBlock } from "./client";
import { NO_MARKING, type Marking, type Selected } from "./marking";
import { SELECTION_ACTIONS_CLASS } from "./selectionactions";
import type { Setting } from "./setting";
import { offsetAt, piecesOf, textOf } from "./textpieces";
import {
  LINE_END_ATTRIBUTE,
  LINE_START_ATTRIBUTE,
  LINES_ATTRIBUTE,
  SAID_CLASS,
  SPOKEN_CLASS,
} from "./transcript";
import { SEGMENT_GAP, TRANSLATION_ATTRIBUTE, type TextRange } from "./translated";

// What the reader selects in the text of a document, as a passage of it: where the words on
// show come from in the blocks' text, how a selection's two ends are taken back to it, and when
// a selection is handed over to what is offered on it.

// What the text view shows of its document: its blocks, the translations on show, the setting,
// and whether a recording's transcript is shown as lines.
export type TextOnShow = {
  documentId: string;
  blocks: Block[];
  translations: ReadonlyMap<number, TranslatedBlock>;
  setting: Setting;
  asLines: boolean;
};

// How long a selection made by keys or by a finger's handles must stay unchanged to count as
// made: the browser says when a selection changes, never when the reader has done changing it.
const SELECTION_SETTLES_MS = 300;

export const LINE_SELECTOR = `[${LINE_START_ATTRIBUTE}]`;
export const WORDS_SELECTOR = [
  `[${BLOCK_ATTRIBUTE}]`,
  `[${TRANSLATION_ATTRIBUTE}]`,
  `${LINE_SELECTOR} .${SAID_CLASS}`,
  `${LINE_SELECTOR} .${SPOKEN_CLASS}`,
].join(", ");

// A stretch of an element's text, in units, that stands for a range of a block's text, in code
// points: a translated segment, or a line's translation.
export type Spanned = { from: number; to: number; source: TextRange };

// Words of a block on show in one element, and how a stretch of them is taken back to the
// block's text. Exact words are the block's own, from a code point offset on, and are taken
// character by character; a translation is taken by whole segments, or whole lines.
export type Words =
  | { kind: "exact"; number: number; element: Element; start: number; text: string }
  | { kind: "translated"; number: number; element: Element; spans: Spanned[] };

export function numberIn(element: Element, attribute: string): number {
  return Number(element.getAttribute(attribute));
}

export function segmentSpans(translated: TranslatedBlock): Spanned[] {
  let from = 0;
  return translated.segments.map((segment) => {
    const span = { from, to: from + segment.text.length, source: segment };
    from = span.to + SEGMENT_GAP.length;
    return span;
  });
}

// The words an element of the text view holds, or none for an element that holds no words of
// the document.
export function wordsOf(element: Element, show: TextOnShow): Words | undefined {
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
export function sourceOf(words: Words, from: number, to: number): TextRange | undefined {
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
function anchorOf(show: TextOnShow, number: number, range: TextRange): Anchor | undefined {
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

function holdsPoint(element: Element, node: Node, offset: number): boolean {
  const range = document.createRange();
  range.selectNodeContents(element);
  return range.comparePoint(node, offset) === 0;
}

// What a selection takes of the words of one element. Words it covers are taken, the block's
// own character for character and a translation shown alone by its whole segments. Beside its
// source, a translation is taken only for the ends of the selection that lie in it: the end
// where the selection begins gives where the passage begins, the start of the segment it lies
// in, and the end where it stops gives where the passage stops, the end of the segment it lies
// in; a translation the selection runs over adds nothing.
type Taken =
  | { kind: "covered"; source: TextRange }
  | { kind: "ends"; begins: TextRange | undefined; stops: TextRange | undefined };

function takenIn(words: Words, range: Range, show: TextOnShow): Taken | undefined {
  const pieces = piecesOf(words.element);
  const from = offsetAt(pieces, range.startContainer, range.startOffset, "start");
  const to = offsetAt(pieces, range.endContainer, range.endOffset, "end");
  if (from >= to) {
    return undefined;
  }
  if (words.kind === "exact" || show.setting !== "both") {
    const source = sourceOf(words, from, to);
    return source === undefined ? undefined : { kind: "covered", source };
  }
  const beginsHere = holdsPoint(words.element, range.startContainer, range.startOffset);
  const stopsHere = holdsPoint(words.element, range.endContainer, range.endOffset);
  const begins = beginsHere ? words.spans.find((span) => from < span.to)?.source : undefined;
  const before = words.spans.filter((span) => span.from < to);
  const stops = stopsHere ? before[before.length - 1]?.source : undefined;
  return begins === undefined && stops === undefined
    ? undefined
    : { kind: "ends", begins, stops };
}

// The passage a selection covers in the text: for each block, the range from the first to
// the last character of its text that the selection takes, in the order of the document.
function passageOf(article: Element, range: Range, show: TextOnShow): Anchor[] {
  const taken = new Map<number, TextRange>();
  let begins: { number: number; segment: TextRange } | undefined;
  let stops: { number: number; segment: TextRange } | undefined;
  for (const element of article.querySelectorAll(WORDS_SELECTOR)) {
    if (!range.intersectsNode(element)) {
      continue;
    }
    const words = wordsOf(element, show);
    if (words === undefined || !words.element.checkVisibility()) {
      continue;
    }
    const part = takenIn(words, range, show);
    if (part?.kind === "covered") {
      const before = taken.get(words.number);
      taken.set(words.number, {
        start: Math.min(part.source.start, before?.start ?? part.source.start),
        end: Math.max(part.source.end, before?.end ?? part.source.end),
      });
    } else if (part?.kind === "ends") {
      begins = part.begins === undefined ? begins : { number: words.number, segment: part.begins };
      stops = part.stops === undefined ? stops : { number: words.number, segment: part.stops };
    }
  }
  // A passage that begins in a translation runs on from its segment to the end of the block,
  // after which the selection goes on; one that stops in a translation ends with its segment.
  if (begins !== undefined) {
    const length = Array.from(show.blocks[begins.number]?.text ?? "").length;
    const before = taken.get(begins.number);
    taken.set(begins.number, { start: begins.segment.start, end: before?.end ?? length });
  }
  if (stops !== undefined) {
    const before = taken.get(stops.number);
    taken.set(stops.number, {
      start: before?.start ?? stops.segment.start,
      end: stops.segment.end,
    });
  }
  return [...taken.entries()]
    .sort(([first], [second]) => first - second)
    .flatMap(([number, source]) => anchorOf(show, number, source) ?? []);
}

function holdsWords(range: Range): boolean {
  return range.toString().trim() !== "" && range.getClientRects().length > 0;
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

// The last line of a selection, in the window: that of the last text the selection takes
// that has words on show, found walking back from the selection's end.
function lastLineOf(range: Range): DOMRect {
  const walker = document.createTreeWalker(range.commonAncestorContainer, NodeFilter.SHOW_TEXT);
  const end = range.endContainer;
  let node: Node | null;
  if (end instanceof Text) {
    node = end;
  } else {
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

// Hands over what the reader selects in the text an article shows, while it is on show: when
// the pointer is let go, or once a selection made by keys or a finger has settled, and again
// with where it stands after a scroll. Where nothing can be marked, nothing is handed over.
export function useSelectionHandedOver(
  article: RefObject<HTMLElement | null>,
  show: TextOnShow,
  marking: Marking,
  onShow: boolean,
): void {
  const markable = marking !== NO_MARKING;
  const latest = useRef({ show, marking });
  latest.current = { show, marking };

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
      event.target instanceof Element &&
      event.target.closest(`.${SELECTION_ACTIONS_CLASS}`) !== null;
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
}
