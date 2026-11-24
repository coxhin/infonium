import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
} from "react";
import type { Highlight } from "./client";
import { CutText, useCut } from "./cuttext";
import { noteOnHighlight } from "./highlights";
import { requestReason } from "./loading";

// The most the service keeps of one comment: COMMENT_CHARACTERS in src/infonium/highlight.py.
// The service counts code points and the field counts UTF-16 units, so a field held to this
// many never holds more than the service keeps.
const COMMENT_CHARACTERS = 10_000;
const AT_BOUND = `评论最多 ${COMMENT_CHARACTERS.toLocaleString()} 字，已经写满。`;
// A comment on show is cut after this many lines, as the marked words are cut.
const COMMENT_LINES = 6;
// The key code a browser gives a key that an input method takes while it composes.
const COMPOSING_KEY_CODE = 229;

type Keeping = { state: "idle" } | { state: "keeping" } | { state: "failed"; reason: string };

// What was typed into a comment and not kept, by highlight: the field comes back with it when
// the entry is drawn anew, as when its item is opened again. A reload loses it.
const unkept = new Map<string, string>();

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
}: {
  highlight: Highlight;
  editing: boolean;
  setEditing: (editing: boolean) => void;
  editButton: RefObject<HTMLButtonElement | null>;
}) {
  const [draft, setDraft] = useState(() => unkept.get(highlight.id) ?? highlight.comment);
  const [keeping, setKeeping] = useState<Keeping>({ state: "idle" });
  const field = useRef<HTMLTextAreaElement>(null);
  const cut = useCut(highlight.comment);
  // A field left by a key, or being kept, is not left again by the blur that follows.
  const settled = useRef(false);
  const returnFocus = useRef(false);
  const wasEditing = useRef(editing);

  useEffect(() => {
    if (editing && draft !== highlight.comment) {
      unkept.set(highlight.id, draft);
    } else {
      unkept.delete(highlight.id);
    }
  }, [editing, draft, highlight.comment]);

  // A field that opens starts from the comment as it stands, and takes the keyboard without
  // scrolling the page: in the one-column layout the drawer stands over the reader, and the
  // reader's place would be lost. The caret stands after what was noted before.
  useLayoutEffect(() => {
    const opening = editing && !wasEditing.current;
    wasEditing.current = editing;
    const opened = field.current;
    if (opening && opened !== null) {
      settled.current = false;
      setKeeping({ state: "idle" });
      setDraft(highlight.comment);
      opened.focus({ preventScroll: true });
      opened.setSelectionRange(highlight.comment.length, highlight.comment.length);
    } else if (!editing && returnFocus.current) {
      returnFocus.current = false;
      editButton.current?.focus({ preventScroll: true });
    }
  }, [editing]);

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
      setKeeping({ state: "failed", reason: requestReason(error) });
    }
  }

  function giveUp() {
    settled.current = true;
    setDraft(highlight.comment);
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
      void keep();
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
        aria-label="对这条高亮的评论"
        aria-describedby={`comment-keys-${highlight.id}`}
        placeholder="写下你的评论…"
        rows={3}
        maxLength={COMMENT_CHARACTERS}
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
        {draft.length >= COMMENT_CHARACTERS && <span className="at-bound">{AT_BOUND}</span>}
        离开输入框或 Ctrl+Enter 保存 · Esc 放弃修改
      </small>
    </div>
  );
}
