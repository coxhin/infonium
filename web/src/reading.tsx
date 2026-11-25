import { useCallback, useMemo, useState, type ReactNode } from "react";
import { sourceLink, type ItemDocument, type ItemEntry, type TimePlace } from "./client";
import type { Pointer } from "./citing";
import { markedOf, openHighlight, useHighlights, useOpenedHighlight } from "./highlights";
import { SelectionActions } from "./selectionactions";
import { NO_MARKING, type Marking, type Selected } from "./marking";
import { placesOf, surfaceOf, timePlaces } from "./places";
import { Clock, Player } from "./player";
import { checkAnchor, hideAnchor } from "./reader";
import { ModelChoiceSelect, useModelChoice } from "./modelchoice";
import { SettingSwitch, useSetting } from "./setting";
import { SidePanel, useLosing } from "./side";
import { TranslationNote } from "./translated";
import { useTranslation } from "./translation";
import { viewsOf, type Pointed, type ViewContext, type ViewName } from "./views";
import "./reading.css";

export type ReadEntry = ItemEntry & { reading: { kind: "read" } };

// Which of the item's documents the reader shows: the one the item holds now, or an earlier
// revision's, which is shown as its text alone, in the words it was written in, under a line
// that says which reading it is. The pages, the web page, the recording and the translation
// the service keeps are those of the document the item holds now, so they are not offered for
// an earlier one, and nothing can be marked in it.
export type ShownRevision =
  | { kind: "holding" }
  | { kind: "earlier"; holding: string; line: ReactNode };

// What the latest attempt to read an item again has come to, above the document that is kept,
// or above an earlier revision, which is not what the last reading gave.
function RereadingNote({ entry, earlier }: { entry: ReadEntry; earlier: boolean }) {
  const attempt = entry.reading.attempt;
  switch (attempt.kind) {
    case "underway":
      return (
        <p className="rereading">
          {earlier ? "正在重新读取这一篇。" : "正在重新读取，下面是上一次读到的内容。"}
        </p>
      );
    case "failed":
      return (
        <p className="rereading failed">
          {earlier ? "重新读取失败。" : "重新读取失败，下面是上一次读到的内容。"}原因：
          {attempt.reason}
        </p>
      );
    case "settled":
      return null;
  }
}

// An item that has been read: its source in the reader, and beside it what infonium wrote
// about it. A passage is shown through the pointer the reader hands the column, which says
// itself when the passage cannot be found. The reader offers the views its source has, draws
// each, and shows one; the translation is asked for only while a view that shows it is on show
// with a translation set. The head holds what is said of the item's revisions; the link is the
// one the reader was opened by.
export function ReadItem({
  entry,
  itemDocument,
  revision,
  link,
  head,
}: {
  entry: ReadEntry;
  itemDocument: ItemDocument;
  revision: ShownRevision;
  link: string;
  head: ReactNode;
}) {
  const earlier = revision.kind === "earlier";
  const surface = earlier ? "flow" : surfaceOf(itemDocument.blocks);
  const views = viewsOf(itemDocument).filter((view) => !earlier || view.name === "text");
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
  const chosenSetting = useSetting();
  const setting = earlier ? "source" : chosenSetting;
  const translationChoice = useModelChoice("translation");
  const answeringChoice = useModelChoice("answering");
  const translation = useTranslation(
    entry.id,
    shown !== undefined && shown.showsTranslation && setting !== "source",
    translationChoice.chosen,
    answeringChoice.chosen,
  );
  const [pointed, setPointed] = useState<Pointed>();
  const [played, setPlayed] = useState<TimePlace[]>([]);
  const [clock] = useState(() => new Clock());
  const lose = useLosing();
  const playFrom = useCallback((place: TimePlace) => setPlayed([place]), []);

  // The reader's highlights that stand in this document, for every view to draw, and the
  // passage last selected in a view, on which the actions are offered.
  const highlights = useHighlights(entry.id, earlier ? undefined : itemDocument.id);
  const opened = useOpenedHighlight();
  const [selected, setSelected] = useState<Selected>();
  const marking = useMemo<Marking>(
    () =>
      earlier
        ? NO_MARKING
        : {
            marked: highlights.state === "ready" ? markedOf(highlights.value) : [],
            opened: opened?.itemId === entry.id ? opened.highlightId : undefined,
            onSelect: setSelected,
            onPress: (highlightId) =>
              openHighlight({ itemId: entry.id, highlightId, commenting: false }),
          },
    [earlier, highlights, opened, entry.id],
  );

  const pointer: Pointer = {
    show(anchors) {
      // Refuses at once a passage that the document does not hold. Each view works out where
      // the passage lies in what it shows, and brings it into view there; the recording,
      // which is not a view, plays from it.
      anchors.forEach((anchor) => checkAnchor(itemDocument, anchor));
      setPointed({ anchors });
      setPlayed(timePlaces(anchors.flatMap((anchor) => placesOf(itemDocument.blocks, anchor))));
    },
    hide() {
      hideAnchor();
      setPointed(undefined);
    },
  };

  const context: Omit<ViewContext, "onShow"> = {
    entry,
    itemDocument,
    setting,
    translation,
    pointed,
    marking,
    playing: surface === "time" ? { clock, played, onPlay: playFrom } : undefined,
    onLost: lose,
  };
  return (
    <>
      <main>
        <RereadingNote entry={entry} earlier={earlier} />
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
            {!earlier && <SettingSwitch setting={setting} />}
            {!earlier && (
              <ModelChoiceSelect
                choice={translationChoice}
                label="翻译模型"
                title="开始、继续或全部重新翻译时用这个模型"
              />
            )}
            {setting !== "source" && <TranslationNote translation={translation} />}
            {head}
          </div>
          {surface === "time" && (
            <Player source={sourceLink(entry.id)} pointed={played} clock={clock} />
          )}
        </div>
        {revision.kind === "earlier" && revision.line}
        {views
          .filter((view) => made.has(view.name))
          .map((view) => (
            <div key={view.name} hidden={view !== shown}>
              <view.Draw {...context} onShow={view === shown} />
            </div>
          ))}
        {!earlier && <SelectionActions itemId={entry.id} selected={selected} />}
      </main>
      <SidePanel
        itemId={entry.id}
        documentId={itemDocument.id}
        holding={revision.kind === "earlier" ? revision.holding : itemDocument.id}
        link={link}
        pointer={pointer}
      />
    </>
  );
}
