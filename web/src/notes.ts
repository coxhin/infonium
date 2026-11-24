import { useEffect, useSyncExternalStore } from "react";
import {
  anchorsOf,
  listNotes,
  readEntry,
  readNote,
  Refused,
  removeNote,
  rewriteNote,
  writeNote,
  type Anchors,
  type LinkedPassage,
  type Note,
  type NoteDraft,
  type NoteEntry,
  type Passage,
} from "./client";
import { itemTitle } from "./entry";
import type { Loaded } from "./loading";
import { appended, firstWords, quoteMarkdown, statementMarkdown } from "./notemarkdown";

// What is put into a note from elsewhere in the app, the one way to put it there, and the one
// way to ask for a note to be shown. The notebook's notes are built on this: the notes of the
// library, the one that is open, and what is written in each note this visit, which is kept
// here so that it outlasts the reader of any one item and the editor it was typed in.

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

// Where one of a note's passages lies in the document its item holds now: at these anchors,
// with the very words that were linked or words that differ a little; or nowhere, so that it
// is shown in the revision it was linked in, at the anchors it was linked at.
export type Whereabouts =
  | { kind: "found"; anchors: Anchors; exact: boolean }
  | { kind: "lost"; anchors: Anchors; revision: number };

// One of a note's passages, with its item and what the item is called.
export type Linked = { passage: Passage; itemId: string; title: string; whereabouts: Whereabouts };

export type Keeping = { state: "kept" } | { state: "keeping" } | { state: "unkept"; reason: string };

// All that is held of one note this visit: the note as it was first read, every passage it
// has linked to since, the text last handed in to be kept, and what the service is known to
// keep.
export type Session = {
  note: Loaded<Note>;
  linked: ReadonlyMap<string, Linked>;
  latest: NoteDraft | undefined;
  kept: NoteDraft | undefined;
  keeping: Keeping;
};

// The editor of the open note: it takes an excerpt into its own text and hands the whole text
// in to be kept, and it hands in what was typed since it last did.
export type Writer = {
  append: (markdown: string) => number;
  flush: (leaving: boolean) => void;
};

const NOT_FOUND = 404;
const KEPT: Keeping = { state: "kept" };
const LOADING: Loaded<never> = { state: "loading" };
const PASSAGE_ID_CHARACTERS = "0123456789abcdefghijklmnopqrstuvwxyz";
const PASSAGE_ID_LENGTH = 8;
// The browser sends a request that outlives the page only up to this size.
const LEAVING_BYTES = 64 * 1024;
const UNTITLED_SOURCE = "摘录";
const UNREACHED = "连不上服务，写下的内容都还在这里";
const NO_SERVICE = "连不上服务";
export const GONE = "这篇笔记已经不在了。";

const listeners = new Set<() => void>();
let shown: Shown | undefined;
let opened: string | undefined;
let notes: Loaded<NoteEntry[]> = LOADING;
let listReadings = 0;
const sessions = new Map<string, Session>();
const readings = new Map<string, Promise<Session>>();
// How many drafts of each note were handed in, how many requests to keep it are under way, and
// who waits for a draft to be kept.
const handedIn = new Map<string, number>();
const underway = new Map<string, number>();
type Waiter = { draft: number; resolve: () => void; reject: (error: Unkept) => void };
const waiters = new Map<string, Waiter[]>();
// The first words of notes without a title, read for the list, by the note and when it was
// changed.
const firstWordsRead = new Map<string, { changed: string; words: Loaded<string> }>();
let writer: { noteId: string; writer: Writer } | undefined;
let lastNoted: string | undefined;
let excerpts: Promise<unknown> = Promise.resolve();
let watching = false;
let left: string | undefined;
let justMade: string | undefined;

// Why a request about notes failed: the service's reason when it refused, and otherwise that it
// could not be reached. The browser's reason for a request that reached nothing is a word of its
// own, such as "Failed to fetch", which tells a reader nothing more.
// A note's text was handed in and the service did not keep it; the message says why.
class Unkept extends Error {}

export function failureText(error: unknown, unreached = NO_SERVICE): string {
  return error instanceof Refused ? error.message : unreached;
}

function tell(): void {
  listeners.forEach((notify) => notify());
}

