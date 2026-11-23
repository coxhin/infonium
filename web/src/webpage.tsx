import { useLayoutEffect, useRef, useState, type RefObject } from "react";
import { unitOffset, unitOffsets } from "./blocks";
import {
  pageLink,
  readPageBlocks,
  type Anchor,
  type Block,
  type Mark,
  type PageBlocks,
  type Style,
  type TranslatedBlock,
} from "./client";
import { useLoaded } from "./loading";
import { scrollerOf, scrolls } from "./scrolling";
import type { Setting } from "./setting";
import type { Pointed, ViewContext } from "./views";
import "./webpage.css";

// What infonium adds to the page goes in an element of a name of its own: the service takes
// every element of such a name out of the page before it marks the blocks, so none is the
// page's. Unknown to the page's stylesheet, it is styled by infonium's alone.
const TRANSLATION_ELEMENT = "infonium-translation";
const POINTED_HIGHLIGHT = "infonium-pointed";
const STYLE_ELEMENTS: Record<Style, string> = {
  strong: "strong",
  emphasis: "em",
  underline: "u",
  code: "code",
  subscript: "sub",
  superscript: "sup",
};
const FORMULA_ELEMENT = "code";
const LINE_BREAK_ELEMENT = "br";
const LINE_BREAK = "\n";
// Segments of one block follow each other as sentences of one paragraph do.
const SEGMENT_GAP = " ";
const CELL_PLACE_SEPARATOR = ",";
const LINKS = "a[href], area[href]";
const LINK_ADDRESS = "href";
// Only an address of the web is opened from the page; the service has taken out any other
// that would run.
const WEB_PROTOCOLS = ["http:", "https:"];
const NEW_TAB = "_blank";
const NEW_TAB_FEATURES = "noopener,noreferrer";
const MIDDLE_BUTTON = 1;
// What the extractor leaves out of a block's text that the page shows or holds: words struck
// out, and code for the browser.
const UNREAD_ELEMENTS = "del, s, strike, script, style, noscript, template";
const INLINE_DISPLAY = "inline";
const FORMAT_CHARACTER = /\p{Cf}/u;
const SPACE = /\s/u;
// Typesetting that a quote may differ in from the page, as the service compares quotes.
const PLAIN_PUNCTUATION: Record<string, string> = {
  "“": '"',
  "”": '"',
  "„": '"',
  "«": '"',
  "»": '"',
  "‘": "'",
  "’": "'",
  "‚": "'",
  "‐": "-",
  "‑": "-",
  "‒": "-",
  "–": "-",
  "—": "-",
  "―": "-",
};
const COMPARISON_FORM = "NFKD";
// A frame never shorter than this, in pixels, however little of the window is left for it.
const SHORTEST_FRAME = 320;
// A part of the frame in view smaller than this, in pixels, is no room to show a passage in;
// the whole frame is taken instead.
const LEAST_ROOM = 48;

const UNPLACED = "这段引文所在的段落在原件里没有单独的位置，标不出来；请到「文本」里看。";
const PAGE_UNREADABLE = "原件的页面没有打开";

// The page in the frame, as it was served and as far as infonium has changed it: the elements
// whose content holds a translation now, each with the nodes it held as served, put back as
// they were when the page is shown in its source again.
class FramedPage {
  readonly document: Document;
  readonly view: Window & typeof globalThis;
  readonly pointedRule: CSSStyleSheet;
  readonly served = new Map<Element, Node[]>();
  readonly written = new Map<Element, { setting: Setting; translated: TranslatedBlock }>();

