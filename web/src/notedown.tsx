import { useRef, useState } from "react";
import type { Anchor, Statement } from "./client";
import { announce } from "./announce";
import { reasonOf } from "./loading";
import { inTurn, showNote, type Cited, type Excerpt, type Noted, type PutDown } from "./notes";
import type { WordsPiece } from "./statementwords";
import "./notedown.css";

// One excerpt a press puts into a note, as it stands when the press comes: a highlight's
// comment may still be being typed then, and is kept first. It rejects, saying why, when the
// excerpt cannot be had.
export type PressedExcerpt = () => Promise<Excerpt>;

// What is put into a note at one press, in order, and how many citations were left out of it
// because the item they lie in could not be told. The control is drawn and pressed from it.
export type Taking = { excerpts: PressedExcerpt[]; leftOut: number };

// A failure the reader was told of where it happened, as a comment's field tells of a keeping
// it began: the control then shows only that nothing was noted, and announces nothing.
export class ToldElsewhere extends Error {}

// A failure says how many of the excerpts, counted from the first, the note took before it,
// and which note that was, so that a press after it puts in only the rest; and why, unless the
// reader was told why elsewhere.
type Noting =
  | { state: "idle" }
  | { state: "writing" }
  | { state: "noted"; noted: Noted; leftOut: number }
  | { state: "failed"; reason: string | undefined; taken: number; noted: Noted | undefined };

// A statement infonium wrote, with its words as they are shown, and one cited passage for each
// of its citations whose item can be told.
export function statementTaking(
  statements: Statement[],
  itemOf: (anchor: Anchor) => string | undefined,
  wordsOf: (text: string) => WordsPiece[],
): Taking {
  let leftOut = 0;
  const excerpts = statements.map((statement): PressedExcerpt => {
    const cited = statement.anchors.flatMap((anchor): Cited[] => {
      const itemId = itemOf(anchor);
      if (itemId === undefined) {
        leftOut += 1;
        return [];
      }
      return [{ anchors: [anchor], itemId }];
    });
    const excerpt: Excerpt = { kind: "statement", words: wordsOf(statement.text), cited };
    return () => Promise.resolve(excerpt);
  });
  return { excerpts, leftOut };
}

function leftOutTitle(leftOut: number): string {
  return `${leftOut} 处引文说不出出自哪一篇，记的时候不带上`;
}

function leftOutNote(leftOut: number): string {
  return leftOut === 0 ? "" : `；${leftOut} 处引文说不出出自哪一篇，没有带上`;
}

// Why the note took no more of the excerpts, unless the reader was told why elsewhere, and
// how far it had come.
class StoppedPartWay extends Error {
  constructor(
    readonly reason: string | undefined,
    readonly taken: number,
    readonly noted: Noted | undefined,
  ) {
    super(reason);
  }
}

