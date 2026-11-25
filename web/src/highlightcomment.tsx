import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
} from "react";
import { announce } from "./announce";
import { requestReason, type Highlight } from "./client";
import { CutText, useCut } from "./cuttext";
import { highlightGone, noteOnHighlight } from "./highlights";
import { ToldElsewhere } from "./notedown";

// The most the service keeps of one comment: COMMENT_CHARACTERS in src/infonium/highlight.py.
// The service counts code points and the field counts UTF-16 units, so a field held to this
// many never holds more than the service keeps.
const COMMENT_CHARACTERS = 10_000;
const AT_BOUND = `批注最多 ${COMMENT_CHARACTERS.toLocaleString()} 字，已经写满。`;
const GONE = "这条高亮已在别处移除，批注没有保存。";
const UNKEPT = "批注没有保存";
const RETRY = "写下的内容还在，离开输入框或按 Ctrl+Enter 再试。";
// How long a press of 记到笔记 waits for its comment to be kept. The note's other presses wait
// their turn behind it, and the client sets no bound on a request, so a keeping that never
// answers would hold every later press in the tab. A keeping answers in milliseconds on a quiet
// machine; 15 seconds leaves a loaded one room, and a keeping not answered by then is taken as
// not answering for the press, while the reader can press again once it has.
const PRESS_WAIT_MS = 15_000;
const STILL_KEEPING = "批注还没有保存好，等保存好了再记";
// A comment on show is cut after this many lines, as the marked words are cut.
const COMMENT_LINES = 6;
// The key code a browser gives a key that an input method takes while it composes.
const COMPOSING_KEY_CODE = 229;

// Who tells the reader how a keeping went, settled when it is sent: the field, for a keeping the
// reader began by leaving it or by Ctrl+Enter; or the press of 记到笔记 that sent it, which says
// a failure on its own line and in the status line, so that the field only says how to go on.
type Teller = "field" | "press";

type Keeping =
  | { state: "idle" }
  | { state: "keeping" }
  | { state: "failed"; reason: string; teller: Teller };

// What came of keeping what was typed: the comment the service holds now, or why it holds
// nothing new.
type Kept = { kept: true; comment: string } | { kept: false; reason: string };

// A keeping of what was typed, under way or done, the text it sends, and who tells its end.
type Sending = { text: string; outcome: Promise<Kept>; teller: Teller };

// The comment as the reader sees it in an open field, once what was typed and not kept yet is
// kept; it rejects, saying why, when that cannot be kept.
export type CommentNow = () => Promise<string>;

// What the reader typed into a field: its text, and the comment the field showed when the typing
// began. From the first key on the field shows the reader's text until it closes; while that
// equals the comment it began from, it is no change. A field the reader has not typed into shows
// the comment as the highlights' store holds it, which another tab may change.
type Typed = { text: string; openedWith: string };

function changed(typed: Typed | undefined): typed is Typed {
  return typed !== undefined && typed.text !== typed.openedWith;
}

// What was typed into a comment and not kept, by highlight: the field comes back with it when
// the entry is drawn anew, as when its item is opened again. A reload loses it.
const unkept = new Map<string, Typed>();

// What came of a keeping by the time a press stops waiting for it; undefined while it is still
// under way then.
function withinPressWait(outcome: Promise<Kept>): Promise<Kept | undefined> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const waited = new Promise<undefined>((done) => {
    timer = setTimeout(() => done(undefined), PRESS_WAIT_MS);
  });
  return Promise.race([outcome, waited]).finally(() => clearTimeout(timer));
}

// Whether a comment was being typed when its entry was last drawn away.
export function typingUnkept(highlightId: string): boolean {
  return unkept.has(highlightId);
}

