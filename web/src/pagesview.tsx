import { useMemo } from "react";
import { layoutOf, placedOf, translatedText, writtenUnspaced, type AreaText } from "./areas";
import { useArrivalPosition } from "./arrival";
import { sourceLink, type PagePlace, type TranslatedBlock } from "./client";
import { NO_MARKING } from "./marking";
import { PageViewer, type Overlays, type PageMarks } from "./pages";
import { PageText } from "./pagelines";
import { markedBlocksOf, pageMarksOf } from "./pagemarking";
import { pagePlaces, placesOf } from "./places";
import { SEGMENT_GAP } from "./translated";
import type { ViewContext } from "./views";

const NO_PAGES: PagePlace[] = [];

// Where each block's translation is set is worked out once for each translation of it.
const placedKept = new WeakMap<TranslatedBlock, AreaText[]>();

// A PDF as its pages. With a translation on show, each translated block is set over the lines
// it stands for, and in 对照 the source stands beside each page, or under each flow where
// there is no room for two. The reader's highlights are drawn on the lines they cover, and the
// reader selects by dragging over the lines. The boxes of a passage pointed at are outlined
// above it all. The pages open where the reader stood in the file as it arrived, if they did.
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
  const text = useMemo(() => new PageText(itemDocument.blocks), [itemDocument]);
  const marks = useMemo<PageMarks>(() => {
    const onPages = pageMarksOf(text, marking.marked, marking.opened);
    return {
      text,
      onPages,
      blocks: markedBlocksOf(marking.marked, marking.opened),
      selecting:
        marking === NO_MARKING
          ? undefined
          : {
              text,
              documentId: itemDocument.id,
              marked: marking.marked,
              pageMarks: onPages,
              onSelect: marking.onSelect,
              onPress: marking.onPress,
            },
    };
  }, [text, marking, itemDocument.id]);
  const shown = setting !== "source";
  // Whether the translation is written without spaces between words, which tells the leadings
  // its pages may take.
  const unspaced = useMemo(
    () =>
      writtenUnspaced(
        [...translation.blocks.values()].map((block) => translatedText(block, SEGMENT_GAP).text),
      ),
    [translation.blocks],
  );
  const overlays = useMemo<Overlays | undefined>(() => {
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
    return { texts: onPages, layout, unspaced };
  }, [shown, translation.blocks, itemDocument, layout, unspaced]);
  const hasTables = itemDocument.blocks.some((block) => block.kind === "table");
  const opening = useArrivalPosition(entry.id);
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
        opening={opening}
        onTurned={undefined}
      />
    </>
  );
}