  constructor(document: Document, view: Window & typeof globalThis) {
    this.document = document;
    this.view = view;
    // Stylesheets of the frame's own realm, adopted rather than put in the page, so that the
    // page's nodes stay those it was served with.
    const own = new view.CSSStyleSheet();
    own.replaceSync(FRAME_STYLE);
    this.pointedRule = new view.CSSStyleSheet();
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, own, this.pointedRule];
  }

  holdsSource(element: Element): boolean {
    return !this.written.has(element);
  }

  // Puts in an element the content the setting asks for: the nodes it was served with, these
  // nodes in their place, or these nodes after them.
  write(
    element: Element,
    setting: Setting,
    translated: TranslatedBlock | undefined,
    nodes: () => Node[],
  ): void {
    const before = this.written.get(element);
    const wanted = setting === "source" ? undefined : translated;
    if (before?.translated === wanted && (wanted === undefined || before?.setting === setting)) {
      return;
    }
    const served = this.served.get(element) ?? [...element.childNodes];
    this.served.set(element, served);
    if (wanted === undefined) {
      element.replaceChildren(...served);
      this.written.delete(element);
      return;
    }
    if (setting === "translation") {
      element.replaceChildren(...nodes());
    } else {
      const beside = this.document.createElement(TRANSLATION_ELEMENT);
      beside.append(...nodes());
      element.replaceChildren(...served, beside);
    }
    this.written.set(element, { setting, translated: wanted });
  }

  mark(selector: string, passage: Range): void {
    this.pointedRule.replaceSync(`${selector} { ${POINTED_OUTLINE} }`);
    this.view.CSS.highlights.set(POINTED_HIGHLIGHT, new this.view.Highlight(passage));
  }

  unmark(): void {
    this.pointedRule.replaceSync("");
    this.view.CSS.highlights.delete(POINTED_HIGHLIGHT);
  }
}

// The translation beside its source is set quieter in the page's own colours and type: a rule
// of the text's own colour before it, the words faded. A passage pointed at is marked in
// colours of its own, the same on any page, light or dark: an outline in two colours that
// shows against either, and its words dark on yellow. The browser's own anchoring of the
// scroll is left to infonium, which keeps the place as it changes the page.
const POINTED_OUTLINE =
  "outline: 3px solid #f2a900 !important; outline-offset: 2px !important;" +
  " box-shadow: 0 0 0 5px rgb(0 0 0 / 0.45) !important;";
const FRAME_STYLE = `
${TRANSLATION_ELEMENT} {
  display: block;
  margin: 0.35em 0 0;
  padding: 0 0 0 0.6em;
  border: 0;
  border-inline-start: 2px solid;
  opacity: 0.72;
  font-size: 0.92em;
}
::highlight(${POINTED_HIGHLIGHT}) {
  background-color: #ffe08a;
  color: #1f2328;
}
html, body {
  overflow-anchor: none !important;
}
`;

function elementOf(target: EventTarget | null): Element | null {
  if (target === null || !("nodeType" in target)) {
    return null;
  }
  const node = target as Node;
  return node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement;
}

// A link in the page is opened in a tab of its own, and never in the frame, by a click or by
// the middle button.
function openLinksElsewhere(page: Document): void {
  function open(event: MouseEvent) {
    const link = elementOf(event.target)?.closest(LINKS);
    if (link === null || link === undefined) {
      return;
    }
    event.preventDefault();
    const address = new URL(link.getAttribute(LINK_ADDRESS) ?? "", page.baseURI);
    if (WEB_PROTOCOLS.includes(address.protocol)) {
      window.open(address.href, NEW_TAB, NEW_TAB_FEATURES);
    }
  }
  page.addEventListener("click", open, true);
  page.addEventListener(
    "auxclick",
    (event) => event.button === MIDDLE_BUTTON && open(event),
    true,
  );
}

function linesOf(page: Document, text: string): Node[] {
  return text
    .split(LINE_BREAK)
    .flatMap((line, index) => [
      ...(index > 0 ? [page.createElement(LINE_BREAK_ELEMENT)] : []),
      ...(line === "" ? [] : [page.createTextNode(line)]),
    ]);
}

