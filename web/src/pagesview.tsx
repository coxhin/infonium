import { useMemo } from "react";
import { layoutOf, placedOf, type AreaText } from "./areas";
import { sourceLink, type PagePlace, type TranslatedBlock } from "./client";
import { PageViewer } from "./pages";
import { pagePlaces, placesOf } from "./places";
import { SEGMENT_GAP } from "./translated";
import type { ViewContext } from "./views";

const NO_PAGES: PagePlace[] = [];

// Where each block's translation is set is worked out once for each translation of it.
const placedKept = new WeakMap<TranslatedBlock, AreaText[]>();

// A PDF as its pages. With a translation on show, each translated block is set over the lines
// it stands for, and in 对照 the source stands beside each page, or under each area where
// there is no room for two. The boxes of a passage pointed at are outlined above it all.
export function PagesView({
  entry,
  itemDocument,
  setting,
  translation,
  pointed,
  onShow,
}: ViewContext) {
  // Worked out anew at every act of pointing, so that the pages turn to the same boxes again.
  const boxes = useMemo(
    () =>
      pointed === undefined ? NO_PAGES : pagePlaces(placesOf(itemDocument.blocks, pointed.anchor)),
    [pointed, itemDocument],
  );
  const layout = useMemo(() => layoutOf(itemDocument.blocks), [itemDocument]);
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
        inView={onShow}
        overlays={overlays}
        pairing={setting === "both"}
      />
    </>
  );
}