function onChange(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

function update(noteId: string, change: (session: Session) => Session): void {
  const session = sessions.get(noteId);
  if (session !== undefined) {
    sessions.set(noteId, change(session));
    tell();
  }
}

function whereaboutsOf(linked: LinkedPassage): Whereabouts {
  return linked.standing.kind === "found"
    ? { kind: "found", anchors: anchorsOf(linked.standing.passage), exact: linked.standing.exact }
    : { kind: "lost", anchors: anchorsOf(linked.passage), revision: linked.revision };
}

// The passages of a note's view, each kept as it was where nothing of it changed, so that a
// chip of a passage on show stays pressed after the note is kept.
function linkedOf(note: Note, held: ReadonlyMap<string, Linked>): Map<string, Linked> {
  const linked = new Map(held);
  for (const passage of note.passages) {
    const fresh: Linked = {
      passage: passage.passage,
      itemId: passage.item,
      title: passage.title,
      whereabouts: whereaboutsOf(passage),
    };
    const before = held.get(passage.id);
    linked.set(
      passage.id,
      before !== undefined && JSON.stringify(before) === JSON.stringify(fresh) ? before : fresh,
    );
  }
  return linked;
}

function draftOf(note: Note): NoteDraft {
  return {
    title: note.title,
    markdown: note.markdown,
    passages: note.passages.map(({ id, passage }) => ({ id, passage })),
  };
}

function sameDraft(one: NoteDraft, other: NoteDraft | undefined): boolean {
  return (
    other !== undefined &&
    one.title === other.title &&
    one.markdown === other.markdown &&
    JSON.stringify(one.passages) === JSON.stringify(other.passages)
  );
}

// The text of a note as it stands this visit: what was last handed in, or what is kept.
export function textOf(session: Session): NoteDraft | undefined {
  return session.latest ?? session.kept;
}

// What a note is called where it is listed or named: its title, or its first words.
export function noteName(title: string, markdown: string): string {
  return title.trim() || firstWords(markdown) || "空白笔记";
}

function sessionFrom(note: Note): Session {
  return {
    note: { state: "ready", value: note },
    linked: linkedOf(note, new Map()),
    latest: undefined,
    kept: draftOf(note),
    keeping: KEPT,
  };
}

// Reads a note, unless it is read already or being read; a reading that failed is tried again.
function begin(noteId: string): Promise<Session> {
  const held = sessions.get(noteId);
  if (held !== undefined && held.note.state === "ready") {
    return Promise.resolve(held);
  }
  const reading = readings.get(noteId);
  if (reading !== undefined) {
    return reading;
  }
  const read = beginReading(noteId).finally(() => readings.delete(noteId));
  readings.set(noteId, read);
  return read;
}

function beginReading(noteId: string): Promise<Session> {
  sessions.set(noteId, {
    note: LOADING,
    linked: new Map(),
    latest: undefined,
    kept: undefined,
    keeping: KEPT,
  });
  tell();
  return readNote(noteId).then(
    (note) => {
      const session = sessionFrom(note);
      sessions.set(noteId, session);
      tell();
      return session;
    },
    (error: unknown) => {
      const reason =
        error instanceof Refused && error.status === NOT_FOUND ? GONE : failureText(error);
      sessions.set(noteId, {
        note: { state: "failed", reason },
        linked: new Map(),
        latest: undefined,
        kept: undefined,
        keeping: KEPT,
      });
      tell();
      throw error;
    },
  );
}

export function useSession(noteId: string): Session | undefined {
  const session = useSyncExternalStore(onChange, () => sessions.get(noteId));
  useEffect(() => {
    if (sessions.get(noteId)?.note.state !== "ready") {
      begin(noteId).catch(() => undefined);
    }
  }, [noteId]);
  return session;
}

export function readAgain(noteId: string): void {
  begin(noteId).catch(() => undefined);
}

function settle(noteId: string, draft: number, failure: Unkept | undefined): void {
  const waiting = waiters.get(noteId) ?? [];
  const settled = waiting.filter((waiter) => waiter.draft <= draft);
  waiters.set(
    noteId,
    waiting.filter((waiter) => waiter.draft > draft),
  );
  settled.forEach((waiter) =>
    failure === undefined ? waiter.resolve() : waiter.reject(failure),
  );
}

// The note as a list shows it, after it was kept.
function listed(note: Note): void {
  if (notes.state !== "ready") {
    return;
  }
  const entry: NoteEntry = {
    id: note.id,
    title: note.title,
    made: note.made,
    changed: note.changed,
    items: [...new Set(note.passages.map((passage) => passage.item))],
  };
  const others = notes.value.filter((held) => held.id !== note.id);
  notes = {
    state: "ready",
    value: [entry, ...others].sort((one, other) => other.changed.localeCompare(one.changed)),
  };
}

// Sends the latest text of a note unless the service keeps it already. One request is under
// way at a time, and the text handed in while it is goes next. As the page is left the latest
// text is sent at once, whatever is under way, so that it is not lost with the page.
function keep(noteId: string, leaving: boolean): void {
  const session = sessions.get(noteId);
  const draft = session?.latest;
  if (draft === undefined) {
    return;
  }
  const number = handedIn.get(noteId) ?? 0;
  if (sameDraft(draft, session?.kept)) {
    update(noteId, (held) => ({ ...held, keeping: KEPT }));
    settle(noteId, number, undefined);
    return;
  }
  const running = underway.get(noteId) ?? 0;
  if (running > 0 && !leaving) {
    return;
  }
  underway.set(noteId, running + 1);
  update(noteId, (held) => ({ ...held, keeping: { state: "keeping" } }));
  // A draft too large to outlive the page goes as any other, which the browser asks about.
  rewriteNote(noteId, draft, leaving && outlives(draft)).then(
    (note) => {
      underway.set(noteId, (underway.get(noteId) ?? 1) - 1);
      update(noteId, (held) => ({
        ...held,
        kept: draft,
        linked: linkedOf(note, held.linked),
        keeping: held.latest === draft ? KEPT : held.keeping,
      }));
      listed(note);
      settle(noteId, number, undefined);
      keep(noteId, false);
    },
    (error: unknown) => {
      underway.set(noteId, (underway.get(noteId) ?? 1) - 1);
      const reason = failureText(error, UNREACHED);
      update(noteId, (held) => ({ ...held, keeping: { state: "unkept", reason } }));
      // Nothing handed in is sent again until the reader asks or writes on, so whatever
      // waited for a later text is not kept either.
      settle(noteId, Infinity, new Unkept(reason));
    },
  );
}

// Takes a note's text to be kept; the number tells this text from those handed in before it.
export function handIn(noteId: string, draft: NoteDraft, leaving: boolean): number {
  const number = (handedIn.get(noteId) ?? 0) + 1;
  handedIn.set(noteId, number);
  watchLeaving();
  update(noteId, (session) => ({ ...session, latest: draft }));
  keep(noteId, leaving);
  return number;
}

export function keepAgain(noteId: string): void {
  keep(noteId, false);
}

function whenKept(noteId: string, draft: number): Promise<void> {
  if (sessions.get(noteId)?.keeping.state === "kept" && (underway.get(noteId) ?? 0) === 0) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    waiters.set(noteId, [...(waiters.get(noteId) ?? []), { draft, resolve, reject }]);
  });
}

