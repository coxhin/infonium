import { memo, useEffect, useRef, useState, type MouseEvent, type RefObject } from "react";
import { anchorsOf, type Highlight, type Passage } from "./client";
import { announce } from "./announce";
import { CutText, useCut } from "./cuttext";
import { CommentField, typingUnkept, type SettleComment } from "./highlightcomment";
import { RemoveControl } from "./highlightremoval";
import { wasMarkedAgain, type Opened } from "./highlights";
import { NOTE_DOWN_PRESS, NoteDown, type Taking } from "./notedown";

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

const WORDS_JOIN = "\n";
// The marked words on show are cut after this many lines.
const WORDS_LINES = 4;
// How many characters of the marked words name an entry's press, and are said when the entry
// is opened from the source; a passage may run through many blocks.
const NAMED_CHARACTERS = 40;
// How far back from the cut, in characters, a space is looked for to end on a whole word.
const WORD_REACH = 15;
const CUT_MARK = "…";
// How far from the edge of the list, in pixels, an entry brought into view stands.
const VIEW_MARGIN = 16;

function wordsOf(passage: Passage): string {
  return passage.anchors.map((anchor) => anchor.quote).join(WORDS_JOIN);
}

// The start of a text, for a name or a saying: whole characters, ending on a whole word where
// the text has spaces near the cut.
function startOf(text: string): string {
  const characters = Array.from(text.replace(/\s+/g, " ").trim());
  if (characters.length <= NAMED_CHARACTERS) {
    return characters.join("");
  }
  const head = characters.slice(0, NAMED_CHARACTERS);
  const space = head.lastIndexOf(" ");
  const kept = space >= NAMED_CHARACTERS - WORD_REACH ? head.slice(0, space) : head;
  return `${kept.join("")}${CUT_MARK}`;
}

// Where a highlight stands now, as the passage to show and quote: where the document on show
// holds it, or as it was marked where that document has lost it.
function passageNow(highlight: Highlight): Passage {
  return highlight.standing.kind === "found" ? highlight.standing.passage : highlight.passage;
}

// The marked words, cut after a few lines; the rest is in reach of a press. Pressed is whether
// the passage is the one on show, where the list can show it; a list that opens the item does
// not, and its press is no toggle.
function Words({
  highlight,
  pressed,
  actions,
}: {
  highlight: Highlight;
  pressed: boolean | undefined;
  actions: EntryActions;
}) {
  const words = wordsOf(passageNow(highlight));
  const cut = useCut(words);
  const text = (
    <CutText cut={cut} lines={WORDS_LINES} className="words-text">
      {words}
    </CutText>
  );
  return (
    <>
      {highlight.standing.kind === "found" ? (
        <button
          className="words"
          dir="auto"
          aria-label={startOf(words)}
          aria-pressed={pressed}
          onClick={() => actions.press(highlight)}
        >
          {text}
        </button>
      ) : (
        <blockquote className="words" dir="auto">
          {text}
        </blockquote>
      )}
      {cut.toggle}
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

function quoteTaking(highlight: Highlight, comment: string): Taking {
  return {
    excerpts: [
      {
        kind: "quote",
        cited: { anchors: anchorsOf(passageNow(highlight)), itemId: highlight.item },
        comment,
      },
    ],
    leftOut: 0,
  };
}

function keepFieldFocused(event: MouseEvent): void {
  if (event.target instanceof Element && event.target.closest(`.${NOTE_DOWN_PRESS}`) !== null) {
    event.preventDefault();
  }
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

const MARKED_ALREADY = "这段已经标过了，就是这一条。";

// An opened highlight is brought into view once its entry is on show; one opened from the
// source is said, and one opened for a comment gets the comment's field. One the reader marked
// again says that it was marked already, on the entry until another highlight is opened.
function useOpening(
  opening: Opened | undefined,
  highlight: Highlight,
  entry: RefObject<HTMLLIElement | null>,
  setEditing: (editing: boolean) => void,
): boolean {
  const [again, setAgain] = useState(false);
  useEffect(() => {
    if (opening === undefined) {
      setAgain(false);
      return;
    }
    if (actedOn.has(opening)) {
      return;
    }
    actedOn.add(opening);
    setAgain(wasMarkedAgain(opening));
    if (opening.commenting) {
      setEditing(true);
    }
    // Once the field that opens is drawn: the entry, and then its field, which matters more
    // where the entry is taller than the list.
    requestAnimationFrame(() => {
      const shown = entry.current;
      if (shown === null) {
        return;
      }
      bringIntoView(shown);
      const field = shown.querySelector("textarea");
      if (field !== null) {
        bringIntoView(field);
      }
    });
    if (!chosenInList.has(opening)) {
      const words = startOf(wordsOf(passageNow(highlight)));
      const opened = wasMarkedAgain(opening) ? MARKED_ALREADY : "笔记本里打开了高亮：";
      announce(`${opened}${words}${opening.commenting ? "。可以写批注了" : ""}`);
    }
  }, [opening]);
  return again;
}

// One highlight in a list: its words, where it stands, its comment, and at its foot the
// control that opens the comment's field and what else can be done with it. Opening is the
// opened highlight while it is this one and the notebook is on show.
export const HighlightEntry = memo(function HighlightEntry({
  highlight,
  pressed,
  opened,
  opening,
  actions,
}: {
  highlight: Highlight;
  pressed: boolean | undefined;
  opened: boolean;
  opening: Opened | undefined;
  actions: EntryActions;
}) {
  const entry = useRef<HTMLLIElement>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  const settler = useRef<SettleComment>(null);
  const [editing, setEditing] = useState(() => typingUnkept(highlight.id));
  const markedAgain = useOpening(opening, highlight, entry, setEditing);

  return (
    <li
      ref={entry}
      className="highlight"
      data-opened={opened}
      data-standing={highlight.standing.kind}
      data-commented={highlight.comment !== ""}
    >
      <Words highlight={highlight} pressed={pressed} actions={actions} />
      {markedAgain && <p className="marked-again">{MARKED_ALREADY}</p>}
      <StandingNote highlight={highlight} actions={actions} />
      <CommentField
        highlight={highlight}
        editing={editing}
        setEditing={setEditing}
        editButton={editButton}
        settler={settler}
      />
      <div className="entry-foot">
        {!editing && (
          <button ref={editButton} className="edit-comment" onClick={() => setEditing(true)}>
            {highlight.comment === "" ? "写批注" : "改批注"}
          </button>
        )}
        {/* Pressing 记到笔记 leaves the keyboard in an open comment's field: leaving it would
            keep the comment and close the field, which moves the control from under the
            pointer before the press ends. The press keeps the comment itself. */}
        <span className="entry-actions" onMouseDown={keepFieldFocused}>
          <NoteDown
            taking={quoteTaking(highlight, highlight.comment)}
            settled={async () =>
              quoteTaking(highlight, (await settler.current?.()) ?? highlight.comment)
            }
            label="记到笔记：这条高亮"
          />
          <RemoveControl highlight={highlight} onRemoved={actions.removed} />
        </span>
      </div>
    </li>
  );
});
