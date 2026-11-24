import { useSyncExternalStore } from "react";
import { readEntry, writeNote, type Anchors } from "./client";
import { itemTitle } from "./entry";
import { appended, quoteMarkdown, statementMarkdown } from "./notemarkdown";
import {
  addLinked,
  currentId,
  adopt,
  failureText,
  freshen,
  handIn,
  letGo,
  newPassageId,
  NotSent,
  OVERTAKEN,
  REMOVED,
  sessionOf,
  textOf,
  whenKept,
  withdraw,
  writerOf,
  type Linked,
  type Ready,
  type Text,
} from "./notekeeping";
import { noteName, openedNote, openNote } from "./notelist";

// What is put into a note from elsewhere in the app, the one way to put it there, and the one
// way to ask for a note to be shown.

// A passage that an excerpt rests on, and the item it lies in.
export type Cited = { anchors: Anchors; itemId: string };

// A statement infonium wrote, of a digest or an answer, with the passages it cites; or a
// passage quoted from a source.
export type Excerpt =
  | { kind: "statement"; text: string; cited: Cited[] }
  | { kind: "quote"; cited: Cited };

// Where an excerpt went: the note, and what it is called.
export type Noted = { noteId: string; title: string };

// The note the reader asked to see. Each asking is told from the one before it, so that
// asking for the same note again shows it again.
export type Shown = { noteId: string; asking: number };

const UNTITLED_SOURCE = "摘录";
// What a source is called in an excerpt when it cannot be read; the excerpt is then refused
// or kept as the note's own request says.
const UNNAMED_SOURCE = "原文";
// How many times an excerpt is put into a note that was written elsewhere meanwhile.
const ATTEMPTS = 3;

const listeners = new Set<() => void>();
let shown: Shown | undefined;
// The note an excerpt was last put into in this tab, kept with the tab so that a reload keeps it.
const LAST_NOTED_KEY = "infonium.lastNoted";
let lastNoted: string | undefined = sessionStorage.getItem(LAST_NOTED_KEY) ?? undefined;
let excerpts: Promise<unknown> = Promise.resolve();

// What the sources of an excerpt are called, by item.
async function sourceTitles(cited: Cited[]): Promise<Map<string, string>> {
  const itemIds = [...new Set(cited.map((one) => one.itemId))];
  const titles = await Promise.all(
    itemIds.map((itemId) =>
      readEntry(itemId).then(
        (entry) => itemTitle(entry),
        () => UNNAMED_SOURCE,
      ),
    ),
  );
  return new Map(itemIds.map((itemId, index) => [itemId, titles[index] ?? UNNAMED_SOURCE]));
}

function citedOf(excerpt: Excerpt): Cited[] {
  return excerpt.kind === "quote" ? [excerpt.cited] : excerpt.cited;
}

// An excerpt as Markdown, with the passages it links to under ids that the note does not use.
function written(
  excerpt: Excerpt,
  titles: ReadonlyMap<string, string>,
  taken: (passageId: string) => boolean,
): { markdown: string; linked: Map<string, Linked> } {
  const linked = new Map<string, Linked>();
  for (const one of citedOf(excerpt)) {
    const passageId = newPassageId((candidate) => taken(candidate) || linked.has(candidate));
    linked.set(passageId, {
      passage: { anchors: one.anchors },
      itemId: one.itemId,
      title: titles.get(one.itemId) ?? UNNAMED_SOURCE,
      whereabouts: { kind: "found", anchors: one.anchors, exact: true },
    });
  }
  const [passageId = ""] = linked.keys();
  const markdown =
    excerpt.kind === "quote"
      ? quoteMarkdown(
          excerpt.cited.anchors.map((anchor) => anchor.quote),
          titles.get(excerpt.cited.itemId) ?? UNNAMED_SOURCE,
          passageId,
        )
      : statementMarkdown(excerpt.text, [...linked.keys()]);
  return { markdown, linked };
}

function draftPassages(linked: ReadonlyMap<string, Linked>): Text["passages"] {
  return [...linked].map(([id, { passage }]) => ({ id, passage }));
}

function sourcesOf(titles: ReadonlyMap<string, string>): string {
  return [...new Set(titles.values())].map((title) => `《${title}》`).join("、");
}