function markElement(page: Document, mark: Mark): Element {
  switch (mark.kind) {
    case "link": {
      const link = page.createElement("a");
      link.setAttribute(LINK_ADDRESS, mark.target);
      return link;
    }
    case "style":
      return page.createElement(STYLE_ELEMENTS[mark.style]);
    case "formula":
      return page.createElement(FORMULA_ELEMENT);
  }
}

// The text as the page's own elements: cut wherever a mark starts or ends, each piece inside
// the elements of the marks that hold it, the first mark innermost, as the reader draws it.
function markedNodes(page: Document, text: string, marks: Mark[]): Node[] {
  const offsets = unitOffsets(text);
  const spans = marks.map((mark) => ({
    mark,
    start: unitOffset(offsets, mark.start),
    end: unitOffset(offsets, mark.end),
  }));
  const cuts = [
    ...new Set([0, text.length, ...spans.flatMap((span) => [span.start, span.end])]),
  ].sort((first, second) => first - second);
  return cuts.slice(0, -1).flatMap((start, index) => {
    const end = cuts[index + 1] ?? text.length;
    const inside = spans.filter((span) => span.start <= start && end <= span.end);
    return inside.reduce<Node[]>((content, span) => {
      const element = markElement(page, span.mark);
      element.append(...content);
      return [element];
    }, linesOf(page, text.slice(start, end)));
  });
}

function translatedNodes(page: Document, translated: TranslatedBlock): Node[] {
  return translated.segments.flatMap((segment, index) => [
    ...(index > 0 ? [page.createTextNode(SEGMENT_GAP)] : []),
    ...markedNodes(page, segment.text, segment.marks),
  ]);
}

function translatedText(translated: TranslatedBlock): string {
  return translated.segments.map((segment) => segment.text).join(SEGMENT_GAP);
}

type Cell = { element: Element; row: number; cell: number };

// The cells of a table block in the page, in the page's order, each with its place in the
// block's text.
function cellsOf(page: Document, pageBlocks: PageBlocks, number: number): Cell[] {
  const attribute = pageBlocks.cell_attribute;
  return [...page.querySelectorAll(`[${attribute}^="${number}${CELL_PLACE_SEPARATOR}"]`)].flatMap(
    (element) => {
      const [block, row, cell] = (element.getAttribute(attribute) ?? "")
        .split(CELL_PLACE_SEPARATOR)
        .map(Number);
      return block === number && row !== undefined && cell !== undefined
        ? [{ element, row, cell }]
        : [];
    },
  );
}

function blockElementOf(page: Document, pageBlocks: PageBlocks, number: number): Element | null {
  return page.querySelector(`[${pageBlocks.block_attribute}="${number}"]`);
}

// Writes into every placed element what the setting asks for of its block.
function writeBlocks(
  framed: FramedPage,
  pageBlocks: PageBlocks,
  blocks: Block[],
  setting: Setting,
  translations: ReadonlyMap<number, TranslatedBlock>,
): void {
  const page = framed.document;
  for (const number of pageBlocks.placed) {
    const block = blocks[number];
    const translated = translations.get(number);
    if (block === undefined) {
      continue;
    }
    if (block.kind !== "table") {
      const element = blockElementOf(page, pageBlocks, number);
      if (element !== null) {
        framed.write(element, setting, translated, () =>
          translated === undefined ? [] : translatedNodes(page, translated),
        );
      }
      continue;
    }
    const rows =
      translated === undefined
        ? []
        : translatedText(translated)
            .split(block.row_separator)
            .map((row) => row.split(block.cell_separator));
    const sources = block.text
      .split(block.row_separator)
      .map((row) => row.split(block.cell_separator));
    for (const { element, row, cell } of cellsOf(page, pageBlocks, number)) {
      const text = rows[row]?.[cell];
      // A cell that its translation leaves as it is, such as a number, is not said twice
      // beside itself; nor is one that the translation does not hold.
      const unchanged =
        text === undefined ||
        (setting === "both" && text.trim() === sources[row]?.[cell]?.trim());
      framed.write(element, setting, unchanged ? undefined : translated, () =>
        linesOf(page, text ?? ""),
      );
    }
  }
}

