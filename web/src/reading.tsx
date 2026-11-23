import { useCallback, useEffect, useState } from "react";
import { sourceLink, type Anchor, type ItemDocument, type ItemEntry, type TimePlace } from "./client";
import type { Pointer } from "./digest";
import { reasonOf } from "./loading";
import { pagePlaces, placesOf, surfaceOf, timePlaces } from "./places";
import { Clock, Player } from "./player";
import { hideAnchor, highlightAnchor } from "./reader";
import { SettingSwitch, useSetting } from "./setting";
import { SidePanel } from "./side";
import { TranslationNote } from "./translated";
import { useTranslation } from "./translation";
import { viewsOf, type Pointed, type ViewContext } from "./views";
import "./reading.css";

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
// about it. The item may be opened for one passage, which is then shown at once. The reader
// offers the views its source has, draws each, and shows one; the translation is asked for
// only while a view that shows it is on show with a translation set.
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
  const [viewName, setViewName] = useState(views[0]?.name);
  const shown = views.find((view) => view.name === viewName) ?? views[0];
  const setting = useSetting();
  const translation = useTranslation(
    entry.id,
    shown !== undefined && shown.showsTranslation && setting !== "source",
  );
  const [pointed, setPointed] = useState<Pointed>();
  const [played, setPlayed] = useState<TimePlace[]>([]);
  const [clock] = useState(() => new Clock());
  const [lost, setLost] = useState<string>();
  const playFrom = useCallback((place: TimePlace) => setPlayed([place]), []);

  const pointer: Pointer = {
    show(anchor) {
      // Refuses at once an anchor whose passage the page does not hold; each view brings it
      // into view where it shows it.
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

  useEffect(() => {
    if (opening !== undefined) {
      try {
        pointer.show(opening);
      } catch (error) {
        setLost(reasonOf(error));
      }
    }
  }, []);

  const context: Omit<ViewContext, "onShow"> = {
    entry,
    itemDocument,
    setting,
    translation,
    pointed,
    playing: surface === "time" ? { clock, played, onPlay: playFrom } : undefined,
    onLost: setLost,
  };
  return (
    <>
      <main>
        <RereadingNote entry={entry} />
        {lost !== undefined && <p className="rereading failed">{lost}</p>}
        <div className="reading-head">
          <div className="reading-bar">
            {views.length > 1 && (
              <div className="views" role="tablist">
                {views.map((view) => (
                  <button
                    key={view.name}
                    role="tab"
                    aria-selected={view === shown}
                    onClick={() => setViewName(view.name)}
                  >
                    {view.label}
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
        {views.map((view) => (
          <div key={view.name} hidden={view !== shown}>
            <view.Draw {...context} onShow={view === shown} />
          </div>
        ))}
      </main>
      <SidePanel itemId={entry.id} documentId={itemDocument.id} pointer={pointer} />
    </>
  );
}
