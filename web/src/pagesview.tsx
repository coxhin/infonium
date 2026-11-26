import { useCallback, useMemo, useState } from "react";
import { layoutOf, placedOf, translatedText, writtenUnspaced, type AreaText } from "./areas";
import { useArrivalPosition } from "./arrival";
import { sourceLink, type PagePlace, type TranslatedBlock } from "./client";
import { NO_MARKING } from "./marking";
import { PageViewer, type PageMarks } from "./pages";
import { PageText } from "./pagelines";
import type { PageLink } from "./pagelinks";
import { placedPieces, type Glow, type TranslatedGlow } from "./pageglow";
import { markedBlocksOf, pageMarksOf } from "./pagemarking";
import { pagePlaces, placesOf } from "./places";
import { pageTextsOf } from "./pagetexts";
import { translatedStretch } from "./sentences";
import { SEGMENT_GAP } from "./translated";
import { linkedAreas, sourceLinksOn, type SourceLink } from "./translatedlinks";
import type { Pointed, ViewContext } from "./views";

const NO_PAGES: PagePlace[] = [];
const NO_LINKS: ReadonlyMap<number, PageLink[]> = new Map();

// Where each block's translation is set is worked out once for each translation of it.
const placedKept = new WeakMap<TranslatedBlock, AreaText[]>();

// Each act of pointing is numbered, so that its passage glows anew.
const pointings = new WeakMap<Pointed, number>();
let pointingCount = 0;

function numberOf(pointed: Pointed): number {
  let number = pointings.get(pointed);
  if (number === undefined) {
    pointingCount += 1;
    number = pointingCount;
    pointings.set(pointed, number);
  }
  return number;
}

// A PDF as its pages. With a translation on show, each translated block is set over the lines
// it stands for, and in 对照 the source stands beside each page, or under each flow where
// there is no room for two. The reader's highlights are drawn on the lines they cover, and the
// reader selects by dragging over the lines. A passage pointed at is turned to and glows for a
// moment: its words above it all, and the sentences of the translation that stand for them.
// The pages open where the reader stood in the file as it arrived, if they did.
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
  // The links of the pages read so far, by page, and by the block whose words each lies over:
  // a translation set over the pages carries those it can.
  const [pageLinks, setPageLinks] = useState(NO_LINKS);
  const linksRead = useCallback(
    (page: number, links: PageLink[]) =>
      setPageLinks((before) =>
        before.get(page) === links ? before : new Map(before).set(page, links),
      ),
    [],
  );
  const sourceLinks = useMemo(() => {
    const byBlock = new Map<number, SourceLink[]>();
    pageLinks.forEach((links, page) => {
      sourceLinksOn(text, page, links).forEach((found, blockNumber) => {
        byBlock.set(blockNumber, [...(byBlock.get(blockNumber) ?? []), ...found]);
      });
    });
    return byBlock;
  }, [text, pageLinks]);
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
      const linked = linkedAreas(
        placed,
        block.text,
        translated,
        sourceLinks.get(blockNumber),
        SEGMENT_GAP,
      );
      for (const text of linked) {
        const onPage = onPages.get(text.area.page) ?? [];
        onPage.push(text);
        onPages.set(text.area.page, onPage);
      }
    });
    // Whether the translation is written without spaces between words tells the leadings its
    // pages may take.
    const unspaced = writtenUnspaced(
      [...translation.blocks.values()].map((block) => translatedText(block, SEGMENT_GAP).text),
    );
    return pageTextsOf(onPages, layout, unspaced);
  }, [shown, translation.blocks, itemDocument, layout, sourceLinks]);
  // The passage pointed at, as it glows: its words on the source's lines, and where a
  // translation is on show, the sentences of each block's translation that stand for them.
  const glow = useMemo<Glow | undefined>(() => {
    if (pointed === undefined) {
      return undefined;
    }
    const translated: TranslatedGlow[] = [];
    for (const anchor of overlays === undefined ? [] : pointed.anchors) {
      const block = itemDocument.blocks[anchor.block_number];
      const translatedBlock = translation.blocks.get(anchor.block_number);
      const areas = translatedBlock === undefined ? undefined : placedKept.get(translatedBlock);
      if (block !== undefined && translatedBlock !== undefined && areas !== undefined) {
        const whole = translatedText(translatedBlock, SEGMENT_GAP).text;
        translated.push({
          blockNumber: anchor.block_number,
          stretch: translatedStretch(block.text, whole, anchor),
          pieces: placedPieces(whole, areas),
        });
      }
    }
    return {
      key: numberOf(pointed),
      spans: pointed.anchors.map((anchor) => ({
        blockNumber: anchor.block_number,
        start: anchor.start,
        end: anchor.end,
      })),
      translated,
    };
  }, [pointed, overlays, itemDocument, translation.blocks]);
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
        glow={glow}
        inView={onShow}
        overlays={overlays}
        pairing={setting === "both"}
        marks={marks}
        opening={opening}
        onTurned={undefined}
        onLinks={shown ? linksRead : undefined}
      />
    </>
  );
}