// An element that holds a block, and where it stands in what the frame shows. Only the
// content of such elements changes, so where one stands moves only with the elements before
// it and with its own height.
type Place = { element: Element; top: number; height: number };

function placeOf(element: Element): Place {
  const place = element.getBoundingClientRect();
  return { element, top: place.top, height: place.height };
}

// The place to keep: that of the passage pointed at, while it is in what the frame shows, and
// otherwise that of the first element that holds a block, in the page's order, that reaches
// down into it.
function placeToKeep(
  framed: FramedPage,
  pageBlocks: PageBlocks,
  pointed: Element | null,
): Place | undefined {
  const shown = framed.document.documentElement.clientHeight;
  const place = pointed === null ? undefined : placeOf(pointed);
  const inView = place !== undefined && place.top + place.height > 0 && place.top < shown;
  if (inView && place.height > 0) {
    return place;
  }
  const holders = framed.document.querySelectorAll(
    `[${pageBlocks.block_attribute}], [${pageBlocks.cell_attribute}]`,
  );
  for (const element of holders) {
    const held = placeOf(element);
    if (held.height > 0 && held.top + held.height > 0) {
      return held;
    }
  }
  return undefined;
}

// Changes the page and keeps the reader where they were: the element whose place is kept is
// put back where it stood, as large a share of it above the frame's top as before, as near as
// its changed height allows.
function keepingPlace(before: Place | undefined, framed: FramedPage, change: () => void): void {
  change();
  if (before === undefined || !before.element.isConnected) {
    return;
  }
  const share = before.height > 0 ? Math.min(1, Math.max(0, -before.top / before.height)) : 0;
  const after = placeOf(before.element);
  const shift = after.top + share * after.height - (before.top + share * before.height);
  if (Math.abs(shift) >= 1) {
    framed.view.scrollBy({ top: shift, behavior: "instant" });
  }
}

// A text as the service compares quotes, a unit at a time, with the place in the text of the
// character each unit comes from.
type Comparable = { text: string; origins: number[] };

function comparable(characters: string[]): Comparable {
  const kept: string[] = [];
  const origins: number[] = [];
  characters.forEach((character, offset) => {
    if (FORMAT_CHARACTER.test(character)) {
      return;
    }
    let plain: string;
    if (SPACE.test(character)) {
      plain = kept.length === 0 || kept.at(-1) === " " ? "" : " ";
    } else {
      plain = [...character.normalize(COMPARISON_FORM)]
        .map((part) => PLAIN_PUNCTUATION[part] ?? part)
        .join("")
        .toLowerCase();
    }
    for (const unit of plain.split("")) {
      kept.push(unit);
      origins.push(offset);
    }
  });
  return { text: kept.join(""), origins };
}

// A character the page shows in an element, and the text node and offset it stands at; a
// line break or a gap between blocks stands at none.
type Shown = { character: string; node: Text | null; offset: number };

function shownCharacters(framed: FramedPage, element: Element): Shown[] {
  const shown: Shown[] = [];
  const gap: Shown = { character: " ", node: null, offset: 0 };
  function walk(parent: Node) {
    for (const child of parent.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        if (child.parentElement?.checkVisibility() ?? false) {
          let offset = 0;
          for (const character of child.textContent ?? "") {
            shown.push({ character, node: child as Text, offset });
            offset += character.length;
          }
        }
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        const inner = child as Element;
        if (inner.matches(UNREAD_ELEMENTS)) {
          continue;
        }
        if (inner.localName === LINE_BREAK_ELEMENT) {
          shown.push({ character: LINE_BREAK, node: null, offset: 0 });
          continue;
        }
        const inline = framed.view.getComputedStyle(inner).display.startsWith(INLINE_DISPLAY);
        if (!inline) {
          shown.push(gap);
        }
        walk(inner);
        if (!inline) {
          shown.push(gap);
        }
      }
    }
  }
  walk(element);
  return shown;
}