// Passages a note links to from now on, given before the text that links to them.
export function addLinked(noteId: string, added: ReadonlyMap<string, Linked>): void {
  update(noteId, (session) => ({ ...session, linked: new Map([...session.linked, ...added]) }));
}

// The passages a draft of a note carries: those of these ids that the note holds.
export function passagesOf(noteId: string, passageIds: string[]): NoteDraft["passages"] {
  const linked = sessions.get(noteId)?.linked ?? new Map<string, Linked>();
  return passageIds.flatMap((id) => {
    const held = linked.get(id);
    return held === undefined ? [] : [{ id, passage: held.passage }];
  });
}

export function linkedPassage(noteId: string, passageId: string): Linked | undefined {
  return sessions.get(noteId)?.linked.get(passageId);
}

// Whether a request with the draft can outlive the page it is sent from.
function outlives(draft: NoteDraft): boolean {
  return new Blob([JSON.stringify(draft)]).size <= LEAVING_BYTES;
}

function unkeptText(): NoteDraft[] {
  return [...sessions.values()].flatMap((session) =>
    session.latest !== undefined && !sameDraft(session.latest, session.kept)
      ? [session.latest]
      : [],
  );
}

// As the page is hidden or left, what was typed is handed in at once. Where it cannot outlive
// the page, being too large or the service being away, the browser asks before the page goes.
function leaving(): void {
  writer?.writer.flush(true);
  for (const [noteId, session] of sessions) {
    if (session.latest !== undefined && !sameDraft(session.latest, session.kept)) {
      keep(noteId, true);
    }
  }
}

function warnBeforeLeaving(event: BeforeUnloadEvent): void {
  leaving();
  const unkept = unkeptText();
  const failed = [...sessions.values()].some((session) => session.keeping.state === "unkept");
  const large = unkept.some((draft) => !outlives(draft));
  if (unkept.length > 0 && (failed || large)) {
    event.preventDefault();
  }
}

function onHidden(): void {
  if (document.visibilityState === "hidden") {
    leaving();
  }
}