// The excerpts from the given one on, begun at the press, go into the note one after another,
// each awaited, so that they arrive in the note in their order; the note that took the last is
// the one said.
async function noteFrom(
  pending: Promise<Excerpt>[],
  taken: number,
  noted: Noted | undefined,
  putExcerpt: PutDown,
): Promise<Noted> {
  let last = noted;
  for (const [index, excerpt] of pending.entries()) {
    try {
      last = await putExcerpt(await excerpt);
    } catch (error) {
      const reason = error instanceof ToldElsewhere ? undefined : reasonOf(error);
      throw new StoppedPartWay(reason, taken + index, last);
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
  const why = noting.reason === undefined ? "" : `：${noting.reason}`;
  if (noting.taken === 0 || noting.noted === undefined) {
    return `没有记下${why}`;
  }
  const rest = count - noting.taken;
  return `《${noting.noted.title}》记下了前 ${noting.taken} 句，其余 ${rest} 句没有记下${why}`;
}

function pressText(noting: Noting, rest: number): string {
  if (noting.state === "writing") {
    return "正在记…";
  }
  return noting.state === "failed" && noting.taken > 0 ? `记下其余 ${rest} 句` : "记到笔记";
}

// How the control is drawn: as its words, on a line or in a foot of its own, or as a mark in the
// gutter beside a statement, which always keeps its room, so that showing it moves no line.
export type NoteDownShape = "words" | "mark";

// The class of a statement that keeps a gutter for its control's mark.
export const NOTE_GUTTER = "note-gutter";

// The mark that stands for the control beside a statement; the control's name is its label.
const MARK = "✎";
const MARK_WRITING = "…";

// What the pointer is told of the control: a mark is named in words, and either says how many
// citations it leaves out.
function pressTitle(shape: NoteDownShape, leftOut: number): string | undefined {
  const named = shape === "mark" ? "记到笔记" : undefined;
  if (leftOut === 0) {
    return named;
  }
  return named === undefined ? leftOutTitle(leftOut) : `${named}（${leftOutTitle(leftOut)}）`;
}

// The control that puts what infonium wrote, or a highlight, into the note being written.
// It is quiet until the pointer is over what it belongs to or the keyboard is in it, and
// keeps its room meanwhile; once it has something to say, it stays on show.
//
// A press by the pointer takes the keyboard only once it is a press, not as the button goes
// down: a comment field open elsewhere is then left, and keeps its comment by itself, only after
// the press, so that the field closing cannot move the control from under the pointer before the
// press ends. A press by the keyboard finds it here already. While it writes, the control
// takes no press but keeps the keyboard, as a disabled button would not, so that Enter presses
// it again once it is done or has failed.
export function NoteDown({
  taking,
  label,
  shape = "words",
}: {
  taking: Taking;
  label: string;
  shape?: NoteDownShape;
}) {
  const [noting, setNoting] = useState<Noting>({ state: "idle" });
  const pressButton = useRef<HTMLButtonElement>(null);

  const { excerpts, leftOut } = taking;
  // After a failure part way, a press puts in only what the note has not taken.
  const taken = noting.state === "failed" ? noting.taken : 0;
  const rest = excerpts.length - taken;

  async function press() {
    if (noting.state === "writing") {
      pressButton.current?.focus({ preventScroll: true });
      return;
    }
    // The excerpts are begun before the press takes the keyboard, so that a comment still being
    // typed is kept by the press, which tells how that went, and not by the field it leaves.
    const pending = excerpts.slice(taken).map((excerpt) => excerpt());
    // One that is not reached, as after a failure before it, is not awaited.
    pending.forEach((excerpt) => void excerpt.catch(() => undefined));
    pressButton.current?.focus({ preventScroll: true });
    setNoting({ state: "writing" });
    const before = noting.state === "failed" ? noting.noted : undefined;
    try {
      const noted = await inTurn((putExcerpt) => noteFrom(pending, taken, before, putExcerpt));
      setNoting({ state: "noted", noted, leftOut });
      announce(`已记到笔记《${noted.title}》${leftOutNote(leftOut)}`);
    } catch (error) {
      const failed =
        error instanceof StoppedPartWay
          ? { reason: error.reason, taken: error.taken, noted: error.noted }
          : { reason: reasonOf(error), taken, noted: undefined };
      const next = { state: "failed" as const, ...failed };
      setNoting(next);
      if (next.reason !== undefined) {
        announce(failureNote(next, excerpts.length));
      }
    }
  }

  return (
    <span className="notedown" data-state={noting.state} data-shape={shape}>
      <button
        ref={pressButton}
        className="notedown-press"
        aria-label={taken === 0 ? label : undefined}
        title={pressTitle(shape, leftOut)}
        aria-disabled={noting.state === "writing"}
        onMouseDown={(event) => event.preventDefault()}
        onClick={() => void press()}
      >
        {shape === "mark" ? (
          <span aria-hidden="true">{noting.state === "writing" ? MARK_WRITING : MARK}</span>
        ) : (
          pressText(noting, rest)
        )}
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
