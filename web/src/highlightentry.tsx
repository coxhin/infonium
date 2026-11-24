import {
  memo,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import { anchorsOf, Refused, type Highlight, type Passage } from "./client";
import { say } from "./heard";
import { noteOnHighlight, unmark, type Opened } from "./highlights";
import { reasonOf } from "./loading";
import { NoteDown, type Taking } from "./notedown";

// What an entry asks of the list it is in. The list keeps these the same from render to
// render, so that an entry is drawn again only when its own highlight changes.
export type EntryActions = {
  // Shows the highlight's passage where it stands now, and opens the highlight.
  press: (highlight: Highlight) => void;
  // Opens the revision the highlight was marked in, at its passage.
  openRevision: (highlight: Highlight) => void;
  // The highlight is gone from the list; the keyboard is put where the reader goes on.
  removed: () => void;
};

// Opened highlights that an entry has acted on: brought into view, said, made ready for a
// comment. Each opening is acted on once, though its entry is drawn again or anew.
const actedOn = new WeakSet<Opened>();
// Openings that the notebook's own lists made, which are not said to a screen reader: the
// reader pressed them there.
const chosenInList = new WeakSet<Opened>();

export function choseInList(opened: Opened): Opened {
  chosenInList.add(opened);
  return opened;
}

// What was typed into a comment and not kept, by highlight: the field comes back with it when
// the entry is drawn anew, as when its item is opened again.
const unkept = new Map<string, string>();

// One observer for every entry, which says when the box of the marked words changes size, as
// it does when the notebook comes on show.
const sizeWatchers = new Map<Element, () => void>();
const sizes = new ResizeObserver((changes) =>
  changes.forEach((changed) => sizeWatchers.get(changed.target)?.()),
);

function watchSize(element: Element, changed: () => void): () => void {
  sizeWatchers.set(element, changed);
  sizes.observe(element);
  return () => {
    sizes.unobserve(element);
    sizeWatchers.delete(element);
  };
}

// The statuses a gateway answers with when the service behind it does not; the service itself
// answers none of them to a change of a highlight, which calls no model.
const UNANSWERED_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);

// Why a change of a highlight did not happen, as the reader is told it.
function changeReason(error: unknown): string {
  return error instanceof Refused && UNANSWERED_STATUSES.has(error.status)
    ? "连不上服务"
    : reasonOf(error);
}

const WORDS_JOIN = "\n";
const SAID_WORDS = 40;
// How far from the edge of the list, in pixels, an entry brought into view stands.
const VIEW_MARGIN = 16;

function wordsOf(passage: Passage): string {
  return passage.anchors.map((anchor) => anchor.quote).join(WORDS_JOIN);
}

// Where a highlight stands now, as the passage to show and quote: where the document on show
// holds it, or as it was marked where that document has lost it.
function passageNow(highlight: Highlight): Passage {
  return highlight.standing.kind === "found" ? highlight.standing.passage : highlight.passage;
}

// The marked words, cut after a few lines; the rest is in reach of a press.
function Words({
  highlight,
  pressed,
  actions,
}: {
  highlight: Highlight;
  pressed: boolean;
  actions: EntryActions;
}) {
  const words = wordsOf(passageNow(highlight));
  const box = useRef<HTMLSpanElement>(null);
  const [whole, setWhole] = useState(false);
  const [cut, setCut] = useState(false);
  useLayoutEffect(() => {
    const element = box.current;
    if (element === null || whole) {
      return undefined;
    }
    return watchSize(element, () => setCut(element.scrollHeight > element.clientHeight + 1));
  }, [words, whole]);

  const text = (
    <span ref={box} className="words-text" data-whole={whole}>
      {words}
    </span>
  );
  return (
    <>
      {highlight.standing.kind === "found" ? (
        <button className="words" aria-pressed={pressed} onClick={() => actions.press(highlight)}>
          {text}
        </button>
      ) : (
        <blockquote className="words">{text}</blockquote>
      )}
      {(cut || whole) && (
        <button className="more" aria-expanded={whole} onClick={() => setWhole(!whole)}>
          {whole ? "收起" : "展开全部"}
        </button>
      )}
    </>
  );
}

// Where the highlight stands, when that is not simply where it was marked.
function StandingNote({ highlight, actions }: { highlight: Highlight; actions: EntryActions }) {
  const { standing } = highlight;
  if (standing.kind === "found") {
    return standing.exact ? null : (
      <p className="moved" title="重新读取后，这段文字和标记时不完全一样，这里是现在的文字">
        文字与标记时略有不同
      </p>
    );
  }
  return (
    <p className="lost-here">
      当前版本里找不到这段，它标在第 {highlight.revision} 版。
      <button className="quiet" onClick={() => actions.openRevision(highlight)}>
        打开第 {highlight.revision} 版
      </button>
    </p>
  );
}

