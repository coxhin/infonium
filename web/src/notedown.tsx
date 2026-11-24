import { useState } from "react";
import type { Anchor, Statement } from "./client";
import { announce } from "./announce";
import { reasonOf } from "./loading";
import { noteDown, showNote, type Cited, type Excerpt, type Noted } from "./notes";
import "./notedown.css";

// What is put into a note at one press, in order, and how many citations were left out of it
// because the item they lie in could not be told.
export type Taking = { excerpts: Excerpt[]; leftOut: number };

// A failure says how many of the excerpts, counted from the first, the note took before it,
// and which note that was, so that a press after it puts in only the rest.
type Noting =
  | { state: "idle" }
  | { state: "writing" }
  | { state: "noted"; noted: Noted; leftOut: number }
  | { state: "failed"; reason: string; taken: number; noted: Noted | undefined };

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

// Why the note took no more of the excerpts, and how far it had come.
class StoppedPartWay extends Error {
  constructor(
    message: string,
    readonly taken: number,
    readonly noted: Noted | undefined,
  ) {
    super(message);
  }
}

// The excerpts from the given one on go into the note one after another, each awaited, so that
// they arrive in the note in their order; the note that took the last is the one said.
async function noteFrom(
  excerpts: Excerpt[],
  taken: number,
  noted: Noted | undefined,
): Promise<Noted> {
  let last = noted;
  for (const [at, excerpt] of excerpts.entries()) {
    if (at >= taken) {
      try {
        last = await noteDown(excerpt);
      } catch (error) {
        throw new StoppedPartWay(reasonOf(error), at, last);
      }
    }
  }
  if (last === undefined) {
    throw new StoppedPartWay("没有可以记下的内容", 0, undefined);
  }
  return last;
}

// What a failure says: of an excerpt in several, how many the note took and that the rest were
// not taken.
function failureNote(noting: Extract<Noting, { state: "failed" }>, count: number): string {
  if (noting.taken === 0 || noting.noted === undefined) {
    return `没有记下：${noting.reason}`;
  }
  const rest = count - noting.taken;
  return `《${noting.noted.title}》记下了前 ${noting.taken} 句，其余 ${rest} 句没有记下：${noting.reason}`;
}

function pressText(noting: Noting, rest: number): string {
  if (noting.state === "writing") {
    return "正在记…";
  }
  return noting.state === "failed" && noting.taken > 0 ? `记下其余 ${rest} 句` : "记到笔记";
}

// The control that puts what infonium wrote, or a highlight, into the note being written.
// It is quiet until the pointer is over what it belongs to or the keyboard is in it; once it
// has something to say, it stays on show.
export function NoteDown({ taking, label }: { taking: Taking; label: string }) {
  const [noting, setNoting] = useState<Noting>({ state: "idle" });

  const { excerpts, leftOut } = taking;
  // After a failure part way, a press puts in only what the note has not taken.
  const taken = noting.state === "failed" ? noting.taken : 0;
  const rest = excerpts.length - taken;

  async function press() {
    setNoting({ state: "writing" });
    try {
      const noted = await noteFrom(
        excerpts,
        taken,
        noting.state === "failed" ? noting.noted : undefined,
      );
      setNoting({ state: "noted", noted, leftOut });
      announce(`已记到笔记《${noted.title}》${leftOutNote(leftOut)}`);
    } catch (error) {
      const failed =
        error instanceof StoppedPartWay
          ? { reason: error.message, taken: error.taken, noted: error.noted }
          : { reason: reasonOf(error), taken, noted: undefined };
      const next = { state: "failed" as const, ...failed };
      setNoting(next);
      announce(failureNote(next, excerpts.length));
    }
  }

  return (
    <span className="notedown" data-state={noting.state}>
      <button
        className="notedown-press"
        aria-label={taken === 0 ? label : undefined}
        title={leftOut === 0 ? undefined : leftOutTitle(leftOut)}
        disabled={noting.state === "writing"}
        onClick={() => void press()}
      >
        {pressText(noting, rest)}
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
        <span className="notedown-said failed">{failureNote(noting, excerpts.length)}</span>
      )}
    </span>
  );
}