// The quoted words in an element that holds its block's text as the page shows it: of the
// places in it that compare equal to the quote, the one nearest where the quote stands in the
// block. None where the page's words differ from the quote's.
function quotedWords(
  framed: FramedPage,
  element: Element,
  anchor: Anchor,
  block: Block,
): Range | null {
  const quote = comparable([...anchor.quote]).text.trim();
  if (quote === "") {
    return null;
  }
  const shown = shownCharacters(framed, element);
  const inPage = comparable(shown.map((character) => character.character));
  const inBlock = comparable([...block.text]);
  const expected = inBlock.origins.findIndex((origin) => origin >= anchor.start);
  let best: number | undefined;
  for (let at = inPage.text.indexOf(quote); at !== -1; at = inPage.text.indexOf(quote, at + 1)) {
    if (best === undefined || Math.abs(at - expected) < Math.abs(best - expected)) {
      best = at;
    }
  }
  if (best === undefined) {
    return null;
  }
  const first = shown[inPage.origins[best] ?? -1];
  const last = shown[inPage.origins[best + quote.length - 1] ?? -1];
  if (first?.node == null || last?.node == null) {
    return null;
  }
  const range = framed.document.createRange();
  range.setStart(first.node, first.offset);
  range.setEnd(last.node, last.offset + last.character.length);
  return range;
}

// The element that stands for a block in the page: its own, or a table's first marked cell.
function holderOf(
  page: Document,
  pageBlocks: PageBlocks,
  blocks: Block[],
  number: number,
): Element | null {
  if (!pageBlocks.placed.includes(number)) {
    return null;
  }
  return blocks[number]?.kind === "table"
    ? (cellsOf(page, pageBlocks, number)[0]?.element ?? null)
    : blockElementOf(page, pageBlocks, number);
}

// What marks a holder: the attribute and value that name it alone.
function selectorOf(pageBlocks: PageBlocks, holder: Element): string {
  const attribute = holder.hasAttribute(pageBlocks.cell_attribute)
    ? pageBlocks.cell_attribute
    : pageBlocks.block_attribute;
  return `[${attribute}="${holder.getAttribute(attribute) ?? ""}"]`;
}

type Target = { found: true; selector: string; passage: Range } | { found: false; reason: string };

// Where a passage stands in the page: the element that stands for its block, whole, or the
// quoted words where the element holds the block's text and they are found in it.
function targetOf(
  framed: FramedPage,
  pageBlocks: PageBlocks,
  blocks: Block[],
  anchor: Anchor,
): Target {
  const page = framed.document;
  const block = blocks[anchor.block_number];
  const holder = holderOf(page, pageBlocks, blocks, anchor.block_number);
  if (block === undefined || holder === null) {
    return { found: false, reason: UNPLACED };
  }
  const words =
    block.kind !== "table" && framed.holdsSource(holder)
      ? quotedWords(framed, holder, anchor, block)
      : null;
  const whole = page.createRange();
  whole.selectNodeContents(holder);
  return { found: true, selector: selectorOf(pageBlocks, holder), passage: words ?? whole };
}

function paddingOf(computed: string): number {
  const pixels = parseFloat(computed);
  return Number.isNaN(pixels) ? 0 : pixels;
}

