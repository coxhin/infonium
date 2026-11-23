import { useCallback, useEffect, useState } from "react";
import {
  sourceLink,
  type Anchor,
  type ItemDocument,
  type ItemEntry,
  type PagePlace,
  type TimePlace,
} from "./client";
import type { Pointer } from "./digest";
import { itemTitle } from "./entry";
import { reasonOf } from "./loading";
import { pagePlaces, placesOf, surfaceOf, timePlaces } from "./places";
import { Clock, Player } from "./player";
import { hideAnchor, highlightAnchor, Reader, showAnchor } from "./reader";
import { SettingSwitch, useSetting } from "./setting";
import { SidePanel } from "./side";
import { TranslationNote } from "./translated";
import { useTranslation } from "./translation";
import { PagesView, VIEW_LABELS, viewsOf, type ViewContext, type ViewName } from "./views";
import "./reading.css";

// One act of pointing at a passage: made anew at every click, so that pointing at the same
// passage again leads there again.
type Pointed = { anchor: Anchor; pages: PagePlace[] };
const NO_PAGES: PagePlace[] = [];

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
  const views = viewsOf(itemDocument);
  const [view, setView] = useState<ViewName>(views[0] ?? "text");
  const setting = useSetting();
  const translation = useTranslation(entry.id, setting !== "source");
  const [pointed, setPointed] = useState<Pointed>();
  const [played, setPlayed] = useState<TimePlace[]>([]);
  const [clock] = useState(() => new Clock());
  const [lost, setLost] = useState<string>();
  const textInView = view === "text";
  const playFrom = useCallback((place: TimePlace) => setPlayed([place]), []);

  const pointer: Pointer = {
    show(anchor) {
      // Refuses at once an anchor whose passage the page does not hold; where it is brought
      // into view is settled once the reader has marked it.
      highlightAnchor(anchor);
      const places = placesOf(itemDocument.blocks, anchor);
      setPointed({ anchor, pages: pagePlaces(places) });
      setPlayed(timePlaces(places));
    },
    hide() {
      hideAnchor();
      setPointed(undefined);
    },
  };

  // A passage is brought into view where it is on show, which depends on the setting: in the
  // source, or in its translation. One pointed at while the text was out of view is brought
  // into view when the text comes back; text that is out of view is only marked, as bringing
  // it into view would scroll the pages that are on show instead.
  useEffect(() => {
    if (textInView && pointed !== undefined) {
      try {
        showAnchor(pointed.anchor);
      } catch (error) {
        setLost(reasonOf(error));
      }
    }
  }, [pointed, textInView, setting]);

  useEffect(() => {
    if (opening !== undefined) {
      try {
        pointer.show(opening);
      } catch (error) {
        setLost(reasonOf(error));
      }
    }
  }, []);

  const context: ViewContext = { setting, translation, inView: view === "pages" };
  return (
    <>
      <main>
        <RereadingNote entry={entry} />
        {lost !== undefined && <p className="rereading failed">{lost}</p>}
        <div className="reading-head">
          <div className="reading-bar">
            {views.length > 1 && (
              <div className="views" role="tablist">
                {views.map((name) => (
                  <button
                    key={name}
                    role="tab"
                    aria-selected={view === name}
                    onClick={() => setView(name)}
                  >
                    {VIEW_LABELS[name]}
                  </button>
                ))}
              </div>
            )}
            <SettingSwitch setting={setting} />
            {setting !== "source" && <TranslationNote translation={translation} />}
          </div>
          {surface === "time" && (
            <Player source={sourceLink(entry.id)} pointed={played} clock={clock} />
          )}
        </div>
        {views.includes("pages") && (
          <div hidden={view !== "pages"}>
            <PagesView
              source={sourceLink(entry.id)}
              pointed={pointed?.pages ?? NO_PAGES}
              context={context}
            />
          </div>
        )}
        <div hidden={!textInView}>
          <Reader
            entry={entry}
            title={itemTitle(entry)}
            itemDocument={itemDocument}
            setting={setting}
            translation={translation}
            pointed={pointed?.anchor}
            playing={surface === "time" ? { clock, played, onPlay: playFrom } : undefined}
          />
        </div>
      </main>
      <SidePanel itemId={entry.id} documentId={itemDocument.id} pointer={pointer} />
    </>
  );
}
