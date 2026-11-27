import { useEffect, useRef, useState, type FocusEvent, type FormEvent } from "react";
import type { Folder } from "./client";
import { FOLDER_NAME_LENGTH } from "./folders";
import { usePress } from "./pressing";
import "./foldername.css";

// What a field asks for: the name of a folder to be made, or a new name of a folder, which the
// field starts from.
export type NameAsked = { kind: "new" } | { kind: "rename"; folder: Folder };

// How a field was done with: the service answered the name sent, while the keyboard was still in
// the field or after it went elsewhere; the field was given up, by Escape or by Enter with nothing
// to send; or the keyboard left it, by a press elsewhere or Tab, with nothing typed to keep.
export type NameDone<Answer> =
  | { kind: "answered"; answer: Answer; keyboard: "kept" | "gone" }
  | { kind: "given up" }
  | { kind: "left" };

const NEW_NAME = "新分类的名称";

// A field that asks for a folder's name, in the place of what asked for it. It takes the keyboard
// when it opens, with the name it starts from selected. Enter sends the name where there is one
// to send, a name changed for a rename, and the field is done with once the service has answered;
// Escape, or Enter with nothing to send, gives it up. The keyboard is the reader's to take
// elsewhere: a field it leaves with a name typed stays as it is, with the name, and takes nothing
// back. A refusal is said under the field, which keeps what was typed. A field for a new folder
// has a press that sends it as Enter does.
export function FolderNameField<Answer>({
  asked,
  waiting = false,
  onSend,
  onDone,
}: {
  asked: NameAsked;
  // Whether what follows the name sent is awaited, as a move into the folder made.
  waiting?: boolean;
  onSend: (name: string) => Promise<Answer>;
  onDone: (done: NameDone<Answer>) => void;
}) {
  const from = asked.kind === "rename" ? asked.folder.name : "";
  const [typed, setTyped] = useState(from);
  const { press, ask } = usePress();
  const own = useRef<HTMLFormElement>(null);
  const field = useRef<HTMLInputElement>(null);
  // Once answered or given up, the field is done with, though it stands until its caller has
  // put it away, and the keyboard's leaving it then tells nothing.
  const finished = useRef(false);
  const busy = waiting || press.state === "asking";
  const name = typed.trim();
  const nothingToSend = name === "" || name === from;

  useEffect(() => {
    field.current?.focus();
    field.current?.select();
  }, []);

  function finish(done: NameDone<Answer>) {
    finished.current = true;
    onDone(done);
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) {
      return;
    }
    if (nothingToSend) {
      finish({ kind: "given up" });
      return;
    }
    ask(
      () => onSend(name),
      (answer) =>
        finish({
          kind: "answered",
          answer,
          keyboard: own.current?.contains(document.activeElement) === true ? "kept" : "gone",
        }),
    );
  }

  function left(event: FocusEvent<HTMLFormElement>) {
    const next = event.relatedTarget;
    const within = next instanceof Node && event.currentTarget.contains(next);
    if (!within && !finished.current && !busy && nothingToSend) {
      onDone({ kind: "left" });
    }
  }

  return (
    <form
      ref={own}
      className="folder-name"
      onSubmit={submit}
      onBlur={left}
      // A press in the field is not one on the row or the menu around it.
      onClick={(event) => event.stopPropagation()}
    >
      <input
        ref={field}
        aria-label={asked.kind === "new" ? NEW_NAME : `分类“${asked.folder.name}”的新名称`}
        placeholder={asked.kind === "new" ? NEW_NAME : undefined}
        maxLength={FOLDER_NAME_LENGTH}
        readOnly={busy}
        value={typed}
        onChange={(event) => setTyped(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            finish({ kind: "given up" });
          }
        }}
      />
      {asked.kind === "new" && <button aria-disabled={busy}>{busy ? "新建中…" : "新建"}</button>}
      {press.state === "refused" && (
        <p className="status failed" role="alert">
          {asked.kind === "new" ? "没有新建分类" : "没有改名"}：{press.reason}
        </p>
      )}
    </form>
  );
}