type Keeping = { state: "idle" } | { state: "keeping" } | { state: "failed"; reason: string };

// What the reader noted on the highlight, written and changed in place. It is kept when the
// field is left or on Ctrl or ⌘ with Enter; Escape gives the change up. The field stays open
// until what was typed is kept, and holds it when it could not be. The entry's other actions
// stand at the foot, beside the control that opens the field.
function CommentField({
  highlight,
  editing,
  setEditing,
  children,
}: {
  highlight: Highlight;
  editing: boolean;
  setEditing: (editing: boolean) => void;
  children: ReactNode;
}) {
  const [draft, setDraft] = useState(() => unkept.get(highlight.id) ?? highlight.comment);
  const [keeping, setKeeping] = useState<Keeping>({ state: "idle" });
  const field = useRef<HTMLTextAreaElement>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  // A field left by a key, or being kept, is not left again by the blur that follows.
  const settled = useRef(false);
  const returnFocus = useRef(false);

  useEffect(() => {
    if (editing && draft !== highlight.comment) {
      unkept.set(highlight.id, draft);
    } else {
      unkept.delete(highlight.id);
    }
  }, [editing, draft, highlight.comment]);

  useLayoutEffect(() => {
    if (!editing && returnFocus.current) {
      returnFocus.current = false;
      editButton.current?.focus();
    }
  }, [editing]);

  function start() {
    settled.current = false;
    setDraft(highlight.comment);
    setKeeping({ state: "idle" });
    setEditing(true);
  }

  function close() {
    // The keyboard goes back to the control that opened the field, unless the reader has
    // taken it elsewhere.
    returnFocus.current = field.current === document.activeElement;
    setEditing(false);
  }

  async function keep() {
    settled.current = true;
    if (draft === highlight.comment) {
      setKeeping({ state: "idle" });
      close();
      return;
    }
    setKeeping({ state: "keeping" });
    try {
      await noteOnHighlight(highlight.item, highlight.id, draft);
      setKeeping({ state: "idle" });
      close();
    } catch (error) {
      settled.current = false;
      setKeeping({ state: "failed", reason: changeReason(error) });
    }
  }

  function giveUp() {
    settled.current = true;
    setDraft(highlight.comment);
    setKeeping({ state: "idle" });
    close();
  }

  function keyed(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (keeping.state === "keeping") {
      return;
    }
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void keep();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      giveUp();
    }
  }

  // The foot stands in the same place whether the field is open or not, so that what the
  // entry's actions hold stays while the comment is written.
  return (
    <>
      {editing ? (
        <div className="comment-field">
          <textarea
            ref={field}
            autoFocus
            aria-label="对这条高亮的评论"
            aria-describedby={`comment-keys-${highlight.id}`}
            placeholder="写下你的评论…"
            rows={3}
            value={draft}
            readOnly={keeping.state === "keeping"}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={keyed}
            onBlur={() => settled.current || void keep()}
          />
          {keeping.state === "keeping" && <p className="keeping">正在保存…</p>}
          {keeping.state === "failed" && (
            <p className="status failed" role="alert">
              评论没有保存：{keeping.reason}。写下的内容还在，离开输入框或按 Ctrl+Enter 再试。
            </p>
          )}
          <small id={`comment-keys-${highlight.id}`}>
            离开输入框或 Ctrl+Enter 保存 · Esc 放弃修改
          </small>
        </div>
      ) : (
        highlight.comment !== "" && <p className="comment-text">{highlight.comment}</p>
      )}
      <div className="entry-foot">
        {!editing && (
          <button ref={editButton} className="edit-comment" onClick={start}>
            {highlight.comment === "" ? "写评论" : "改评论"}
          </button>
        )}
        <span className="entry-actions">{children}</span>
      </div>
    </>
  );
}

// Why the service refused to remove a highlight, by highlight. The entry leaves the list as
// soon as its removal is asked for, and is drawn anew when the refusal puts it back, so the
// reason is kept apart from it.
const refusals = new Map<string, string>();
const refusalListeners = new Set<() => void>();

function refuse(highlightId: string, reason: string | undefined): void {
  if (reason === undefined) {
    refusals.delete(highlightId);
  } else {
    refusals.set(highlightId, reason);
  }
  refusalListeners.forEach((notify) => notify());
}

function onRefusal(notify: () => void): () => void {
  refusalListeners.add(notify);
  return () => {
    refusalListeners.delete(notify);
  };
}

type Removing = "idle" | "confirming";