// The part of the frame, in the frame's own coordinates, that the window shows the reader: not
// what lies above the reader's top or under the column held at the foot of the window, which
// the scroll padding of the reader's scroller sets aside.
function roomInFrame(frame: HTMLIFrameElement): { top: number; bottom: number } {
  const scroller = scrollerOf(frame);
  const view = scroller?.getBoundingClientRect() ?? new DOMRect(0, 0, innerWidth, innerHeight);
  const setAside = paddingOf(
    getComputedStyle(scroller ?? document.documentElement).scrollPaddingBottom,
  );
  const top = frame.getBoundingClientRect().top + frame.clientTop;
  const room = {
    top: Math.max(top, view.top) - top,
    bottom: Math.min(top + frame.clientHeight, view.bottom - setAside) - top,
  };
  return room.bottom - room.top < LEAST_ROOM ? { top: 0, bottom: frame.clientHeight } : room;
}

// Brings the reader's column to the top of the reader, where the frame stands whole below its
// head, then puts the passage in the middle of everything in the frame that scrolls around it,
// innermost first, and in the page itself in the middle of the room the window shows of the
// frame. A passage larger than the room is put at its start. The jumps are instant, as the
// reader's are.
function bringIntoFrame(frame: HTMLIFrameElement, framed: FramedPage, passage: Range): void {
  const main = frame.closest("main");
  if (main !== null && scrollerOf(frame) === null) {
    scrollBy({ top: main.getBoundingClientRect().top, behavior: "instant" });
  }
  const room = roomInFrame(frame);
  const page = framed.document;
  for (
    let ancestor = passage.startContainer.parentElement;
    ancestor !== null;
    ancestor = ancestor.parentElement
  ) {
    const isPage = ancestor === page.scrollingElement;
    const style = framed.view.getComputedStyle(ancestor);
    const place = passage.getBoundingClientRect();
    const view = isPage
      ? new DOMRect(0, room.top, page.documentElement.clientWidth, room.bottom - room.top)
      : ancestor.getBoundingClientRect();
    if ((isPage || scrolls(style.overflowY)) && ancestor.scrollHeight > ancestor.clientHeight) {
      const margin = Math.max(0, (view.height - place.height) / 2);
      ancestor.scrollBy({ top: place.top - view.top - margin, behavior: "instant" });
    }
    if ((isPage || scrolls(style.overflowX)) && ancestor.scrollWidth > ancestor.clientWidth) {
      const margin = Math.max(0, (view.width - place.width) / 2);
      ancestor.scrollBy({ left: place.left - view.left - margin, behavior: "instant" });
    }
  }
}

// The frame as tall as what the reader has of the window below what stands above the frame in
// the reader, measured again whenever any of it changes size.
function useFrameHeight(
  frame: RefObject<HTMLIFrameElement | null>,
  onShow: boolean,
): number | undefined {
  const [height, setHeight] = useState<number>();
  useLayoutEffect(() => {
    const element = frame.current;
    const main = element?.closest("main") ?? null;
    if (!onShow || element === null || main === null) {
      return;
    }
    function measure() {
      if (element === null || main === null) {
        return;
      }
      const scrolled = scrolls(getComputedStyle(main).overflowY);
      const above =
        element.getBoundingClientRect().top -
        main.getBoundingClientRect().top +
        (scrolled ? main.scrollTop : 0);
      const view = scrolled ? main.clientHeight : innerHeight;
      const border = element.offsetHeight - element.clientHeight;
      setHeight(Math.max(SHORTEST_FRAME, Math.floor(view - above - border)));
    }
    measure();
    const resized = new ResizeObserver(measure);
    resized.observe(main);
    for (const child of main.children) {
      resized.observe(child);
    }
    addEventListener("resize", measure);
    return () => {
      resized.disconnect();
      removeEventListener("resize", measure);
    };
  }, [frame, onShow]);
  return height;
}

function UnplacedNote({
  total,
  placed,
  setting,
}: {
  total: number;
  placed: number;
  setting: Setting;
}) {
  const unplaced = total - placed;
  if (unplaced === 0) {
    return <p className="view-note">本文 {total} 段在原件里都有自己的位置。</p>;
  }
  return (
    <p className="view-note">
      本文 {total} 段中有 {unplaced} 段在原件里没有单独的位置，按网页原样显示
      {setting !== "source" && "，不换成译文"}；「文本」里有全部 {total} 段。
    </p>
  );
}