// Why an excerpt is not in a note: what was refused, named with its sources.
function notNoted(error: unknown, titles: ReadonlyMap<string, string>): Error {
  if (error instanceof NotSent) {
    return error;
  }
  return new Error(`摘自${sourcesOf(titles)}的内容没有记下：${failureText(error)}`);
}

async function intoNewNote(excerpt: Excerpt, titles: ReadonlyMap<string, string>): Promise<Noted> {
  const { markdown, linked } = written(excerpt, titles, () => false);
  const [first] = linked.values();
  const title = first?.title || UNTITLED_SOURCE;
  const note = await writeNote({ title, markdown, passages: draftPassages(linked), edition: 1 });
  adopt(note);
  return { noteId: note.id, title };
}

function readyNow(noteId: string): Ready | undefined {
  const session = sessionOf(noteId);
  return session?.state === "ready" ? session : undefined;
}

// Puts an excerpt at the end of a note: the note is read again unless this visit holds text of
// it not kept yet, and the note's text with the excerpt is sent. Only once the service has kept
// it does the excerpt stand in the open editor, so that an excerpt refused never stands in the
// note. A note written elsewhere meanwhile that is not open is read again and the excerpt put
// at its end once more. Undefined when the note is not there.
async function intoNote(
  noteId: string,
  excerpt: Excerpt,
  titles: ReadonlyMap<string, string>,
): Promise<Noted | undefined> {
  for (let attempt = 0; attempt < ATTEMPTS; attempt += 1) {
    const read = await freshen(noteId);
    if (read === undefined) {
      return undefined;
    }
    if (read.keeping.state === "overtaken") {
      throw new NotSent(OVERTAKEN);
    }
    if (read.keeping.state === "removed") {
      throw new NotSent(REMOVED);
    }
    // What is under way goes first, so that the excerpt's own request is sent at once and
    // what comes of it is its own.
    if (read.onItsWay.length > 0) {
      await whenKept(noteId, read.handedIn).catch(() => undefined);
    }
    const ready = readyNow(noteId);
    if (ready === undefined) {
      return undefined;
    }
    const editor = writerOf(noteId);
    const before = editor?.text() ?? textOf(ready);
    const { markdown, linked } = written(
      excerpt,
      titles,
      (passageId) =>
        ready.linked.has(passageId) || before.passages.some(({ id }) => id === passageId),
    );
    addLinked(noteId, linked);
    const number = handIn(
      noteId,
      {
        title: before.title,
        markdown: appended(before.markdown, markdown),
        passages: [...before.passages, ...draftPassages(linked)],
      },
      false,
      true,
    );
    try {
      await whenKept(noteId, number);
    } catch (error) {
      if (
        error instanceof NotSent &&
        editor === undefined &&
        readyNow(noteId)?.keeping.state === "overtaken"
      ) {
        await letGo(noteId);
        continue;
      }
      // The note holds again what it held before the excerpt, which the open editor shows, so
      // that whatever the reader does next sends only that.
      withdraw(noteId, before);
      throw error;
    }
    editor?.append(markdown);
    return { noteId: currentId(noteId), title: noteName(before.title, appended(before.markdown, markdown)) };
  }
  throw new NotSent(OVERTAKEN);
}

async function putDown(excerpt: Excerpt): Promise<Noted> {
  const titles = await sourceTitles(citedOf(excerpt));
  const target = openedNote() ?? lastNoted;
  try {
    const noted =
      (target === undefined ? undefined : await intoNote(target, excerpt, titles)) ??
      (await intoNewNote(excerpt, titles));
    lastNoted = noted.noteId;
    sessionStorage.setItem(LAST_NOTED_KEY, noted.noteId);
    return noted;
  } catch (error) {
    throw notNoted(error, titles);
  }
}

// Puts an excerpt at the end of the note being written, with links to its passages, and makes
// a note for it when none is being written; the result says where it went, once it is kept.
// Excerpts are put down one after another, in the order they came.
export function noteDown(excerpt: Excerpt): Promise<Noted> {
  const noted = excerpts.then(() => putDown(excerpt));
  excerpts = noted.catch(() => undefined);
  return noted;
}

// Asks for a note to be shown: the notebook comes on show with the note open in it.
export function showNote(noteId: string): void {
  shown = { noteId, asking: (shown?.asking ?? 0) + 1 };
  openNote(noteId);
  listeners.forEach((notify) => notify());
}

function onShown(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

export function useShownNote(): Shown | undefined {
  return useSyncExternalStore(onShown, () => shown);
}