// Removing a highlight takes its comment with it, so it is asked for twice.
function RemoveControl({ highlight, actions }: { highlight: Highlight; actions: EntryActions }) {
  const [removing, setRemoving] = useState<Removing>("idle");
  const problem = useSyncExternalStore(onRefusal, () => refusals.get(highlight.id));
  const removeButton = useRef<HTMLButtonElement>(null);
  const cancelled = useRef(false);

  useLayoutEffect(() => {
    if (cancelled.current && removing === "idle") {
      cancelled.current = false;
      removeButton.current?.focus();
    }
  }, [removing]);

  function cancel() {
    cancelled.current = true;
    setRemoving("idle");
  }

  async function confirm() {
    refuse(highlight.id, undefined);
    actions.removed();
    try {
      await unmark(highlight.item, highlight.id);
    } catch (error) {
      refuse(highlight.id, `没有移除：${changeReason(error)}`);
    }
  }

  return (
    <>
      {removing === "idle" && (
        <button
          ref={removeButton}
          className="quiet"
          aria-label="移除这条高亮"
          onClick={() => {
            refuse(highlight.id, undefined);
            setRemoving("confirming");
          }}
        >
          移除
        </button>
      )}
      {removing === "confirming" && (
        <span
          className="confirm"
          role="group"
          aria-label="确认移除高亮"
          onKeyDown={(event) => event.key === "Escape" && cancel()}
        >
          移除这条高亮{highlight.comment === "" ? "" : "和它的评论"}？
          <button className="danger" onClick={() => void confirm()}>
            移除
          </button>
          <button autoFocus onClick={cancel}>
            取消
          </button>
        </span>
      )}
      {problem !== undefined && (
        <span className="status failed" role="alert">
          {problem}
        </span>
      )}
    </>
  );
}

function quoteTaking(highlight: Highlight): Taking {
  return {
    excerpts: [
      {
        kind: "quote",
        cited: { anchors: anchorsOf(passageNow(highlight)), itemId: highlight.item },
      },
    ],
    leftOut: 0,
  };
}

// Scrolls the list the entry is in, and only that, so that the whole entry is in view, or its
// start where it is taller than the list: in the one-column layout, scrolling the page as well
// would move the reader under the drawer.
function bringIntoView(element: HTMLElement): void {
  const list = element.closest(".notebook-part");
  if (list === null) {
    return;
  }
  const shown = list.getBoundingClientRect();
  const box = element.getBoundingClientRect();
  if (box.top < shown.top) {
    list.scrollTop += box.top - shown.top - VIEW_MARGIN;
  } else if (box.bottom > shown.bottom) {
    list.scrollTop += Math.min(box.bottom - shown.bottom + VIEW_MARGIN, box.top - shown.top);
  }
}

// An opened highlight is brought into view once its entry is on show; one opened from the
// source is said, and one opened for a comment gets the comment's field.
function useOpening(
  opening: Opened | undefined,
  highlight: Highlight,
  entry: RefObject<HTMLLIElement | null>,
  setEditing: (editing: boolean) => void,
): void {
  useEffect(() => {
    if (opening === undefined || actedOn.has(opening)) {
      return;
    }
    actedOn.add(opening);
    if (entry.current !== null) {
      bringIntoView(entry.current);
    }
    if (opening.commenting) {
      setEditing(true);
    }
    if (!chosenInList.has(opening)) {
      const words = wordsOf(passageNow(highlight)).slice(0, SAID_WORDS).trim();
      say(`笔记本里打开了高亮：${words}${opening.commenting ? "。可以写评论了" : ""}`);
    }
  }, [opening]);
}

// One highlight in a list: its words, where it stands, its comment, and what can be done
// with it. Opening is the opened highlight while it is this one and the notebook is on show.
export const HighlightEntry = memo(function HighlightEntry({
  highlight,
  pressed,
  opened,
  opening,
  actions,
}: {
  highlight: Highlight;
  pressed: boolean;
  opened: boolean;
  opening: Opened | undefined;
  actions: EntryActions;
}) {
  const entry = useRef<HTMLLIElement>(null);
  const [editing, setEditing] = useState(() => unkept.has(highlight.id));
  useOpening(opening, highlight, entry, setEditing);

  return (
    <li
      ref={entry}
      className="highlight"
      data-opened={opened}
      data-standing={highlight.standing.kind}
    >
      <Words highlight={highlight} pressed={pressed} actions={actions} />
      <StandingNote highlight={highlight} actions={actions} />
      <CommentField highlight={highlight} editing={editing} setEditing={setEditing}>
        <NoteDown taking={quoteTaking(highlight)} label="记到笔记：这条高亮" />
        <RemoveControl highlight={highlight} actions={actions} />
      </CommentField>
    </li>
  );
});