// The page is watched for being left from the first text written, for the rest of the visit.
function watchLeaving(): void {
  if (!watching) {
    watching = true;
    addEventListener("beforeunload", warnBeforeLeaving);
    addEventListener("pagehide", leaving);
    document.addEventListener("visibilitychange", onHidden);
  }
}

export function attachWriter(noteId: string, attached: Writer): () => void {
  const held = { noteId, writer: attached };
  watchLeaving();
  writer = held;
  return () => {
    if (writer === held) {
      writer = undefined;
    }
  };
}

export function openNote(noteId: string | undefined): void {
  writer?.writer.flush(false);
  left = opened ?? left;
  opened = noteId;
  tell();
}

// The note the reader last went back to the list from, which the list brings into focus.
export function noteLeft(): string | undefined {
  return left;
}

// Whether the note was made just now by the reader, to be written in: asked once.
export function takeJustMade(noteId: string): boolean {
  const made = justMade === noteId;
  justMade = undefined;
  return made;
}

export function useOpenedNote(): string | undefined {
  return useSyncExternalStore(onChange, () => opened);
}

export function rereadNotes(): void {
  listReadings += 1;
  const reading = listReadings;
  if (notes.state === "failed") {
    notes = LOADING;
    tell();
  }
  listNotes().then(
    (value) => {
      if (reading === listReadings) {
        notes = { state: "ready", value };
        tell();
      }
    },
    (error: unknown) => {
      // A list on show stays when it cannot be read again.
      if (reading === listReadings && notes.state !== "ready") {
        notes = { state: "failed", reason: failureText(error) };
        tell();
      }
    },
  );
}

// The notes of the library, read again whenever a list of them comes on show.
export function useNotes(): Loaded<NoteEntry[]> {
  useEffect(rereadNotes, []);
  return useSyncExternalStore(onChange, () => notes);
}

// What a note without a title is called in the list: its first words, from what is written in
// it this visit or read for the purpose.
export function useFirstWords(entry: NoteEntry): Loaded<string> {
  const words = useSyncExternalStore(onChange, () => {
    const session = sessions.get(entry.id);
    const text = session === undefined ? undefined : textOf(session);
    if (text !== undefined) {
      return text.markdown;
    }
    return firstWordsRead.get(entry.id);
  });
  useEffect(() => {
    const read = firstWordsRead.get(entry.id);
    const session = sessions.get(entry.id);
    if (
      entry.title !== "" ||
      (session !== undefined && textOf(session) !== undefined) ||
      read?.changed === entry.changed
    ) {
      return;
    }
    firstWordsRead.set(entry.id, { changed: entry.changed, words: LOADING });
    tell();
    readNote(entry.id).then(
      (note) => {
        firstWordsRead.set(entry.id, {
          changed: entry.changed,
          words: { state: "ready", value: firstWords(note.markdown) },
        });
        tell();
      },
      (error: unknown) => {
        firstWordsRead.set(entry.id, {
          changed: entry.changed,
          words: { state: "failed", reason: failureText(error) },
        });
        tell();
      },
    );
  }, [entry.id, entry.changed, entry.title]);
  if (typeof words === "string") {
    return { state: "ready", value: firstWords(words) };
  }
  return words?.words ?? LOADING;
}

// Makes an empty note and opens it.
export async function makeNote(): Promise<void> {
  const note = await writeNote({ title: "", markdown: "", passages: [] });
  sessions.set(note.id, sessionFrom(note));
  listed(note);
  justMade = note.id;
  openNote(note.id);
}

export async function dropNote(noteId: string): Promise<void> {
  await removeNote(noteId);
  sessions.delete(noteId);
  if (notes.state === "ready") {
    notes = { state: "ready", value: notes.value.filter((entry) => entry.id !== noteId) };
  }
  if (opened === noteId) {
    opened = undefined;
  }
  if (lastNoted === noteId) {
    lastNoted = undefined;
  }
  tell();
}

function newPassageId(taken: (passageId: string) => boolean): string {
  for (;;) {
    const random = crypto.getRandomValues(new Uint8Array(PASSAGE_ID_LENGTH));
    const passageId = [...random]
      .map((value) => PASSAGE_ID_CHARACTERS[value % PASSAGE_ID_CHARACTERS.length])
      .join("");
    if (!taken(passageId)) {
      return passageId;
    }
  }
}

// What the sources of an excerpt are called, by item.
async function sourceTitles(cited: Cited[]): Promise<Map<string, string>> {
  const itemIds = [...new Set(cited.map((one) => one.itemId))];
  const entries = await Promise.all(itemIds.map((itemId) => readEntry(itemId)));
  return new Map(entries.map((entry) => [entry.id, itemTitle(entry)]));
}

