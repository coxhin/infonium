import { unitOffset, unitOffsets } from "./blocks";
import type { Block, Mark, PageBlocks, Style, TranslatedBlock } from "./client";
import type { Setting } from "./setting";

// What infonium adds to the page goes in an element of a name of its own: the service takes
// every element of such a name out of the page before it marks the blocks, so none is the
// page's. Unknown to the page's stylesheet, it is styled by infonium's alone.
const TRANSLATION_ELEMENT = "infonium-translation";
const POINTED_HIGHLIGHT = "infonium-pointed";
// The same elements as the reader's text uses for the marks, in blocks.tsx, of which this is a
// copy until the two are joined.
const STYLE_ELEMENTS: Record<Style, string> = {
  strong: "strong",
  emphasis: "em",
  underline: "u",
  code: "code",
  subscript: "sub",
  superscript: "sup",
};
const LINK_ELEMENT = "a";
const AREA_ELEMENT = "area";
const FORMULA_ELEMENT = "code";
const LINE_BREAK_ELEMENT = "br";
const LINE_BREAK = "\n";
// Segments of one block follow each other as sentences of one paragraph do.
const SEGMENT_GAP = " ";
const CELL_PLACE_SEPARATOR = ",";
const LINK_ADDRESS = "href";
const HTML_NAMESPACE = "http://www.w3.org/1999/xhtml";
const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
const XLINK_NAMESPACE = "http://www.w3.org/1999/xlink";
// Only an address of the web is opened from the page, or written into it.
const WEB_PROTOCOLS = ["http:", "https:"];
const NEW_TAB = "_blank";
const NEW_TAB_FEATURES = "noopener,noreferrer";
const MIDDLE_BUTTON = 1;
// A fragment that names no element of the page leads to its top, as in a browser.
const TOP_FRAGMENT = "top";
const PERCENT_ESCAPE = /^%([0-9a-f]{2})$/i;
const ESCAPE_LENGTH = 3;
const HEXADECIMAL = 16;
const INLINE_DISPLAY = "inline";

// A passage pointed at is marked in colours of its own, the same on any page, light or dark:
// an outline in two colours that shows against either, and its words dark on yellow. The
// outline goes round an element laid out as a block; round one laid out inline, as a run of
// words inside a larger element is, it would be drawn round each line apart, so there the
// words alone mark it, unless the browser cannot mark words.
const POINTED_OUTLINE =
  "outline: 3px solid #f2a900 !important; outline-offset: 2px !important;" +
  " box-shadow: 0 0 0 5px rgb(0 0 0 / 0.45) !important;";
// The translation beside its source is set quieter in the page's own colours and type: a rule
// of the text's own colour before it, the words faded. The browser's own anchoring of the
// scroll is left to infonium, which keeps the place as it changes the page.
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

// Where a passage stands in the page: the element that stands for its block, what to mark of
// it, and the attribute and value that name the element alone.
export type PointedPlace = { holder: Element; selector: string; passage: Range };

// The page in the frame, as it was served and as far as infonium has changed it: the elements
// whose content holds a translation now, each with the nodes it held as served, put back as
// they were when the page is shown in its source again.
export class FramedPage {
  readonly document: Document;
  readonly view: Window & typeof globalThis;
  readonly pointedRule: CSSStyleSheet;
  readonly served = new Map<Element, Node[]>();
  readonly written = new Map<Element, { setting: Setting; translated: TranslatedBlock }>();
  marked = false;

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

  marksWords(): boolean {
    return "highlights" in this.view.CSS && typeof this.view.Highlight === "function";
  }

  mark({ holder, selector, passage }: PointedPlace): void {
    const words = this.marksWords();
    const inline = this.view.getComputedStyle(holder).display.startsWith(INLINE_DISPLAY);
    this.pointedRule.replaceSync(inline && words ? "" : `${selector} { ${POINTED_OUTLINE} }`);
    if (words) {
      this.view.CSS.highlights.set(POINTED_HIGHLIGHT, new this.view.Highlight(passage));
    }
    this.marked = true;
  }

  unmark(): void {
    if (!this.marked) {
      return;
    }
    this.pointedRule.replaceSync("");
    if (this.marksWords()) {
      this.view.CSS.highlights.delete(POINTED_HIGHLIGHT);
    }
    this.marked = false;
  }
}

function webAddress(raw: string, base: string): URL | null {
  if (!URL.canParse(raw, base)) {
    return null;
  }
  const address = new URL(raw, base);
  return WEB_PROTOCOLS.includes(address.protocol) ? address : null;
}

function isElement(target: EventTarget): target is Element {
  return "nodeType" in target && (target as Node).nodeType === Node.ELEMENT_NODE;
}

// The address of a link of HTML or of SVG, as the page wrote it.
function linkAddress(element: Element): string | null {
  const name = element.localName;
  const namespace = element.namespaceURI;
  if (namespace === HTML_NAMESPACE && (name === LINK_ELEMENT || name === AREA_ELEMENT)) {
    return element.getAttribute(LINK_ADDRESS);
  }
  if (namespace === SVG_NAMESPACE && name === LINK_ELEMENT) {
    return (
      element.getAttribute(LINK_ADDRESS) ?? element.getAttributeNS(XLINK_NAMESPACE, LINK_ADDRESS)
    );
  }
  return null;
}

// The link an event happened in, wherever in the page it stands, a shadow tree included.
function linkOf(event: Event): string | null {
  for (const target of event.composedPath()) {
    if (isElement(target)) {
      const address = linkAddress(target);
      if (address !== null) {
        return address;
      }
    }
  }
  return null;
}

