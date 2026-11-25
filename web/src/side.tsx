import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import type { Anchors } from "./client";
import { ChatPanel, type ScopeKind } from "./chat";
import { CitingNote, useCiting, type Pointer } from "./citing";
import { DigestPanel } from "./digest";
import { Announcer } from "./announce";
import { ModelChoiceSelect, useModelChoice } from "./modelchoice";
import { NotebookPanel, useNotebookAsked } from "./notebook";
import type { Seat, Unshown } from "./seat";
import "./side.css";

// What the reader tells of itself: its item, the document it shows, the document the item
// holds now, and the link it was opened by.
type Shown = { itemId: string; documentId: string; holding: string; link: string };

type Seating = {
  seat: (shown: Shown) => Seat;
  unseat: (seat: Seat) => void;
  pointer: RefObject<Pointer | undefined>;
  cannotShow: (unshown: Unshown) => void;
  canShow: (unshown: Unshown) => void;
  // How the reader says that a passage pointed at cannot be shown in the view that came on
  // show after it was pointed at; the column says it where it says every lost passage.
  lose: RefObject<((reason: string) => void) | undefined>;
};

const SeatingContext = createContext<Seating | undefined>(undefined);
export const SeatingProvider = SeatingContext.Provider;

const NOT_ON_SHOW = "原文还没有打开";

// The column lives as long as the app, so that what it holds outlasts the reader of any one
// item: the tab on show, the conversations and their places. The reader tells it, through
// SidePanel, which document is on show and how to point into it.
export function useSeating(): {
  seating: Seating;
  seated: Seat | undefined;
  unshown: Unshown | undefined;
  pointer: Pointer;
} {
  const [seated, setSeated] = useState<Seat>();
  const [unshown, setUnshown] = useState<Unshown>();
  const current = useRef<Pointer>(undefined);
  const lose = useRef<(reason: string) => void>(undefined);
  const lives = useRef(0);
  const seating = useMemo<Seating>(
    () => ({
      seat: (shown) => {
        lives.current += 1;
        const seat = { ...shown, life: lives.current };
        setSeated(seat);
        return seat;
      },
      unseat: (left) => {
        current.current = undefined;
        setSeated((held) => (held?.life === left.life ? undefined : held));
      },
      pointer: current,
      cannotShow: setUnshown,
      canShow: (left) => setUnshown((held) => (held === left ? undefined : held)),
      lose,
    }),
    [],
  );
  const pointer = useMemo<Pointer>(
    () => ({
      show(anchors) {
        if (current.current === undefined) {
          throw new Error(NOT_ON_SHOW);
        }
        current.current.show(anchors);
      },
      hide() {
        current.current?.hide();
      },
    }),
    [],
  );
  return { seating, seated, unshown, pointer };
}

function useSeatingContext(): Seating {
  const seating = useContext(SeatingContext);
  if (seating === undefined) {
    throw new Error("the reader is drawn outside the app's SeatingProvider");
  }
  return seating;
}

// Tells the column, while an item is selected and cannot be shown, why not.
export function useCannotShow(itemId: string, reason: string | undefined): void {
  const seating = useSeatingContext();
  useEffect(() => {
    if (reason === undefined) {
      return undefined;
    }
    const unshown = { itemId, reason };
    seating.cannotShow(unshown);
    return () => seating.canShow(unshown);
  }, [itemId, reason]);
}

// How a view of the reader says that the passage pointed at cannot be shown in it.
export function useLosing(): (reason: string) => void {
  const seating = useSeatingContext();
  return useCallback((reason) => seating.lose.current?.(reason), [seating]);
}

// Where the reader of an item meets the column beside it: the column itself is drawn by the
// app, which keeps it while items come and go.
export function SidePanel({
  itemId,
  documentId,
  holding,
  link,
  pointer,
}: {
  itemId: string;
  documentId: string;
  holding: string;
  link: string;
  pointer: Pointer;
}) {
  const seating = useSeatingContext();
  // The reader's pointer is made anew at every render and knows what is in view then.
  useLayoutEffect(() => {
    seating.pointer.current = pointer;
  });
  useLayoutEffect(() => {
    const seat = seating.seat({ itemId, documentId, holding, link });
    return () => seating.unseat(seat);
  }, [itemId, documentId, holding, link]);
  return null;
}

type Tab = "digest" | "chat" | "notes";
const TAB_NAMES: Record<Tab, string> = { digest: "摘要", chat: "对话", notes: "笔记" };
// The tabs that are about one item, which cannot be chosen while none is open.
const ITEM_TABS: ReadonlySet<Tab> = new Set(["digest"]);

function tabId(tab: Tab): string {
  return `side-tab-${tab}`;
}

function panelId(tab: Tab): string {
  return `side-panel-${tab}`;
}

