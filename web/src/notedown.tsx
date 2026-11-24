import { useState } from "react";
import type { Anchor, Statement } from "./client";
import { say } from "./heard";
import { reasonOf } from "./loading";
import { noteDown, showNote, type Cited, type Excerpt, type Noted } from "./notes";
import "./notedown.css";

// What is put into a note at one press, in order, and how many citations were left out of it
// because the item they lie in could not be told.
export type Taking = { excerpts: Excerpt[]; leftOut: number };

type Noting =
  | { state: "idle" }
  | { state: "writing" }
  | { state: "noted"; noted: Noted; leftOut: number }
  | { state: "failed"; reason: string };

// A statement infonium wrote, with one cited passage for each of its citations whose item can
// be told.
export function statementTaking(
  statements: Statement[],
  itemOf: (anchor: Anchor) => string | undefined,
): Taking {
  let leftOut = 0;
  const excerpts = statements.map((statement): Excerpt => {
    const cited = statement.anchors.flatMap((anchor): Cited[] => {
      const itemId = itemOf(anchor);
      if (itemId === undefined) {
        leftOut += 1;
        return [];
      }
      return [{ anchors: [anchor], itemId }];
    });
    return { kind: "statement", text: statement.text, cited };
  });
  return { excerpts, leftOut };
}

function leftOutTitle(leftOut: number): string {
  return `${leftOut} 处引文说不出出自哪一篇，记的时候不带上`;
}

function leftOutNote(leftOut: number): string {
  return leftOut === 0 ? "" : `；${leftOut} 处引文说不出出自哪一篇，没有带上`;
}

// The excerpts go into the note one after another, each awaited, so that they arrive in the
// note in their order; the note that took the last is the one said.
async function noteAll(excerpts: Excerpt[]): Promise<Noted> {
  let noted: Noted | undefined;
  for (const excerpt of excerpts) {
    noted = await noteDown(excerpt);
  }
  if (noted === undefined) {
    throw new Error("没有可以记下的内容");
  }
  return noted;
}

// The control that puts what infonium wrote, or a highlight, into the note being written.
// It is quiet until the pointer is over what it belongs to or the keyboard is in it; once it
// has something to say, it stays on show.
export function NoteDown({ taking, label }: { taking: Taking; label: string }) {
  const [noting, setNoting] = useState<Noting>({ state: "idle" });

  async function press() {
    const { excerpts, leftOut } = taking;
    setNoting({ state: "writing" });
    try {
      const noted = await noteAll(excerpts);
      setNoting({ state: "noted", noted, leftOut });
      say(`已记到笔记《${noted.title}》${leftOutNote(leftOut)}`);
    } catch (error) {
      const reason = reasonOf(error);
      setNoting({ state: "failed", reason });
      say(`没有记到笔记：${reason}`);
    }
  }

  return (
    <span className="notedown" data-state={noting.state}>
      <button
        className="notedown-press"
        aria-label={label}
        title={taking.leftOut === 0 ? undefined : leftOutTitle(taking.leftOut)}
        disabled={noting.state === "writing"}
        onClick={() => void press()}
      >
        {noting.state === "writing" ? "正在记…" : "记到笔记"}
      </button>
      {noting.state === "noted" && (
        <span className="notedown-said">
          已记到《{noting.noted.title}》{leftOutNote(noting.leftOut)}
          <button className="notedown-show" onClick={() => showNote(noting.noted.noteId)}>
            查看
          </button>
        </span>
      )}
      {noting.state === "failed" && (
        <span className="notedown-said failed">没有记下：{noting.reason}</span>
      )}
    </span>
  );
}
