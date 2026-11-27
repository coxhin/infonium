import { memo, useEffect, useRef, type CSSProperties, type RefObject } from "react";
import { percent, pieceStarts, type AreaText } from "./areas";
import { unitOffset, unitOffsets } from "./blocktext";
import {
  drawingReaches,
  FADE_EASING,
  GLOW_FADE,
  GLOW_LASTS,
  GLOW_WHOLE,
  glowOver,
  relight,
} from "./glowing";
import type { PageBox, PageText, Span } from "./pagelines";
import type { Stretch } from "./sentences";
import { rangeIn, textOf } from "./textpieces";
import "./pageglow.css";

// A passage pointed at, glowing for a moment on a PDF's pages: over the words it holds on the
// source's lines, and over the sentences of a translation set over the pages that stand for
// them. Each act of pointing glows anew, drawn over the passage in the order it is read.

// Where a part of a block's translation begins in the whole of it, in code points.
type PlacedPiece = { text: string; start: number };

// The stretch of one block's translation that stands for the passage, and the parts of that
// translation the block's areas hold.
export type TranslatedGlow = { blockNumber: number; stretch: Stretch; pieces: PlacedPiece[] };

export type Glow = { key: number; spans: Span[]; translated: TranslatedGlow[] };

const TRANSLATED_AREAS = ".page[data-translated] .area[data-block-translated]";
// The times of a glow, for the boxes of a page to fade by as words do.
const GLOW_TIMES = {
  "--glow-whole": `${GLOW_WHOLE}ms`,
  "--glow-fade": `${GLOW_FADE}ms`,
  "--glow-fade-easing": FADE_EASING,
} as CSSProperties;

// A box of the passage on a page, and when the glow is drawn over it, in milliseconds since it
// began: the boxes take their shares of the drawing by their lengths along their lines, in
// their order, as words take theirs by their widths (glowing.ts).
type DrawnBox = PageBox & { from: number; until: number };

function drawnBoxes(boxes: PageBox[]): DrawnBox[] {
  const lengths = boxes.map(({ box, downward }) => (downward ? box.height : box.width));
  const whole = lengths.reduce((sum, length) => sum + length, 0);
  let before = 0;
  return boxes.map((covered, index) => {
    const from = whole > 0 ? before / whole : 0;
    before += lengths[index] ?? 0;
    const until = whole > 0 ? before / whole : 1;
    return { ...covered, from: drawingReaches(from), until: drawingReaches(until) };
  });
}

// Where each part of a block's translation that holds any of it begins in the whole of it.
export function placedPieces(whole: string, areas: AreaText[]): PlacedPiece[] {
  const starts = pieceStarts(whole, areas);
  return areas.flatMap(({ piece }, index) => {
    const start = starts[index];
    return start === undefined ? [] : [{ text: piece.text, start }];
  });
}

// Whether the passage runs over a line of this page.
export function glowsOn(glow: Glow | undefined, text: PageText, page: number): boolean {
  return (
    glow !== undefined &&
    glow.spans.some((span) =>
      (text.ofBlocks.get(span.blockNumber) ?? []).some(
        (line) => line.place.page === page && line.start < span.end && span.start < line.end,
      ),
    )
  );
}

// The passage's words on one page's lines, glowing for a moment: over the source, and under
// a translation set over the page, where only lines no flow covers show it. Drawn again when
// the page's lines are cut anew, without starting the glow again.
export const PageGlow = memo(function PageGlow({
  glow,
  text,
  page,
  under,
}: {
  glow: Glow | undefined;
  text: PageText;
  page: number;
  // How many times the lines on the page were cut anew.
  drawings: number;
  under: boolean;
}) {
  if (glow === undefined) {
    return null;
  }
  const boxes = drawnBoxes(glow.spans.flatMap((span) => text.coveredBoxes(span))).filter(
    (covered) => covered.page === page,
  );
  if (boxes.length === 0) {
    return null;
  }
  return (
    <div key={glow.key} className={under ? "page-glow under" : "page-glow"} style={GLOW_TIMES}>
      {boxes.map(({ box, downward, from, until }, index) => (
        <div
          key={index}
          data-downward={downward ? "" : undefined}
          style={
            {
              left: percent(box.left),
              top: percent(box.top),
              width: percent(box.width),
              height: percent(box.height),
              "--drawn-from": `${from}ms`,
              "--drawn-for": `${Math.max(until - from, 1)}ms`,
            } as CSSProperties
          }
        />
      ))}
    </div>
  );
});

// The ranges of an area that show the part of the passage's translation it holds. An area is
// told by its text, which is a part of the block's translation after the marker of a list
// item; one whose text is no part the reader knows of glows whole.
function areaRanges(area: Element, glow: TranslatedGlow): Range[] {
  const text = textOf(area);
  const piece = glow.pieces
    .filter((each) => text.endsWith(each.text))
    .sort((one, other) => other.text.length - one.text.length)[0];
  if (piece === undefined) {
    const whole = document.createRange();
    whole.selectNodeContents(area);
    return [whole];
  }
  const before = text.length - piece.text.length;
  const length = [...piece.text].length;
  const start = Math.max(glow.stretch.start, piece.start) - piece.start;
  const end = Math.min(glow.stretch.end, piece.start + length) - piece.start;
  if (start >= end) {
    return [];
  }
  const offsets = unitOffsets(piece.text);
  return [rangeIn(area, before + unitOffset(offsets, start), before + unitOffset(offsets, end))];
}

// Makes the sentences of the translation that stand for the passage glow in the areas that
// hold them, on translated pages, at every act of pointing while the pages are on show. For as
// long as the glow lasts, the areas are watched: one set after the glow began, on a page drawn
// late, glows with it, and one whose text is drawn anew, as when the links of its page arrive,
// glows again over its new text.
export function useTranslatedGlow(
  viewer: RefObject<HTMLElement | null>,
  glow: Glow | undefined,
  inView: boolean,
): void {
  const latest = useRef(glow);
  latest.current = glow;
  const key = glow?.key;
  useEffect(() => {
    const root = viewer.current;
    const current = latest.current;
    if (!inView || root === null || current === undefined || current.translated.length === 0) {
      return;
    }
    const blocks = new Map(current.translated.map((each) => [String(each.blockNumber), each]));
    let begun = false;
    const light = () => {
      const ranges = [...root.querySelectorAll<HTMLElement>(TRANSLATED_AREAS)].flatMap((area) => {
        const each = blocks.get(area.dataset.blockTranslated ?? "");
        return each === undefined ? [] : areaRanges(area, each);
      });
      if (begun) {
        relight(ranges);
      } else if (ranges.length > 0) {
        begun = true;
        glowOver(ranges);
      }
    };
    light();
    const watcher = new MutationObserver(light);
    watcher.observe(root, { childList: true, subtree: true, characterData: true });
    const stop = window.setTimeout(() => watcher.disconnect(), GLOW_LASTS);
    return () => {
      watcher.disconnect();
      window.clearTimeout(stop);
    };
  }, [viewer, key, inView]);
}