// A web article as the page it was read from, kept and inert, in the frame: in its source, its
// translation in the place of each block's text, or each block followed by its translation. A
// passage pointed at is marked and brought into view in the frame, and one whose block has no
// element of its own in the page is said to be lost.
export function WebpageView({
  entry,
  itemDocument,
  setting,
  translation,
  pointed,
  onShow,
  onLost,
}: ViewContext) {
  const pageBlocks = useLoaded(() => readPageBlocks(entry.id), entry.id);
  const frame = useRef<HTMLIFrameElement>(null);
  const [framed, setFramed] = useState<FramedPage>();
  const [failure, setFailure] = useState<string>();
  const height = useFrameHeight(frame, onShow && pageBlocks.state === "ready");
  const link = pageLink(entry.id);
  const blocks = itemDocument.blocks;
  // The act of pointing last brought into view, so that it is brought into view once, and
  // again when the view comes on show again.
  const shownPointing = useRef<Pointed>(undefined);

  function loaded() {
    const element = frame.current;
    if (element === null) {
      return;
    }
    const page = element.contentDocument;
    const view = element.contentWindow;
    // Nothing in the page may put another in its place; were it ever to, the page is put
    // back.
    if (page === null || view === null || page.location.pathname !== link) {
      setFramed(undefined);
      element.src = link;
      return;
    }
    if (page.contentType !== "text/html") {
      setFailure(PAGE_UNREADABLE);
      return;
    }
    openLinksElsewhere(page);
    setFramed(new FramedPage(page, view as Window & typeof globalThis));
  }

  const ready = pageBlocks.state === "ready" ? pageBlocks.value : undefined;
  const failed = pageBlocks.state === "failed" ? pageBlocks.reason : failure;

  useLayoutEffect(() => {
    if (framed !== undefined && ready !== undefined && onShow) {
      const holder =
        pointed === undefined
          ? null
          : holderOf(framed.document, ready, blocks, pointed.anchor.block_number);
      keepingPlace(placeToKeep(framed, ready, holder), framed, () =>
        writeBlocks(framed, ready, blocks, setting, translation.blocks),
      );
    }
  }, [framed, ready, blocks, setting, translation.blocks, onShow, pointed]);

  useLayoutEffect(() => {
    const first = shownPointing.current !== pointed;
    if (!onShow || pointed === undefined) {
      framed?.unmark();
      shownPointing.current = undefined;
      return;
    }
    if (failed !== undefined) {
      if (first) {
        onLost(`原件打不开：${failed}`);
        shownPointing.current = pointed;
      }
      return;
    }
    if (framed === undefined || ready === undefined || frame.current === null) {
      return;
    }
    const target = targetOf(framed, ready, blocks, pointed.anchor);
    if (!target.found) {
      framed.unmark();
      if (first) {
        onLost(target.reason);
      }
    } else {
      framed.mark(target.selector, target.passage);
      if (first) {
        bringIntoFrame(frame.current, framed, target.passage);
      }
    }
    shownPointing.current = pointed;
  }, [framed, ready, failed, blocks, pointed, onShow, setting, translation.blocks]);

  if (failed !== undefined) {
    return (
      <p className="status failed webpage-failed">
        原件打不开：{failed}。「文本」里有这一篇的全部内容。
      </p>
    );
  }
  if (ready === undefined) {
    return <p className="status webpage-loading">正在打开原件…</p>;
  }
  return (
    <div className="webpage">
      <UnplacedNote total={blocks.length} placed={ready.placed.length} setting={setting} />
      <iframe
        ref={frame}
        className="webpage-frame"
        src={link}
        sandbox="allow-same-origin"
        title="原件"
        style={{ height }}
        onLoad={loaded}
      />
    </div>
  );
}
