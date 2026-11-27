import { cellTexts, cutAtMarks, STYLE_ELEMENTS } from "./blocktext";
import type { Block, Mark, PageBlocks, TranslatedBlock } from "./client";
import { FrameWords } from "./framewords";
import { FADE_EASING, Glow, GLOW_LASTS, GLOW_WHOLE, levelRules } from "./glowing";
import type { Setting } from "./setting";

// What infonium adds to the page goes in an element of a name of its own: the service takes
// every element of such a name out of the page before it marks the blocks, so none is the
// page's. Unknown to the page's stylesheet, it is styled by infonium's alone.
export const TRANSLATION_ELEMENT = "infonium-translation";
const POINTED_HIGHLIGHT = "infonium-pointed";
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
// A pointer moved further than this, in pixels, between its press and its release dragged.
const DRAG_DISTANCE = 4;
// The count of presses a click reports when a key, not a pointer, activated what it is on.
const KEY_ACTIVATION = 0;
// A fragment that names no element of the page leads to its top, as in a browser.
const TOP_FRAGMENT = "top";
const PERCENT_ESCAPE = /^%([0-9a-f]{2})$/i;
const ESCAPE_LENGTH = 3;
const HEXADECIMAL = 16;
// A passage pointed at glows in the page as it does in the reader's own views (glowing.ts),
// over the reader's highlights where they overlap. Its words keep the page's colour, and the
// glow takes the colour of the reader's own in its light theme on a page whose words are dark,
// and in its dark theme on one whose words are light (glowing.css). A colour the page gives
// its words other than in RGB is taken for a dark one.
type Tone = "light" | "dark";
const POINTED_GLOWS: Record<Tone, { channels: string; alpha: number }> = {
  light: { channels: "255 200 60", alpha: 0.42 },
  dark: { channels: "240 180 60", alpha: 0.34 },
};
const RGB = /^rgba?\((\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?)/;
// How much red, green and blue each weigh in the lightness of a colour, and the lightness,
// from 0 to 1, above which words are light.
const LIGHTNESS_WEIGHTS = [0.2126, 0.7152, 0.0722];
const CHANNEL_MOST = 255;
const LIGHT_WORDS = 0.5;

function toneOf(view: Window, element: Element): Tone {
  const found = RGB.exec(view.getComputedStyle(element).color);
  if (found === null) {
    return "light";
  }
  const lightness = found
    .slice(1)
    .reduce((sum, channel, index) => sum + (LIGHTNESS_WEIGHTS[index] ?? 0) * Number(channel), 0);
  return lightness / CHANNEL_MOST > LIGHT_WORDS ? "dark" : "light";
}

function pointedGlowRules(pointed: string): string {
  return (Object.keys(POINTED_GLOWS) as Tone[])
    .map((tone) => {
      const { channels, alpha } = POINTED_GLOWS[tone];
      return levelRules(`${pointed}-${tone}`, (strength) => {
        const shown = (alpha * strength).toFixed(3);
        return `background-color: rgb(${channels} / ${shown}) !important;`;
      });
    })
    .join("\n");
}

// Where the browser cannot mark words, the elements that hold a passage pointed at are ringed
// instead, in two colours that show against any page, and the ring fades as a glow does. Each
// act of pointing names its fading anew, so that the ring begins again.
const RING = "#f2a900";
const RING_EDGE = "0 0 0 5px rgb(0 0 0 / 0.45)";
const RING_GONE = "0 0 0 5px rgb(0 0 0 / 0)";
const PERCENT = 100;

function ringRules(selectors: string, fading: string): string {
  const whole = ((GLOW_WHOLE / GLOW_LASTS) * PERCENT).toFixed(1);
  return `
@keyframes ${fading} {
  ${whole}% {
    outline-color: ${RING};
    box-shadow: ${RING_EDGE};
    animation-timing-function: ${FADE_EASING};
  }
  to {
    outline-color: transparent;
    box-shadow: ${RING_GONE};
  }
}
${selectors} {
  outline-style: solid !important;
  outline-width: 3px !important;
  outline-offset: 2px !important;
  outline-color: ${RING};
  box-shadow: ${RING_EDGE};
  animation: ${fading} ${GLOW_LASTS}ms linear forwards !important;
}
`;
}

// The translation beside its source is set quieter in the page's own colours and type: a rule
// of the text's own colour before it, the words faded. A passage pointed at glows in
// highlights of the page's own, one for each strength of each tone. The browser's own
// anchoring of the scroll is left to infonium, which keeps the place as it changes the page. A
// link is not dragged, and its words may be selected, which a browser does not let a press in
// a link begin otherwise, so that a drag from or over one selects its words.
const frameStyle = (pointed: string) => `
${TRANSLATION_ELEMENT} {
  display: block;
  margin: 0.35em 0 0;
  padding: 0 0 0 0.6em;
  border: 0;
  border-inline-start: 2px solid;
  opacity: 0.72;
  font-size: 0.92em;
}
${pointedGlowRules(pointed)}
html, body {
  overflow-anchor: none !important;
}
a, a * {
  -webkit-user-drag: none !important;
  user-select: text !important;
}
`;

// What infonium draws in the page is named with letters drawn anew for each page, which no rule
// of the page's own stylesheet can name; and a rule of infonium's that marks elements weighs as
// much as two identifiers more than the selector it is given, so that it outranks the page's
// own rules for those elements, important as they may be.
const NAME_RADIX = 36;
const NAME_LETTERS = 10;
const OWN_NAME_PREFIX = "infonium-";

function drawnName(): string {
  return `${OWN_NAME_PREFIX}${Math.random()
    .toString(NAME_RADIX)
    .slice(2, 2 + NAME_LETTERS)}`;
}

export function outranking(selector: string, name: string): string {
  return `${selector}:not(#${name}):not(#${name})`;
}

// Where a passage stands in the page: the element that stands for its block, what to mark of
// it, and the attribute and value that name the element alone.
export type PointedPlace = { holder: Element; selector: string; passage: Range };

// What an element that stands for a block shows of it: its source, its translation in place
// of the source, or the source followed by the translation.
export type Shown = "source" | "translation" | "both";

// The page in the frame, as it was served and as far as infonium has changed it: the elements
// whose content holds a translation now, each with the nodes it held as served, put back as
// they were when the page is shown in its source again; and, of the elements whose words
// have been compared with their text, the correspondence, until their content changes.
export class FramedPage {
  readonly document: Document;
  readonly view: Window & typeof globalThis;
  readonly pointedRule: CSSStyleSheet;
  readonly served = new Map<Element, Node[]>();
  readonly written = new Map<Element, { setting: Setting; translated: TranslatedBlock }>();
  readonly words = new Map<Element, FrameWords>();
  // The letters that end the names of what infonium draws in this page.
  readonly name = drawnName();
  readonly pointedHighlight = `${POINTED_HIGHLIGHT}-${this.name}`;
  // The glow of a passage pointed at, where the browser marks words.
  readonly glow: Glow | undefined;
  marked = false;
  // How many acts of pointing have ringed elements, which names each ring's fading.
  rings = 0;

  constructor(document: Document, view: Window & typeof globalThis) {
    this.document = document;
    this.view = view;
    // Stylesheets of the frame's own realm, adopted rather than put in the page, so that the
    // page's nodes stay those it was served with.
    const own = new view.CSSStyleSheet();
    own.replaceSync(frameStyle(this.pointedHighlight));
    this.pointedRule = new view.CSSStyleSheet();
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, own, this.pointedRule];
    this.glow = this.marksWords()
      ? new Glow({ registry: view.CSS.highlights, Highlight: view.Highlight })
      : undefined;
  }

  holdsSource(element: Element): boolean {
    return !this.written.has(element);
  }

  shownIn(element: Element): Shown {
    const setting = this.written.get(element)?.setting;
    return setting === "translation" ? "translation" : setting === "both" ? "both" : "source";
  }

  // The correspondence between the source an element shows and the text it stands for.
  wordsOf(element: Element, text: string): FrameWords {
    const known = this.words.get(element);
    if (known?.text === text) {
      return known;
    }
    const compared = new FrameWords(element, text);
    this.words.set(element, compared);
    return compared;
  }

  // Forgets every correspondence, which tells what the reader sees of the page, once the page's
  // stylesheets have changed what that is.
  forgetWords(): void {
    this.words.clear();
  }

  // Puts in an element the content the setting asks for: the nodes it was served with, these
  // nodes in their place, or these nodes after them; and says whether its content changed.
  write(
    element: Element,
    setting: Setting,
    translated: TranslatedBlock | undefined,
    nodes: () => Node[],
  ): boolean {
    const before = this.written.get(element);
    const wanted = setting === "source" ? undefined : translated;
    if (before?.translated === wanted && (wanted === undefined || before?.setting === setting)) {
      return false;
    }
    const served = this.served.get(element) ?? [...element.childNodes];
    this.served.set(element, served);
    this.words.delete(element);
    if (wanted === undefined) {
      element.replaceChildren(...served);
      this.written.delete(element);
      return true;
    }
    if (setting === "translation") {
      element.replaceChildren(...nodes());
    } else {
      const beside = this.document.createElement(TRANSLATION_ELEMENT);
      beside.append(...nodes());
      element.replaceChildren(...served, beside);
    }
    this.written.set(element, { setting, translated: wanted });
    return true;
  }

  marksWords(): boolean {
    return "highlights" in this.view.CSS && typeof this.view.Highlight === "function";
  }

  // Marks the places of a passage, which glows above every other highlight of the page: a
  // passage pointed at anew glows from its start, and one marked again, as when the elements
  // that hold it are written anew, glows on as far as it has come and faded.
  mark(places: PointedPlace[], anew: boolean): void {
    const [first] = places;
    if (first === undefined) {
      this.unmark();
      return;
    }
    this.marked = true;
    if (this.glow === undefined) {
      if (anew) {
        this.rings += 1;
        const selectors = places.map(({ selector }) => outranking(selector, this.name));
        this.pointedRule.replaceSync(
          ringRules(selectors.join(", "), `${this.pointedHighlight}-ring-${this.rings}`),
        );
      }
      return;
    }
    const passages = places.map(({ passage }) => passage);
    if (anew) {
      this.glow.begin(passages, `${this.pointedHighlight}-${toneOf(this.view, first.holder)}`);
    } else {
      this.glow.relight(passages);
    }
  }

  unmark(): void {
    if (!this.marked) {
      return;
    }
    this.glow?.end();
    this.pointedRule.replaceSync("");
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
export function linkOf(event: Event): string | null {
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
// opens in a tab of its own, unless it leads to infonium itself; any other does nothing. A link
// is followed only for a press that stayed where it began, or for a key that activates it: a
// browser also fires a click at the end of a drag that began on a link, whether or not the drag
// selected anything. A form is never sent.
export function guardLinks(page: Document): void {
  let pressedAt: { x: number; y: number } | undefined;
  function follow(event: MouseEvent) {
    const raw = linkOf(event);
    if (raw === null) {
      return;
    }
    event.preventDefault();
    const pressed = pressedAt;
    pressedAt = undefined;
    const byKey = event.detail === KEY_ACTIVATION;
    const stayed =
      pressed !== undefined &&
      Math.hypot(event.clientX - pressed.x, event.clientY - pressed.y) <= DRAG_DISTANCE;
    if (!byKey && !stayed) {
      return;
    }
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
  page.addEventListener(
    "pointerdown",
    (event) => {
      pressedAt = { x: event.clientX, y: event.clientY };
    },
    { capture: true, passive: true },
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
function markedNodes(page: Document, text: string, marks: Mark[]): Node[] {
  return cutAtMarks(text, marks).flatMap((piece) =>
    piece.marks.reduce<Node[]>(
      (content, mark) => {
        const element = markElement(page, mark);
        element.append(...content);
        return [element];
      },
      linesOf(page, piece.text),
    ),
  );
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
  const selector = `[${attribute}^="${number}${CELL_PLACE_SEPARATOR}"]`;
  return [...page.querySelectorAll(selector)].flatMap((element) => {
    const place = cellPlaceOf(pageBlocks, element);
    return place?.number === number ? [{ element, row: place.row, cell: place.cell }] : [];
  });
}

// The block a cell's element stands in, and the cell's place in the block's text.
function cellPlaceOf(
  pageBlocks: PageBlocks,
  element: Element,
): { number: number; row: number; cell: number } | null {
  const [number, row, cell] = (element.getAttribute(pageBlocks.cell_attribute) ?? "")
    .split(CELL_PLACE_SEPARATOR)
    .map(Number);
  return number === undefined || row === undefined || cell === undefined
    ? null
    : { number, row, cell };
}

// What an element of the page stands for: a block whole, or a cell of a table block; and its
// place among those elements, in the page's order.
export type HolderPlace = {
  number: number;
  cell: { row: number; cell: number } | undefined;
  order: number;
};

// The elements of the page that stand for blocks or for cells, found once, since the page never
// changes which elements those are: all of them in the page's order, what each stands for, and
// each block's elements.
export type Holders = {
  pageBlocks: PageBlocks;
  selector: string;
  ordered: Element[];
  places: Map<Element, HolderPlace>;
  ofBlock: Map<number, Element[]>;
};

export function holdersOf(page: Document, pageBlocks: PageBlocks): Holders {
  const selector = `[${pageBlocks.block_attribute}], [${pageBlocks.cell_attribute}]`;
  const ordered: Element[] = [];
  const places = new Map<Element, HolderPlace>();
  const ofBlock = new Map<number, Element[]>();
  for (const element of page.querySelectorAll(selector)) {
    const cell = element.hasAttribute(pageBlocks.cell_attribute)
      ? cellPlaceOf(pageBlocks, element)
      : null;
    const number = cell?.number ?? Number(element.getAttribute(pageBlocks.block_attribute));
    if (!Number.isInteger(number)) {
      continue;
    }
    places.set(element, {
      number,
      cell: cell === null ? undefined : { row: cell.row, cell: cell.cell },
      order: ordered.length,
    });
    ordered.push(element);
    const elements = ofBlock.get(number) ?? [];
    elements.push(element);
    ofBlock.set(number, elements);
  }
  return { pageBlocks, selector, ordered, places, ofBlock };
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

// Writes into every placed element what the setting asks for of its block, and returns the
// elements whose content changed.
export function writeBlocks(
  framed: FramedPage,
  pageBlocks: PageBlocks,
  blocks: Block[],
  setting: Setting,
  translations: ReadonlyMap<number, TranslatedBlock>,
): Element[] {
  const page = framed.document;
  const changed: Element[] = [];
  for (const number of pageBlocks.placed) {
    const block = blocks[number];
    const translated = translations.get(number);
    if (block === undefined) {
      continue;
    }
    if (block.kind !== "table") {
      const element = blockElementOf(page, pageBlocks, number);
      const wrote =
        element !== null &&
        framed.write(element, setting, translated, () =>
          translated === undefined ? [] : translatedNodes(page, translated),
        );
      if (wrote) {
        changed.push(element);
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
      if (
        framed.write(element, setting, unchanged ? undefined : translated, () =>
          linesOf(page, text ?? ""),
        )
      ) {
        changed.push(element);
      }
    }
  }
  return changed;
}
