import { useEffect, useRef, useState, type FormEvent } from "react";
import type { Folder } from "./client";
import { FOLDER_NAME_LENGTH } from "./folders";
import { usePress, type Press } from "./pressing";
import "./foldername.css";

// What a field asks for: the name of a folder to be made, or a new name of a folder, which the
// field starts from.
export type NameAsked = { kind: "new" } | { kind: "rename"; folder: Folder };

// How a field was done with: the service answered the name sent, or the field was given up, by
// Escape or by Enter with nothing to send.
export type NameDone<Answer> = { kind: "answered"; answer: Answer } | { kind: "given up" };

// The field as its own leaving finds it: what is typed, the press, and the sending of the name.
export type NameField = { typed: string; press: Press; send: () => void };

const NEW_NAME = "新分类的名称";

// A field that asks for a folder's name, in the place of what asked for it. It takes the keyboard
// when it opens, with the name it starts from selected. Enter sends the name where there is one
// to send, a name changed for a rename, and the field is done with once the service has answered;
// Escape, or Enter with nothing to send, gives it up. A refusal is said under the field, which
// keeps what was typed. A field for a new folder has a press that sends it as Enter does.
export function FolderNameField<Answer>({
  asked,
  waiting = false,
  onSend,
  onDone,
  onBlur,
}: {
  asked: NameAsked;
  // Whether what follows the name sent is awaited, as a move into the folder made.
  waiting?: boolean;
  onSend: (name: string) => Promise<Answer>;
  onDone: (done: NameDone<Answer>) => void;
  onBlur: (field: NameField) => void;
}) {
  const from = asked.kind === "rename" ? asked.folder.name : "";
  const [typed, setTyped] = useState(from);
  const { press, ask } = usePress();
  const field = useRef<HTMLInputElement>(null);
  const busy = waiting || press.state === "asking";

  useEffect(() => {
    field.current?.focus();
    field.current?.select();
  }, []);

  function send() {
    const name = typed.trim();
    if (name === "" || name === from) {
      onDone({ kind: "given up" });
      return;
    }
    ask(
      () => onSend(name),
      (answer) => onDone({ kind: "answered", answer }),
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!busy) {
      send();
    }
  }

  return (
    <form
      className="folder-name"
      onSubmit={submit}
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
            onDone({ kind: "given up" });
          }
        }}
        onBlur={() => onBlur({ typed, press, send })}
      />
      {asked.kind === "new" && (
        <button aria-disabled={busy}>{busy ? "新建中…" : "新建"}</button>
      )}
      {press.state === "refused" && (
        <p className="status failed" role="alert">
          {asked.kind === "new" ? "没有新建分类" : "没有改名"}：{press.reason}
        </p>
      )}
    </form>
  );
}
