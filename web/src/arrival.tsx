import { useCallback, useEffect, useMemo, useState } from "react";
import { sourceLink, type PagePlace } from "./client";
import { PageText } from "./pagelines";
import { PageViewer, type PageMarks, type PagePosition } from "./pages";

const NO_PLACES: PagePlace[] = [];
const NO_BLOCKS: ReadonlySet<number> = new Set();

// Where the reader last stood in the pages of each item's file as it arrived, for the pages of
// the document read from it to open at.
const standing = new Map<string, PagePosition>();

// A PDF read for the first time, shown as it arrived while it is read: its pages alone, with
// nothing to mark, translate or point at until its document is read.
export function ArrivedPages({ itemId }: { itemId: string }) {
  const marks = useMemo<PageMarks>(
    () => ({ text: new PageText([]), onPages: new Map(), blocks: new Map(), selecting: undefined }),
    [],
  );
  const turned = useCallback(
    (position: PagePosition) => standing.set(itemId, position),
    [itemId],
  );
  return (
    <main>
      <p className="pages-note">
        正在识别文字，PDF 可能需要几分钟。识别完后可以看文本、高亮、翻译和提问。
      </p>
      <PageViewer
        source={sourceLink(itemId)}
        pointed={NO_PLACES}
        pointedBlocks={NO_BLOCKS}
        inView
        overlays={undefined}
        pairing={false}
        marks={marks}
        opening={undefined}
        onTurned={turned}
      />
    </main>
  );
}

// Where the pages of the item's document open: where the reader stood in its file as it
// arrived, which is told once.
export function useArrivalPosition(itemId: string): PagePosition | undefined {
  const [position] = useState(() => standing.get(itemId));
  useEffect(() => {
    standing.delete(itemId);
  }, [itemId]);
  return position;
}
