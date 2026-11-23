import { useCallback, useEffect, useState } from "react";
import { sourceLink, type Anchor, type ItemDocument, type ItemEntry, type TimePlace } from "./client";
import type { Pointer } from "./digest";
import { reasonOf } from "./loading";
import { placesOf, surfaceOf, timePlaces } from "./places";
import { Clock, Player } from "./player";
import { checkAnchor, hideAnchor } from "./reader";
import { SettingSwitch, useSetting } from "./setting";
import { SidePanel } from "./side";
import { TranslationNote } from "./translated";
import { useTranslation } from "./translation";
import { viewsOf, type Pointed, type ViewContext, type ViewName } from "./views";
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
  // A view is made the first time it is on show, and kept from then on, so that what it
  // loads is loaded only for a reader who looks at it, and only once.
  const [made, setMade] = useState<ReadonlySet<ViewName>>(
    () => new Set(shown === undefined ? [] : [shown.name]),
  );
  function showView(name: ViewName) {
    setViewName(name);
    setMade((before) => (before.has(name) ? before : new Set(before).add(name)));
  }
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
      // Refuses at once an anchor whose passage the document does not hold. Each view works
      // out where the passage lies in what it shows, and brings it into view there; the
      // recording, which is not a view, plays from it.
      checkAnchor(itemDocument, anchor);
      setPointed({ anchor });
      setPlayed(timePlaces(placesOf(itemDocument.blocks, anchor)));
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
                    onClick={() => showView(view.name)}
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
        {views
          .filter((view) => made.has(view.name))
          .map((view) => (
            <div key={view.name} hidden={view !== shown}>
              <view.Draw {...context} onShow={view === shown} />
            </div>
          ))}
      </main>
      <SidePanel itemId={entry.id} documentId={itemDocument.id} pointer={pointer} />
    </>
  );
}
