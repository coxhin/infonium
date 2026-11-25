import { memo, useEffect, useRef, type RefObject } from "react";
import { percent, pieceStarts, type AreaText } from "./areas";
import { unitOffset, unitOffsets } from "./blocktext";
import { glowAlso, glowOver } from "./glowing";
import type { PageText, Span } from "./pagelines";
import type { Stretch } from "./sentences";
import { rangeIn, textOf } from "./textpieces";
import "./pageglow.css";

// A passage pointed at, glowing for a moment on a PDF's pages: over the words it holds on the
// source's lines, and over the sentences of a translation set over the pages that stand for
// them. Each act of pointing glows anew.

// Where a part of a block's translation begins in the whole of it, in code points.
type PlacedPiece = { text: string; start: number };

// The stretch of one block's translation that stands for the passage, and the parts of that
// translation the block's areas hold.
export type TranslatedGlow = { blockNumber: number; stretch: Stretch; pieces: PlacedPiece[] };

export type Glow = { key: number; spans: Span[]; translated: TranslatedGlow[] };

// How long after an act of pointing the areas of a translation are waited for, in
// milliseconds: a page far from where the reader stood is drawn, and its translation set,
// after the pages have turned to it. The glow lasts 1,900 ms.
const AREAS_AWAITED = 1500;
const TRANSLATED_AREAS = ".page[data-translated] .area[data-block-translated]";

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
// a translation set over the page, where only lines no area covers show it. Drawn again when
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
  const boxes = glow.spans
    .flatMap((span) => text.coveredBoxes(span))
    .filter((covered) => covered.page === page);
  if (boxes.length === 0) {
    return null;
  }
  return (
    <div key={glow.key} className={under ? "page-glow under" : "page-glow"}>
      {boxes.map(({ box }, index) => (
        <div
          key={index}
          style={{
            left: percent(box.left),
            top: percent(box.top),
            width: percent(box.width),
            height: percent(box.height),
          }}
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
// hold them, on translated pages, at every act of pointing while the pages are on show. Areas
// that are set after the glow began, on a page drawn late, glow with it.
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
    const done = new Set<Element>();
    let begun = false;
    const light = () => {
      const ranges: Range[] = [];
      for (const area of root.querySelectorAll<HTMLElement>(TRANSLATED_AREAS)) {
        const each = blocks.get(area.dataset.blockTranslated ?? "");
        if (each !== undefined && !done.has(area)) {
          done.add(area);
          ranges.push(...areaRanges(area, each));
        }
      }
      if (ranges.length > 0) {
        if (begun) {
          glowAlso(ranges);
        } else {
          begun = true;
          glowOver(ranges);
        }
      }
    };
    light();
    const watcher = new MutationObserver(light);
    watcher.observe(root, { childList: true, subtree: true });
    const stop = window.setTimeout(() => watcher.disconnect(), AREAS_AWAITED);
    return () => {
      watcher.disconnect();
      window.clearTimeout(stop);
    };
  }, [viewer, key, inView]);
}
