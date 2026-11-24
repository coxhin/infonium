import { useMemo, useRef } from "react";
import { layoutOf, placedOf, type AreaText } from "./areas";
import { sourceLink, type PagePlace, type TranslatedBlock } from "./client";
import { NO_MARKING } from "./marking";
import { PageViewer, type PageMarks } from "./pages";
import { linesOf, markedBlocksOf, pageMarksOf, type PageMark } from "./pagemarks";
import { pagePlaces, placesOf } from "./places";
import { SEGMENT_GAP } from "./translated";
import type { ViewContext } from "./views";

const NO_PAGES: PagePlace[] = [];
const NO_CHARACTERS: string[] = [];

// Where each block's translation is set is worked out once for each translation of it.
const placedKept = new WeakMap<TranslatedBlock, AreaText[]>();

function sameMarks(one: PageMark[], other: PageMark[]): boolean {
  return one.length === other.length && one.every((mark, index) => mark.key === other[index]?.key);
}

// A PDF as its pages. With a translation on show, each translated block is set over the lines
// it stands for, and in 对照 the source stands beside each page, or under each area where
// there is no room for two. The reader's highlights are drawn on the lines they cover, and the
// reader selects by dragging over the lines. The boxes of a passage pointed at are outlined
// above it all.
export function PagesView({
  entry,
  itemDocument,
  setting,
  translation,
  pointed,
  marking,
  onShow,
}: ViewContext) {
  // Worked out anew at every act of pointing, so that the pages turn to the same boxes again.
  const boxes = useMemo(
    () =>
      pointed === undefined
        ? NO_PAGES
        : pointed.anchors.flatMap((anchor) => pagePlaces(placesOf(itemDocument.blocks, anchor))),
    [pointed, itemDocument],
  );
  const pointedBlocks = useMemo(
    () => new Set(pointed?.anchors.map((anchor) => anchor.block_number)),
    [pointed],
  );
  const layout = useMemo(() => layoutOf(itemDocument.blocks), [itemDocument]);
  const lines = useMemo(() => linesOf(itemDocument.blocks), [itemDocument]);
  // The text of each block as code points, as offsets count them.
  const characters = useMemo(
    () => itemDocument.blocks.map((block) => [...block.text]),
    [itemDocument],
  );
  // A page whose highlights are drawn as they were keeps them, so that marking one passage
  // more draws again only the pages it lies on.
  const kept = useRef(new Map<number, PageMark[]>());
  const marks = useMemo<PageMarks>(() => {
    const onPages = pageMarksOf(lines, marking.marked, marking.opened);
    onPages.forEach((pageMarks, page) => {
      const before = kept.current.get(page);
      if (before !== undefined && sameMarks(before, pageMarks)) {
        onPages.set(page, before);
      }
    });
    kept.current = onPages;
    return {
      onPages,
      blocks: markedBlocksOf(marking.marked, marking.opened),
      selecting:
        marking === NO_MARKING
          ? undefined
          : {
              lines,
              charactersOf: (blockNumber) => characters[blockNumber] ?? NO_CHARACTERS,
              documentId: itemDocument.id,
              marked: marking.marked,
              pageMarks: onPages,
              onSelect: marking.onSelect,
              onPress: marking.onPress,
            },
    };
  }, [lines, characters, marking, itemDocument.id]);
  const shown = setting !== "source";
  const overlays = useMemo(() => {
    if (!shown) {
      return undefined;
    }
    const onPages = new Map<number, AreaText[]>();
    translation.blocks.forEach((translated, blockNumber) => {
      const block = itemDocument.blocks[blockNumber];
      if (block === undefined) {
        return;
      }
      let placed = placedKept.get(translated);
      if (placed === undefined) {
        placed = placedOf(block, blockNumber, translated, layout, SEGMENT_GAP);
        placedKept.set(translated, placed);
      }
      for (const text of placed) {
        const onPage = onPages.get(text.area.page) ?? [];
        onPage.push(text);
        onPages.set(text.area.page, onPage);
      }
    });
    return onPages;
  }, [shown, translation.blocks, itemDocument, layout]);
  const hasTables = itemDocument.blocks.some((block) => block.kind === "table");
  return (
    <>
      {shown && hasTables && (
        <p className="pages-note">页面上的表格仍是原文；表格的译文在「文本」里。</p>
      )}
      <PageViewer
        source={sourceLink(entry.id)}
        pointed={boxes}
        pointedBlocks={pointedBlocks}
        inView={onShow}
        overlays={overlays}
        pairing={setting === "both"}
        marks={marks}
      />
    </>
  );
}