// An excerpt as Markdown, with the passages it links to under ids that the note does not use.
function written(
  excerpt: Excerpt,
  titles: ReadonlyMap<string, string>,
  taken: (passageId: string) => boolean,
): { markdown: string; linked: Map<string, Linked> } {
  const cited = excerpt.kind === "quote" ? [excerpt.cited] : excerpt.cited;
  const linked = new Map<string, Linked>();
  for (const one of cited) {
    const passageId = newPassageId((candidate) => taken(candidate) || linked.has(candidate));
    linked.set(passageId, {
      passage: { anchors: one.anchors },
      itemId: one.itemId,
      title: titles.get(one.itemId) ?? "",
      whereabouts: { kind: "found", anchors: one.anchors, exact: true },
    });
  }
  const [passageId = ""] = linked.keys();
  const markdown =
    excerpt.kind === "quote"
      ? quoteMarkdown(
          excerpt.cited.anchors.map((anchor) => anchor.quote),
          titles.get(excerpt.cited.itemId) ?? "",
          passageId,
        )
      : statementMarkdown(excerpt.text, [...linked.keys()]);
  return { markdown, linked };
}

function draftPassages(linked: ReadonlyMap<string, Linked>): NoteDraft["passages"] {
  return [...linked].map(([id, { passage }]) => ({ id, passage }));
}

async function intoNewNote(excerpt: Excerpt, titles: ReadonlyMap<string, string>): Promise<Noted> {
  const { markdown, linked } = written(excerpt, titles, () => false);
  const [first] = linked.values();
  const title = first?.title || UNTITLED_SOURCE;
  const note = await writeNote({ title, markdown, passages: draftPassages(linked) });
  sessions.set(note.id, sessionFrom(note));
  listed(note);
  tell();
  return { noteId: note.id, title };
}

// Puts an excerpt at the end of a note this visit holds or can read: into the editor's own
// text when the note is open in it, so that nothing being typed is lost, and into the text
// this visit holds of it otherwise. Undefined when the note is not there.
async function intoNote(
  noteId: string,
  excerpt: Excerpt,
  titles: ReadonlyMap<string, string>,
): Promise<Noted | undefined> {
  let session: Session;
  try {
    session = await begin(noteId);
  } catch (error) {
    if (error instanceof Refused && error.status === NOT_FOUND) {
      return undefined;
    }
    throw error;
  }
  const text = textOf(session);
  if (text === undefined) {
    return undefined;
  }
  const { markdown, linked } = written(
    excerpt,
    titles,
    (passageId) => session.linked.has(passageId),
  );
  addLinked(noteId, linked);
  const number =
    writer?.noteId === noteId
      ? writer.writer.append(markdown)
      : handIn(
          noteId,
          {
            title: text.title,
            markdown: appended(text.markdown, markdown),
            passages: [...text.passages, ...draftPassages(linked)],
          },
          false,
        );
  await whenKept(noteId, number);
  const kept = sessions.get(noteId);
  const keptText = kept === undefined ? text : (textOf(kept) ?? text);
  return { noteId, title: noteName(keptText.title, keptText.markdown) };
}

async function putDown(excerpt: Excerpt): Promise<Noted> {
  const cited = excerpt.kind === "quote" ? [excerpt.cited] : excerpt.cited;
  const titles = await sourceTitles(cited);
  const target = opened ?? lastNoted;
  const noted =
    (target === undefined ? undefined : await intoNote(target, excerpt, titles)) ??
    (await intoNewNote(excerpt, titles));
  lastNoted = noted.noteId;
  return noted;
}

// Puts an excerpt at the end of the note being written, with links to its passages, and makes
// a note for it when none is being written; the result says where it went, once it is kept.
// Excerpts are put down one after another, in the order they came.
export function noteDown(excerpt: Excerpt): Promise<Noted> {
  const noted = excerpts
    .then(() => putDown(excerpt))
    .catch((error: unknown) => {
      throw error instanceof Unkept ? error : new Error(failureText(error));
    });
  excerpts = noted.catch(() => undefined);
  return noted;
}

// Asks for a note to be shown: the notebook comes on show with the note open in it.
export function showNote(noteId: string): void {
  shown = { noteId, asking: (shown?.asking ?? 0) + 1 };
  openNote(noteId);
}

export function useShownNote(): Shown | undefined {
  return useSyncExternalStore(onChange, () => shown);
}