// The column beside the reader: the digest of the open item, the chat, and the notebook.
export function SideColumn({
  selected,
  seated,
  unshown,
  pointer,
  onOpen,
}: {
  selected: string | undefined;
  seated: Seat | undefined;
  unshown: Unshown | undefined;
  pointer: Pointer;
  onOpen: (itemId: string) => void;
}) {
  const seating = useSeatingContext();
  // What the reader chose. With no item open, the library's chat is on show in place of a tab
  // about one item without being chosen, so that an item opened from the list shows what was
  // chosen before.
  const [chosen, setChosen] = useState<Tab>("digest");
  const [chosenScope, setChosenScope] = useState<ScopeKind>("item");
  const tab: Tab = selected === undefined && ITEM_TABS.has(chosen) ? "chat" : chosen;
  // Until the reader shows the selected item, the column points at nothing.
  const seat = seated?.itemId === selected ? seated : undefined;
  const citing = useCiting(pointer, selected, seat, unshown);
  useLayoutEffect(() => {
    seating.lose.current = citing.fail;
  });

  // Opens an item at a passage, which is shown once the item's reader is on show.
  function openAt(itemId: string, anchors: Anchors) {
    citing.awaitOpening(anchors, itemId);
    onOpen(itemId);
  }

  // A citation that opens another item leaves the reader in the conversation it was pressed
  // in: only the library's can cite another item. A citation into the selected item, whose
  // reader is not on show yet, changes nothing of what is on show.
  function openFromChat(itemId: string, anchors: Anchors) {
    if (itemId !== selected) {
      setChosen("chat");
      setChosenScope("library");
    }
    openAt(itemId, anchors);
  }

  // Where the app is one column, the column is a drawer at the foot of the window, which folds
  // to its tabs; choosing a tab opens it, and choosing the open tab again folds it. It starts
  // folded, so that the first screen of a source shows the source, not the drawer over it.
  const [folded, setFolded] = useState(true);
  function choose(name: Tab) {
    setFolded(name === tab && !folded);
    setChosen(name);
  }

  // The notebook asked for from elsewhere comes on show, unfolded.
  useNotebookAsked(() => {
    setChosen("notes");
    setFolded(false);
  });

  // A digest may take a model call to write, so it is asked for only once its tab is shown,
  // and then kept while the item stays open.
  const [digestShown, setDigestShown] = useState<string>();
  const digestWanted = seat !== undefined && (tab === "digest" || digestShown === seat.itemId);
  useEffect(() => {
    if (seat !== undefined && tab === "digest") {
      setDigestShown(seat.itemId);
    }
  }, [seat?.itemId, tab]);

  // The model the column's work is asked of: digests, answers, and a block translated again.
  const answeringChoice = useModelChoice("answering");

  return (
    <aside className="side" data-folded={folded}>
      <div className="tabs" role="tablist">
        {(Object.keys(TAB_NAMES) as Tab[]).map((name) => (
          <button
            key={name}
            id={tabId(name)}
            role="tab"
            aria-selected={tab === name}
            aria-controls={panelId(name)}
            disabled={selected === undefined && ITEM_TABS.has(name)}
            onClick={() => choose(name)}
          >
            {TAB_NAMES[name]}
          </button>
        ))}
      </div>
      <button className="fold" aria-expanded={!folded} onClick={() => setFolded(!folded)}>
        {folded ? "展开" : "收起"}
      </button>
      <div className="side-model">
        <ModelChoiceSelect
          choice={answeringChoice}
          label="摘要和对话的模型"
          title="生成摘要、回答问题和单段重译（↻）时用这个模型；已有的摘要不会重写"
        />
      </div>
      <CitingNote citing={citing} />
      <div
        id={panelId("digest")}
        role="tabpanel"
        aria-labelledby={tabId("digest")}
        className="panel scrolled"
        hidden={tab !== "digest"}
      >
        {digestWanted ? (
          <DigestPanel
            key={seat.holding}
            itemId={seat.itemId}
            model={answeringChoice.chosen}
            citing={citing}
          />
        ) : (
          <p className="status">原文打开后，这里显示它的摘要。</p>
        )}
      </div>
      <div
        id={panelId("chat")}
        role="tabpanel"
        aria-labelledby={tabId("chat")}
        className="panel"
        hidden={tab !== "chat"}
      >
        <ChatPanel
          model={answeringChoice.chosen}
          itemId={selected}
          seat={seat}
          scope={chosenScope}
          onScope={setChosenScope}
          shown={tab === "chat"}
          citing={citing}
          onOpen={openFromChat}
        />
      </div>
      <div
        id={panelId("notes")}
        role="tabpanel"
        aria-labelledby={tabId("notes")}
        className="panel"
        hidden={tab !== "notes"}
      >
        <NotebookPanel
          selected={selected}
          seat={seat}
          shown={tab === "notes"}
          citing={citing}
          onOpen={openAt}
        />
      </div>
      <Announcer />
    </aside>
  );
}