function withoutFragment(address: URL): string {
  return address.href.slice(0, address.href.length - address.hash.length);
}

// A fragment's name as a browser decodes it to find its place: each escape a byte, the bytes
// read as UTF-8, a byte that is not part of a character read as a replacement character, so
// that a fragment written wrongly names nothing instead of failing.
function decodedFragment(fragment: string): string {
  const bytes: number[] = [];
  const encoder = new TextEncoder();
  for (let at = 0; at < fragment.length; ) {
    const escape = PERCENT_ESCAPE.exec(fragment.slice(at, at + ESCAPE_LENGTH));
    if (escape?.[1] !== undefined) {
      bytes.push(parseInt(escape[1], HEXADECIMAL));
      at += ESCAPE_LENGTH;
    } else {
      const character = String.fromCodePoint(fragment.codePointAt(at) ?? 0);
      bytes.push(...encoder.encode(character));
      at += character.length;
    }
  }
  return new TextDecoder().decode(new Uint8Array(bytes));
}

// Scrolls the page itself, and nothing around the frame, to the place a fragment names.
function scrollToFragment(page: Document, fragment: string): void {
  const name = decodedFragment(fragment);
  const place = page.getElementById(name) ?? page.getElementsByName(name)[0] ?? null;
  const scroller = page.scrollingElement;
  if (scroller === null) {
    return;
  }
  if (place === null) {
    if (name === "" || name.toLowerCase() === TOP_FRAGMENT) {
      scroller.scrollTo({ top: 0, behavior: "instant" });
    }
    return;
  }
  scroller.scrollBy({ top: place.getBoundingClientRect().top, behavior: "instant" });
}

// Nothing in the page is let to load in the frame: a click on a link, by any button, is taken
// from the page. A link to a place in the page itself scrolls the frame there; one to the web
// opens in a tab of its own, unless it leads to infonium itself; any other does nothing. A
// form is never sent.
export function guardLinks(page: Document): void {
  function follow(event: MouseEvent) {
    const raw = linkOf(event);
    if (raw === null) {
      return;
    }
    event.preventDefault();
    const address = webAddress(raw.trim(), page.baseURI);
    if (address === null || address.origin === location.origin) {
      return;
    }
    const here = webAddress(page.baseURI, page.baseURI);
    const inPage = here !== null && withoutFragment(address) === withoutFragment(here);
    if (inPage && address.hash !== "") {
      scrollToFragment(page, address.hash.slice(1));
    } else {
      window.open(address.href, NEW_TAB, NEW_TAB_FEATURES);
    }
  }
  page.addEventListener("click", follow, true);
  page.addEventListener(
    "auxclick",
    (event) => event.button === MIDDLE_BUTTON && follow(event),
    true,
  );
  page.addEventListener("submit", (event) => event.preventDefault(), true);
}

function linesOf(page: Document, text: string): Node[] {
  return text
    .split(LINE_BREAK)
    .flatMap((line, index) => [
      ...(index > 0 ? [page.createElement(LINE_BREAK_ELEMENT)] : []),
      ...(line === "" ? [] : [page.createTextNode(line)]),
    ]);
}

// A link of the translation leads where its mark says only when that is an address of the
// web; the service took every other address out of the page, and none is put back.
function markElement(page: Document, mark: Mark): Element {
  switch (mark.kind) {
    case "link": {
      const link = page.createElement(LINK_ELEMENT);
      const address = webAddress(mark.target, page.baseURI);
      if (address !== null) {
        link.setAttribute(LINK_ADDRESS, address.href);
      }
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
// The cutting is a copy of piecesOf in blocks.tsx until the two are joined.
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

// A table's text as rows of cells, split as TableForm in blocks.tsx splits it, of which this is
// a copy until the two are joined.
function cellTexts(block: Block & { kind: "table" }, text: string): string[][] {
  return text.split(block.row_separator).map((row) => row.split(block.cell_separator));
}

type Cell = { element: Element; row: number; cell: number };

// The cells of a table block in the page, in the page's order, each with its place in the
// block's text.
function cellsOf(page: Document, pageBlocks: PageBlocks, number: number): Cell[] {
  const attribute = pageBlocks.cell_attribute;
  const selector = `[${attribute}^="${number}${CELL_PLACE_SEPARATOR}"]`;
  return [...page.querySelectorAll(selector)].flatMap((element) => {
    const [block, row, cell] = (element.getAttribute(attribute) ?? "")
      .split(CELL_PLACE_SEPARATOR)
      .map(Number);
    return block === number && row !== undefined && cell !== undefined
      ? [{ element, row, cell }]
      : [];
  });
}

function blockElementOf(page: Document, pageBlocks: PageBlocks, number: number): Element | null {
  return page.querySelector(`[${pageBlocks.block_attribute}="${number}"]`);
}

// The element that stands for a block in the page: its own, or a table's first marked cell.
export function holderOf(
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

// The attribute and value that name a holder alone.
export function selectorOf(pageBlocks: PageBlocks, holder: Element): string {
  const attribute = holder.hasAttribute(pageBlocks.cell_attribute)
    ? pageBlocks.cell_attribute
    : pageBlocks.block_attribute;
  return `[${attribute}="${holder.getAttribute(attribute) ?? ""}"]`;
}

// Writes into every placed element what the setting asks for of its block.
export function writeBlocks(
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
    const rows = translated === undefined ? [] : cellTexts(block, translatedText(translated));
    const sources = cellTexts(block, block.text);
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
