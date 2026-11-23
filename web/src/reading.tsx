import { useEffect, useState } from "react";
import {
  sourceLink,
  type Anchor,
  type ItemDocument,
  type ItemEntry,
  type PagePlace,
  type TimePlace,
} from "./client";
import type { Pointer } from "./citing";
import { itemTitle } from "./entry";
import { reasonOf } from "./loading";
import { PageViewer } from "./pages";
import { pagePlaces, placesOf, surfaceOf, timePlaces } from "./places";
import { Player } from "./player";
import { hideAnchor, highlightAnchor, Reader, showAnchor } from "./reader";
import { SidePanel } from "./side";

// A paged source is shown either as its pages or as the text read from them.
type View = "pages" | "text";
const VIEW_NAMES: Record<View, string> = { pages: "原件", text: "文本" };

// One act of pointing at a passage: made anew at every click, so that pointing at the same
// passage again leads there again.
type Pointed = { anchor: Anchor; pages: PagePlace[]; times: TimePlace[] };
const NOWHERE: { pages: PagePlace[]; times: TimePlace[] } = { pages: [], times: [] };

export type ReadEntry = ItemEntry & { reading: { kind: "read" } };

// What the latest attempt to read an item again has come to, above the document that is kept.
function RereadingNote({ entry }: { entry: ReadEntry }) {
  const attempt = entry.reading.attempt;
  switch (attempt.kind) {
    case "underway":
      return <p className="rereading">正在重新读取，下面是上一次读到的内容。</p>;
    case "failed":
      return (
        <p className="rereading failed">
          重新读取失败，下面是上一次读到的内容。原因：{attempt.reason}
        </p>
      );
    case "settled":
      return null;
  }
}

// An item that has been read: its source in the reader, and beside it what infonium wrote
// about it. The item may be opened for one passage, which is then shown at once.
export function ReadItem({
  entry,
  itemDocument,
  opening,
}: {
  entry: ReadEntry;
  itemDocument: ItemDocument;
  opening: Anchor | undefined;
}) {
  const surface = surfaceOf(itemDocument.blocks);
  const [view, setView] = useState<View>("pages");
  const [pointed, setPointed] = useState<Pointed>();
  const [lost, setLost] = useState<string>();
  const textInView = surface !== "page" || view === "text";

  const pointer: Pointer = {
    show(anchor) {
      // Text that is out of view is only marked; bringing it into view would scroll the
      // pages that are on show instead.
      (textInView ? showAnchor : highlightAnchor)(anchor);
      const places = placesOf(itemDocument.blocks, anchor);
      setPointed({ anchor, pages: pagePlaces(places), times: timePlaces(places) });
    },
    hide() {
      hideAnchor();
      setPointed(undefined);
    },
  };

  // A passage pointed at while the text was out of view is brought into view when the text
  // comes back.
  useEffect(() => {
    if (textInView && pointed !== undefined) {
      showAnchor(pointed.anchor);
    }
  }, [textInView]);

  useEffect(() => {
    if (opening !== undefined) {
      try {
        pointer.show(opening);
      } catch (error) {
        setLost(reasonOf(error));
      }
    }
  }, []);

  const { pages, times } = pointed ?? NOWHERE;
  return (
    <>
      <main>
        <RereadingNote entry={entry} />
        {lost !== undefined && <p className="rereading failed">{lost}</p>}
        {surface === "time" && <Player source={sourceLink(entry.id)} pointed={times} />}
        {surface === "page" && (
          <div className="views" role="tablist">
            {(Object.keys(VIEW_NAMES) as View[]).map((name) => (
              <button
                key={name}
                role="tab"
                aria-selected={view === name}
                onClick={() => setView(name)}
              >
                {VIEW_NAMES[name]}
              </button>
            ))}
          </div>
        )}
        {surface === "page" && (
          <div hidden={view !== "pages"}>
            <PageViewer source={sourceLink(entry.id)} pointed={pages} inView={view === "pages"} />
          </div>
        )}
        <div hidden={!textInView}>
          <Reader entry={entry} title={itemTitle(entry)} itemDocument={itemDocument} />
        </div>
      </main>
      <SidePanel itemId={entry.id} documentId={itemDocument.id} pointer={pointer} />
    </>
  );
}

