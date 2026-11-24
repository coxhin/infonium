import type { Anchor, Block } from "./client";
import { heldTextOf } from "./framewords";
import type { Marked } from "./marking";
import { linkOf, selectorOf, type FramedPage, type Holders } from "./pagewriter";

// The reader's highlights as drawn in the framed page a web article was read from, and which
// of them a press falls on.

// A highlight is drawn in the green the app's views share (the light values of `--marked`,
// `--opened` and `--marked-line` in styles.css, whose variables do not reach the page), the
// same on any page, light or dark: its words dark on that green, so that they read on a dark
// page as on a white one, and those of the one open in the notebook on the deeper green; one
// with a comment is underlined. Where its words are not drawn, its element is tinted whole,
// more quietly, in the same green made translucent, which lets the page's own colours and
// words through: light on a white page, dark on a dark one. Each look is a highlight of its
// own in the page's registry, so that where two overlap the one that says more is drawn: the
// open one over the others, exact words over a tint, and the passage pointed at, whose
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
const WORDS_BACKGROUND = "#cdeccb";
const OPENED_WORDS_BACKGROUND = "#86cf80";
const WORDS_UNDERLINE = "#2f7d32";
// The opened green, translucent: over white about the shade of the words' green, over a dark
// page a dark green under which light words still read.
const WHOLE_BACKGROUND = "rgb(134 207 128 / 0.35)";
const OPENED_WHOLE_BACKGROUND = "rgb(134 207 128 / 0.5)";
// Between the two greens of `--marked-line`, so that it shows on a white page and a dark one.
const WHOLE_UNDERLINE = "#5fa85b";
const UNDERLINE_THICKNESS = "2px";
const IMPORTANT = " !important";
// The names of the looks end in letters drawn anew for each page, so that no rule the page's
// own stylesheet holds can name them and change how they look.
const NAME_RADIX = 36;
const NAME_LETTERS = 10;

function nameOf({ extent, opened, commented }: Look, suffix: string): string {
  const open = opened ? "opened" : "kept";
  const noted = commented ? "commented" : "bare";
  return `${HIGHLIGHT_PREFIX}-${extent}-${open}-${noted}-${suffix}`;
}

// Exact words over a tint, the open highlight over the others, a comment over none: each
// weighs more than both after it together.
function priorityOf({ extent, opened, commented }: Look): number {
  return (extent === "words" ? 4 : 0) + (opened ? 2 : 0) + (commented ? 1 : 0);
}

// How a highlight looks, as declarations that a highlight of the registry and a rule of the
// page's stylesheet both take, standing above the page's own.
function declarationsOf({ extent, opened, commented }: Look): string {
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
    `background-color: ${background}${IMPORTANT};`,
    ...(words ? [`color: ${WORDS_INK}${IMPORTANT};`] : []),
    ...(commented
      ? [`text-decoration: underline ${underline} ${UNDERLINE_THICKNESS}${IMPORTANT};`]
      : []),
  ].join(" ");
}

function contentsOf(element: Element): Range {
  const range = element.ownerDocument.createRange();
  range.selectNodeContents(element);
  return range;
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

// A highlight as drawn: where in the page, in which element, and where its passage begins in
// the document, by which the one that begins last is told among several under a press.
type Drawn = { id: string; holder: Element; range: Range; begins: Anchor };

export class FrameHighlights {
  readonly framed: FramedPage;
  readonly suffix = Math.random()
    .toString(NAME_RADIX)
    .slice(2, 2 + NAME_LETTERS);
  // The rules that tint elements whole where the page's browser has no highlight registry.
  readonly wholeRules: CSSStyleSheet;
  drawn: Drawn[] = [];

  constructor(framed: FramedPage) {
    this.framed = framed;
    const view = framed.view;
    const style = new view.CSSStyleSheet();
    style.replaceSync(
      LOOKS.map(
        (look) => `::highlight(${nameOf(look, this.suffix)}) { ${declarationsOf(look)} }`,
      ).join("\n"),
    );
    this.wholeRules = new view.CSSStyleSheet();
    // Before infonium's other sheets, so that the outline of the passage pointed at, which
    // the last of them draws, is drawn over a tint.
    const page = framed.document;
    page.adoptedStyleSheets = [style, this.wholeRules, ...page.adoptedStyleSheets];
  }

  // Draws the highlights, each anchor in the elements of its block that the page has.
  draw(holders: Holders, blocks: Block[], marked: Marked[], opened: string | undefined): void {
    const framed = this.framed;
    const registry = framed.marksWords();
    const ranges = new Map<string, Range[]>();
    const tinted = new Map<string, { look: Look; selectors: string[] }>();
    const drawn: Drawn[] = [];
    for (const highlight of marked) {
      for (const anchor of highlight.anchors) {
        const block = blocks[anchor.block_number];
        for (const element of holders.ofBlock.get(anchor.block_number) ?? []) {
          const place = holders.places.get(element);
          const held =
            block === undefined || place === undefined ? undefined : heldTextOf(block, place);
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
              ? framed.wordsOf(element, held.text).wordsOf(span)
              : null;
          const look: Look = {
            extent: words === null ? "whole" : "words",
            opened: highlight.id === opened,
            commented: highlight.commented,
          };
          const range = words ?? contentsOf(element);
          const name = nameOf(look, this.suffix);
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
    }
    if (registry) {
      const view = framed.view;
      for (const look of LOOKS) {
        const name = nameOf(look, this.suffix);
        const highlight = new view.Highlight(...(ranges.get(name) ?? []));
        highlight.priority = priorityOf(look);
        view.CSS.highlights.set(name, highlight);
      }
    }
    this.wholeRules.replaceSync(
      [...tinted.values()]
        .map(({ look, selectors }) => `${selectors.join(", ")} { ${declarationsOf(look)} }`)
        .join("\n"),
    );
    this.drawn = drawn;
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
}