// What the reader noted on the highlight, written and changed in place. It is kept when the
// field is left or on Ctrl or ⌘ with Enter; Escape gives the change up. The field stays open
// until what was typed is kept, and holds it when it could not be. The entry opens the field
// with its own control, to which the keyboard goes back when the field closes.
export function CommentField({
  highlight,
  editing,
  setEditing,
  editButton,
  commentNow,
}: {
  highlight: Highlight;
  editing: boolean;
  setEditing: (editing: boolean) => void;
  editButton: RefObject<HTMLButtonElement | null>;
  // Holds how to have the comment as the reader sees it while the field is open, and nothing
  // while it is closed.
  commentNow: RefObject<CommentNow | null>;
}) {
  const [typed, setTyped] = useState<Typed | undefined>(() => unkept.get(highlight.id));
  const shown = typed?.text ?? highlight.comment;
  const [keeping, setKeeping] = useState<Keeping>({ state: "idle" });
  const field = useRef<HTMLTextAreaElement>(null);
  const cut = useCut(highlight.comment);
  // A field left by a key, or being kept, is not left again by the blur that follows.
  const settled = useRef(false);
  const returnFocus = useRef(false);
  const wasEditing = useRef(editing);
  // The latest keeping of what was typed, kept until it fails, so that what asks for the comment
  // while it is under way, or after it before the field is drawn again, sends nothing again.
  const sending = useRef<Sending | undefined>(undefined);

  useEffect(() => {
    if (editing && changed(typed)) {
      unkept.set(highlight.id, typed);
    } else {
      unkept.delete(highlight.id);
    }
  }, [editing, typed]);

  // A field that opens starts from the comment as it stands, and takes the keyboard without
  // scrolling the page: in the one-column layout the drawer stands over the reader, and the
  // reader's place would be lost. The caret stands after what was noted before.
  useLayoutEffect(() => {
    const opening = editing && !wasEditing.current;
    wasEditing.current = editing;
    const opened = field.current;
    if (opening && opened !== null) {
      settled.current = false;
      sending.current = undefined;
      setKeeping({ state: "idle" });
      setTyped(undefined);
      opened.focus({ preventScroll: true });
      opened.setSelectionRange(highlight.comment.length, highlight.comment.length);
    }
  }, [editing]);

  // A field closed with the keyboard in it gives the keyboard to the control that opens it again,
  // unless the reader took it elsewhere meanwhile. That control is drawn beside the field once
  // it closes, and is reached only after the drawing is done: a layout effect here runs before
  // the control's reference is set.
  useEffect(() => {
    if (editing || !returnFocus.current) {
      return;
    }
    returnFocus.current = false;
    const lost = document.activeElement === null || document.activeElement === document.body;
    if (lost) {
      editButton.current?.focus({ preventScroll: true });
    }
  }, [editing]);

  function close() {
    // The keyboard goes back to the control that opened the field, unless the reader has
    // taken it elsewhere.
    returnFocus.current = field.current === document.activeElement;
    setEditing(false);
  }

  async function send(text: string, teller: Teller): Promise<Kept> {
    setKeeping({ state: "keeping" });
    try {
      await noteOnHighlight(highlight.item, highlight.id, text);
    } catch (error) {
      // A highlight removed elsewhere has left the list with its entry and this field.
      if (highlightGone(error)) {
        if (teller === "field") {
          announce(GONE);
        }
        return { kept: false, reason: GONE };
      }
      settled.current = false;
      const reason = requestReason(error);
      setKeeping({ state: "failed", reason, teller });
      return { kept: false, reason: `${UNKEPT}：${reason}` };
    }
    setKeeping({ state: "idle" });
    close();
    return { kept: true, comment: text };
  }

  function keep(teller: Teller): Promise<Kept> {
    settled.current = true;
    if (!changed(typed)) {
      setKeeping({ state: "idle" });
      close();
      return Promise.resolve({ kept: true, comment: highlight.comment });
    }
    const sent: Sending = { text: typed.text, outcome: send(typed.text, teller), teller };
    sending.current = sent;
    void sent.outcome.then(({ kept }) => {
      if (!kept && sending.current === sent) {
        sending.current = undefined;
      }
    });
    return sent.outcome;
  }

  // A field with no change gives the comment as it stands and is left as it is; a keeping of the
  // very text typed, under way or done, is awaited, not sent again; other typing is kept first,
  // by the press that asks, which tells how it went. A press comes here before it takes the
  // keyboard, so that the field's blur finds the keeping begun.
  //
  // The press waits no longer than PRESS_WAIT_MS: past it, nothing goes into the note and the
  // presses after it go on, while the keeping goes on and keeps the comment when it answers. A
  // keeping the field began is told of by the field, should it fail, and the press then says
  // only that nothing was noted.
  async function commentAsSeen(): Promise<string> {
    if (!changed(typed)) {
      return highlight.comment;
    }
    if (sending.current?.text !== typed.text) {
      void keep("press");
    }
    const sent = sending.current;
    if (sent === undefined) {
      throw new Error(UNKEPT);
    }
    const outcome = await withinPressWait(sent.outcome);
    if (outcome === undefined) {
      throw new Error(STILL_KEEPING);
    }
    if (!outcome.kept) {
      throw sent.teller === "field" ? new ToldElsewhere(outcome.reason) : new Error(outcome.reason);
    }
    return outcome.comment;
  }

  // Set after every drawing, so that what is handed over is what the field holds now.
  useLayoutEffect(() => {
    commentNow.current = editing ? commentAsSeen : null;
  });
  useEffect(
    () => () => {
      commentNow.current = null;
    },
    [],
  );

  function giveUp() {
    settled.current = true;
    setTyped(undefined);
    setKeeping({ state: "idle" });
    close();
  }

  function keyed(event: KeyboardEvent<HTMLTextAreaElement>) {
    // A key an input method takes while it composes is the method's: Escape there cancels the
    // composition and Enter commits it, neither of which is the field's to act on.
    const composing =
      event.nativeEvent.isComposing || event.nativeEvent.keyCode === COMPOSING_KEY_CODE;
    if (keeping.state === "keeping" || composing) {
      return;
    }
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void keep("field");
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      giveUp();
    }
  }

  if (!editing) {
    return highlight.comment === "" ? null : (
      <div className="comment-text" dir="auto">
        <CutText cut={cut} lines={COMMENT_LINES} className="comment-words">
          {highlight.comment}
        </CutText>
        {cut.toggle}
      </div>
    );
  }
  return (
    <div className="comment-field">
      <textarea
        ref={field}
        dir="auto"
        aria-label="对这条高亮的批注"
        aria-describedby={`comment-keys-${highlight.id}`}
        placeholder="写下你的批注…"
        rows={3}
        maxLength={COMMENT_CHARACTERS}
        value={shown}
        readOnly={keeping.state === "keeping"}
        onChange={(event) => {
          setTyped({
            text: event.target.value,
            openedWith: typed?.openedWith ?? highlight.comment,
          });
        }}
        onKeyDown={keyed}
        onBlur={() => settled.current || void keep("field")}
      />
      {keeping.state === "keeping" && <p className="keeping">正在保存…</p>}
      {keeping.state === "failed" && (
        <p className="status failed" role={keeping.teller === "field" ? "alert" : undefined}>
          {keeping.teller === "field" ? `${UNKEPT}：${keeping.reason}。${RETRY}` : RETRY}
        </p>
      )}
      <small id={`comment-keys-${highlight.id}`}>
        {shown.length >= COMMENT_CHARACTERS && <span className="at-bound">{AT_BOUND}</span>}
        离开输入框或 Ctrl+Enter 保存 · Esc 放弃修改
      </small>
    </div>
  );
}
