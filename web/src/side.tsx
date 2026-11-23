import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import type { Anchor } from "./client";
import { ChatPanel, type Opener, type ScopeKind } from "./chat";
import { useCiting, type Pointer } from "./citing";
import { DigestPanel } from "./digest";
import type { Seat } from "./seat";
import "./side.css";

// What the reader tells of itself: its item and the document it shows.
type Shown = { itemId: string; documentId: string };

type Seating = {
  seat: (shown: Shown) => Seat;
  unseat: (seat: Seat) => void;
  pointer: RefObject<Pointer | undefined>;
};

const SeatingContext = createContext<Seating | undefined>(undefined);
export const SeatingProvider = SeatingContext.Provider;

const NOT_ON_SHOW = "原文还没有打开";

// The column lives as long as the app, so that what it holds outlasts the reader of any one
// item: the tab on show, the conversations and their places. The reader tells it, through
// SidePanel, which document is on show and how to point into it.
export function useSeating(): { seating: Seating; seated: Seat | undefined; pointer: Pointer } {
  const [seated, setSeated] = useState<Seat>();
  const current = useRef<Pointer>(undefined);
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
    }),
    [],
  );
  const pointer = useMemo<Pointer>(
    () => ({
      show(anchor) {
        if (current.current === undefined) {
          throw new Error(NOT_ON_SHOW);
        }
        current.current.show(anchor);
      },
      hide() {
        current.current?.hide();
      },
    }),
    [],
  );
  return { seating, seated, pointer };
}

// Where the reader of an item meets the column beside it: the column itself is drawn by the
// app, which keeps it while items come and go.
export function SidePanel({
  itemId,
  documentId,
  pointer,
}: {
  itemId: string;
  documentId: string;
  pointer: Pointer;
}) {
  const seating = useContext(SeatingContext);
  if (seating === undefined) {
    throw new Error("SidePanel is used outside the app's SeatingProvider");
  }
  // The reader's pointer is made anew at every render and knows what is in view then.
  useLayoutEffect(() => {
    seating.pointer.current = pointer;
  });
  useLayoutEffect(() => {
    const seat = seating.seat({ itemId, documentId });
    return () => seating.unseat(seat);
  }, [itemId, documentId]);
  return null;
}

type Tab = "digest" | "chat" | "notes";
const TAB_NAMES: Record<Tab, string> = { digest: "摘要", chat: "对话", notes: "笔记" };
// The tabs that are about one item, which cannot be chosen while none is open.
const ITEM_TABS: ReadonlySet<Tab> = new Set(["digest", "notes"]);

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
  pointer,
  onOpen,
}: {
  selected: string | undefined;
  seated: Seat | undefined;
  pointer: Pointer;
  onOpen: Opener;
}) {
  // What the reader chose. With no item open, the library's chat is on show without being
  // chosen, so that an item opened from the list shows what was chosen before.
  const [chosen, setChosen] = useState<Tab>("digest");
  const [chosenScope, setChosenScope] = useState<ScopeKind>("item");
  const tab: Tab = selected === undefined ? "chat" : chosen;
  // Until the reader shows the selected item, the column points at nothing.
  const seat = seated?.itemId === selected ? seated : undefined;
  const citing = useCiting(pointer, seat);

  // A citation that opens an item leaves the reader in the conversation it was pressed in.
  function openAt(itemId: string, anchor: Anchor) {
    setChosen("chat");
    setChosenScope("library");
    citing.awaitOpening(anchor, itemId);
    onOpen(itemId, anchor);
  }

  // A digest may take a model call to write, so it is asked for only once its tab is shown,
  // and then kept while the item stays open.
  const [digestShown, setDigestShown] = useState<string>();
  const digestWanted = seat !== undefined && (tab === "digest" || digestShown === seat.itemId);
  useEffect(() => {
    if (seat !== undefined && tab === "digest") {
      setDigestShown(seat.itemId);
    }
  }, [seat?.itemId, tab]);

  return (
    <aside className="side">
      <div className="tabs" role="tablist">
        {(Object.keys(TAB_NAMES) as Tab[]).map((name) => (
          <button
            key={name}
            id={tabId(name)}
            role="tab"
            aria-selected={tab === name}
            aria-controls={panelId(name)}
            disabled={selected === undefined && ITEM_TABS.has(name)}
            onClick={() => setChosen(name)}
          >
            {TAB_NAMES[name]}
          </button>
        ))}
      </div>
      {citing.lost !== undefined && (
        <p className="lost status failed" role="alert">
          {citing.lost}
        </p>
      )}
      <div
        id={panelId("digest")}
        role="tabpanel"
        aria-labelledby={tabId("digest")}
        className="panel scrolled"
        hidden={tab !== "digest"}
      >
        {digestWanted ? (
          <DigestPanel key={seat.itemId} itemId={seat.itemId} citing={citing} />
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
          itemId={selected}
          seat={seat}
          scope={chosenScope}
          onScope={setChosenScope}
          shown={tab === "chat"}
          citing={citing}
          onOpen={openAt}
        />
      </div>
      <div
        id={panelId("notes")}
        role="tabpanel"
        aria-labelledby={tabId("notes")}
        className="panel scrolled"
        hidden={tab !== "notes"}
      >
        <p className="status">笔记本会在以后的版本里加入。</p>
      </div>
    </aside>
  );
}
