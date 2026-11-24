import { useEffect, useSyncExternalStore } from "react";
import {
  anchorsOf,
  readNote,
  Refused,
  rewriteNote,
  UnreachedService,
  writeNote,
  type Anchors,
  type LinkedPassage,
  type Note,
  type NoteDraft,
  type Passage,
} from "./client";

// What is written in each note this visit, and its keeping: a note is read, what is typed in
// it is handed in a draft at a time, and each draft is sent to the service, one request at a
// time and the latest text winning. Each draft is numbered with an edition when it is made,
// one after the note's own, so that the service refuses a draft that was overtaken by one kept
// elsewhere, and a draft sent late never takes the place of a later one.

// Where one of a note's passages lies in the document its item holds now: at these anchors,
// with the very words that were linked or words that differ a little; or nowhere, so that it
// is shown in the revision it was linked in, at the anchors it was linked at.
export type Whereabouts =
  | { kind: "found"; anchors: Anchors; exact: boolean }
  | { kind: "lost"; anchors: Anchors; revision: number };

// One of a note's passages, with its item and what the item is called.
export type Linked = { passage: Passage; itemId: string; title: string; whereabouts: Whereabouts };

// What is written in a note, before it is numbered as a draft.
export type Text = Omit<NoteDraft, "edition">;

// Where the text of a note stands with the service. A text that was overtaken by one kept
// elsewhere, and a note removed elsewhere, are not sent again until the reader says what to do.
export type Keeping =
  | { state: "kept" }
  | { state: "keeping" }
  | { state: "unkept"; reason: string }
  | { state: "overtaken" }
  | { state: "removed" };

type Waiter = { draft: number; resolve: () => void; reject: (error: Error) => void };

// A note read: the note as the service last gave it, every passage it has linked to since,
// the number of the last draft made, the newest text handed in and not known to be kept, what
// the service is known to keep, the drafts handed in, the requests under way, and who waits
// for a draft to be kept. Each reading of the note is told from the one before, so that an
// editor of it is made anew.
export type Ready = {
  state: "ready";
  reading: number;
  note: Note;
  linked: ReadonlyMap<string, Linked>;
  edition: number;
  latest: NoteDraft | undefined;
  kept: Text;
  keeping: Keeping;
  handedIn: number;
  underway: number;
  waiters: Waiter[];
};

export type Session =
  | { state: "loading"; reading: number }
  | { state: "failed"; reason: string; gone: boolean }
  | Ready;

// The editor of the open note: what is written in it now, and the excerpt put at its end once
// the service has kept it. It hands in what was typed since it last did.
export type Writer = {
  text: () => Text;
  append: (markdown: string) => void;
  flush: (leaving: boolean) => void;
};

const NOT_FOUND = 404;
const STALE = 412;
const KEPT: Keeping = { state: "kept" };
const PASSAGE_ID_CHARACTERS = "0123456789abcdefghijklmnopqrstuvwxyz";
const PASSAGE_ID_LENGTH = 8;
// The browser sends a request that outlives the page only up to this size.
const LEAVING_BYTES = 64 * 1024;
export const GONE = "这篇笔记已经不在了。";
export const UNREACHED = "连不上服务";
export const OVERTAKEN = "这篇笔记在别处改过，这里写的没有保存。";
export const REMOVED = "这篇笔记在别处删除了，这里写的没有保存。";
// What the reader is told of the refusals a reader can act on, by the service's status.
const REFUSALS: Record<number, string | undefined> = {
  404: "引文所在的原文已经不在库里了",
  409: "有一段引文与原文对不上",
  422: "标题或正文超出了服务能保存的长度",
};

// A draft overtaken by one kept elsewhere, or of a note removed elsewhere; the session says
// which, and what was written is held there.
export class NotSent extends Error {}

const sessions = new Map<string, Session>();
const listeners = new Set<() => void>();
const keptListeners = new Set<(note: Note) => void>();
const readings = new Map<string, Promise<Ready | undefined>>();
let writer: { noteId: string; writer: Writer } | undefined;
let watching = false;
let readCount = 0;

// Why a request about a note failed, as a reader is told it: that the service was not reached,
// or what it refused. Any other failure is a fault of the app, told as it is.
export function failureText(error: unknown): string {
  if (error instanceof UnreachedService) {
    return UNREACHED;
  }
  if (error instanceof Refused) {
    return REFUSALS[error.status] ?? `服务没有收下（${error.status}）`;
  }
  return error instanceof Error ? error.message : String(error);
}

function tell(): void {
  listeners.forEach((notify) => notify());
}

export function onSessions(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

// Called with a note's view each time the service keeps it, as the list shows it.
export function onKept(notify: (note: Note) => void): void {
  keptListeners.add(notify);
}

export function sessionOf(noteId: string): Session | undefined {
  return sessions.get(noteId);
}

function readyOf(noteId: string): Ready | undefined {
  const session = sessions.get(noteId);
  return session?.state === "ready" ? session : undefined;
}

function update(noteId: string, change: (ready: Ready) => Ready): void {
  const ready = readyOf(noteId);
  if (ready !== undefined) {
    sessions.set(noteId, change(ready));
    tell();
  }
}

function whereaboutsOf(linked: LinkedPassage): Whereabouts {
  return linked.standing.kind === "found"
    ? { kind: "found", anchors: anchorsOf(linked.standing.passage), exact: linked.standing.exact }
    : { kind: "lost", anchors: anchorsOf(linked.passage), revision: linked.revision };
}

// The passages of a note's view, each kept as it was where nothing of it changed, so that a
// chip of a passage on show stays as it is after the note is kept.
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

function textOfNote(note: Note): Text {
  return {
    title: note.title,
    markdown: note.markdown,
    passages: note.passages.map(({ id, passage }) => ({ id, passage })),
  };
}

function sameText(one: Text, other: Text): boolean {
  return (
    one.title === other.title &&
    one.markdown === other.markdown &&
    JSON.stringify(one.passages) === JSON.stringify(other.passages)
  );
}

// The text of a note as it stands this visit: what was last handed in, or what is kept.
export function textOf(ready: Ready): Text {
  return ready.latest ?? ready.kept;
}

// Whether the note holds text this visit that the service is not known to keep.
function unkept(ready: Ready): boolean {
  return ready.underway > 0 || (ready.latest !== undefined && !sameText(ready.latest, ready.kept));
}

function readyFrom(note: Note, reading: number): Ready {
  return {
    state: "ready",
    reading,
    note,
    linked: linkedOf(note, new Map()),
    edition: note.edition,
    latest: undefined,
    kept: textOfNote(note),
    keeping: KEPT,
    handedIn: 0,
    underway: 0,
    waiters: [],
  };
}

// A note made just now, which this visit holds as read.
export function adopt(note: Note): void {
  readCount += 1;
  sessions.set(note.id, readyFrom(note, readCount));
  tell();
}

export function forget(noteId: string): void {
  sessions.delete(noteId);
  tell();
}

function startReading(noteId: string): Promise<Ready | undefined> {
  readCount += 1;
  const reading = readCount;
  sessions.set(noteId, { state: "loading", reading });
  tell();
  return readNote(noteId).then(
    (note) => {
      const ready = readyFrom(note, reading);
      sessions.set(noteId, ready);
      tell();
      return ready;
    },
    (error: unknown) => {
      const gone = error instanceof Refused && error.status === NOT_FOUND;
      sessions.set(noteId, { state: "failed", reason: gone ? GONE : failureText(error), gone });
      tell();
      if (gone) {
        return undefined;
      }
      throw error;
    },
  );
}

// The note as the service holds it now: read again unless this visit holds text of it that is
// not kept yet, which stays as it is. Undefined when the note is not there.
export function freshen(noteId: string): Promise<Ready | undefined> {
  const ready = readyOf(noteId);
  if (ready !== undefined && unkept(ready)) {
    return Promise.resolve(ready);
  }
  const underway = readings.get(noteId);
  if (underway !== undefined) {
    return underway;
  }
  const read = startReading(noteId).finally(() => readings.delete(noteId));
  readings.set(noteId, read);
  return read;
}

export function useSession(noteId: string): Session | undefined {
  return useSyncExternalStore(onSessions, () => sessions.get(noteId));
}

function settle(noteId: string, draft: number, failure: Error | undefined): void {
  const ready = readyOf(noteId);
  if (ready === undefined) {
    return;
  }
  const done = ready.waiters.filter((waiter) => waiter.draft <= draft);
  update(noteId, (held) => ({
    ...held,
    waiters: held.waiters.filter((waiter) => waiter.draft > draft),
  }));
  done.forEach((waiter) => (failure === undefined ? waiter.resolve() : waiter.reject(failure)));
}

// Whether a request with the draft can outlive the page it is sent from.
function outlives(draft: NoteDraft): boolean {
  return new Blob([JSON.stringify(draft)]).size <= LEAVING_BYTES;
}

// What came of a request belongs to the reading of the note it was sent from; one that comes
// after the note was read again, its text given up, is dropped.
function ofReading(noteId: string, reading: number): boolean {
  return readyOf(noteId)?.reading === reading;
}

function sent(noteId: string, draft: NoteDraft, number: number, note: Note): void {
  update(noteId, (held) => ({
    ...held,
    underway: held.underway - 1,
    note,
    kept: draft,
    linked: linkedOf(note, held.linked),
    keeping: held.latest === draft ? KEPT : held.keeping,
  }));
  keptListeners.forEach((notify) => notify(note));
  settle(noteId, number, undefined);
  keep(noteId, false);
}

// The service answers not found both for a note that is not there and for a passage whose
// document is not there, so a note is asked for to tell the two apart.
async function refusal(noteId: string, error: unknown): Promise<"overtaken" | "removed" | "other"> {
  if (!(error instanceof Refused)) {
    return "other";
  }
  if (error.status === STALE) {
    return "overtaken";
  }
  if (error.status !== NOT_FOUND) {
    return "other";
  }
  try {
    await readNote(noteId);
    return "other";
  } catch (reading) {
    return reading instanceof Refused && reading.status === NOT_FOUND ? "removed" : "other";
  }
}

async function refused(noteId: string, draft: NoteDraft, error: unknown): Promise<void> {
  const kind = await refusal(noteId, error);
  const overtaken = kind === "overtaken";
  const removed = kind === "removed";
  const reason = failureText(error);
  update(noteId, (held) => ({
    ...held,
    underway: held.underway - 1,
    keeping: overtaken
      ? { state: "overtaken" }
      : removed
        ? { state: "removed" }
        : { state: "unkept", reason },
  }));
  const failure = overtaken
    ? new NotSent(OVERTAKEN)
    : removed
      ? new NotSent(REMOVED)
      : new Error(reason);
  // Nothing handed in is sent again by itself, so whatever waited for a later text is not kept.
  settle(noteId, Infinity, failure);
  // A refused text that was overtaken by a newer one of this visit does not hold the newer back.
  const ready = readyOf(noteId);
  if (!overtaken && !removed && ready?.latest !== undefined && ready.latest !== draft) {
    update(noteId, (held) => ({ ...held, keeping: { state: "keeping" } }));
    keep(noteId, false);
  }
}

// Sends the latest text of a note unless the service keeps it already. One request is under
// way at a time, and the text handed in while it is goes next. As the page is left, the latest
// text is sent at once under the next edition, whatever is under way, so that it wins over the
// request on its way and is not lost with the page.
function keep(noteId: string, leaving: boolean): void {
  const ready = readyOf(noteId);
  const latest = ready?.latest;
  if (ready === undefined || latest === undefined) {
    return;
  }
  if (ready.keeping.state === "overtaken" || ready.keeping.state === "removed") {
    return;
  }
  if (sameText(latest, ready.kept) && ready.underway === 0) {
    update(noteId, (held) => ({ ...held, keeping: KEPT }));
    settle(noteId, ready.handedIn, undefined);
    return;
  }
  if (ready.underway > 0 && !leaving) {
    return;
  }
  const draft =
    leaving && ready.underway > 0 ? { ...latest, edition: ready.edition + 1 } : latest;
  const number = ready.handedIn;
  const { reading } = ready;
  update(noteId, (held) => ({
    ...held,
    edition: Math.max(held.edition, draft.edition),
    latest: draft,
    underway: held.underway + 1,
    keeping: { state: "keeping" },
  }));
  // A draft too large to outlive the page goes as any other, which the browser asks about.
  rewriteNote(noteId, draft, leaving && outlives(draft)).then(
    (note) => ofReading(noteId, reading) && sent(noteId, draft, number, note),
    (error: unknown) => ofReading(noteId, reading) && void refused(noteId, draft, error),
  );
}

// Takes a note's text to be kept, numbered as the note's next edition; the number it answers
// tells this text from those handed in before it.
export function handIn(noteId: string, text: Text, leaving: boolean): number {
  const ready = readyOf(noteId);
  if (ready === undefined) {
    return 0;
  }
  watchLeaving();
  const number = ready.handedIn + 1;
  update(noteId, (held) => ({
    ...held,
    handedIn: number,
    edition: held.edition + 1,
    latest: { ...text, edition: held.edition + 1 },
  }));
  keep(noteId, leaving);
  return number;
}

export function keepAgain(noteId: string): void {
  update(noteId, (held) => ({ ...held, keeping: { state: "keeping" } }));
  keep(noteId, false);
}

// Waits until the text handed in under this number is kept, or fails with why it is not.
export function whenKept(noteId: string, draft: number): Promise<void> {
  const ready = readyOf(noteId);
  if (ready === undefined) {
    return Promise.reject(new Error(GONE));
  }
  if (ready.keeping.state === "kept" && ready.underway === 0) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    update(noteId, (held) => ({
      ...held,
      waiters: [...held.waiters, { draft, resolve, reject }],
    }));
  });
}

// Gives up a text handed in that the service refused, and keeps instead what the note held
// before it, if this visit has anything of it unkept.
export function withdraw(noteId: string, before: Text): void {
  const ready = readyOf(noteId);
  if (ready === undefined) {
    return;
  }
  if (sameText(before, ready.kept)) {
    update(noteId, (held) => ({ ...held, latest: undefined, keeping: KEPT }));
    return;
  }
  handIn(noteId, before, false);
}

// The text of an overtaken note takes the place of what the service holds now: the note's
// edition is read, and the text is sent as the next one.
export async function putInPlace(noteId: string): Promise<void> {
  const ready = readyOf(noteId);
  if (ready?.latest === undefined) {
    return;
  }
  const note = await readNote(noteId);
  update(noteId, (held) => ({ ...held, edition: note.edition, keeping: { state: "keeping" } }));
  handIn(noteId, textOf(ready), false);
}

// The text of an overtaken or removed note is kept as a new note, which is answered; the note
// it was written in is read as the service holds it.
export async function keepAsNew(noteId: string): Promise<Note> {
  const ready = readyOf(noteId);
  if (ready === undefined) {
    throw new Error(GONE);
  }
  const text = textOf(ready);
  const note = await writeNote({ ...text, title: `${text.title || "无题"}（另存）`, edition: 1 });
  adopt(note);
  keptListeners.forEach((notify) => notify(note));
  letGo(noteId);
  return note;
}

// What this visit holds of a note is given up, and the note is read as the service holds it.
export function letGo(noteId: string): void {
  const ready = readyOf(noteId);
  if (ready === undefined) {
    return;
  }
  settle(noteId, Infinity, new NotSent(OVERTAKEN));
  sessions.set(noteId, { ...ready, latest: undefined, underway: 0, keeping: KEPT });
  freshen(noteId).catch(() => undefined);
}

// Passages a note links to from now on, given before the text that links to them.
export function addLinked(noteId: string, added: ReadonlyMap<string, Linked>): void {
  update(noteId, (held) => ({ ...held, linked: new Map([...held.linked, ...added]) }));
}

// An id for a passage of a note, which the test says is not taken.
export function newPassageId(taken: (passageId: string) => boolean): string {
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

// The passages a draft of a note carries: those of these ids that the note holds.
export function passagesOf(noteId: string, passageIds: string[]): Text["passages"] {
  const linked = readyOf(noteId)?.linked ?? new Map<string, Linked>();
  return passageIds.flatMap((id) => {
    const held = linked.get(id);
    return held === undefined ? [] : [{ id, passage: held.passage }];
  });
}

export function linkedPassage(noteId: string, passageId: string): Linked | undefined {
  return readyOf(noteId)?.linked.get(passageId);
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

export function writerOf(noteId: string): Writer | undefined {
  return writer?.noteId === noteId ? writer.writer : undefined;
}

// What was typed in the open note is handed in now.
export function flushWriter(): void {
  writer?.writer.flush(false);
}

function unkeptDrafts(): NoteDraft[] {
  return [...sessions.values()].flatMap((session) =>
    session.state === "ready" && session.latest !== undefined && unkept(session)
      ? [session.latest]
      : [],
  );
}

// As the page is hidden or left, what was typed is handed in at once, and every text not kept
// is sent.
function leaving(): void {
  writer?.writer.flush(true);
  for (const [noteId, session] of sessions) {
    if (
      session.state === "ready" &&
      session.latest !== undefined &&
      !sameText(session.latest, session.kept) &&
      session.keeping.state !== "overtaken" &&
      session.keeping.state !== "removed"
    ) {
      keep(noteId, true);
    }
  }
}

// Where what is not kept cannot outlive the page, being too large, refused or the service
// away, the browser asks before the page goes.
function warnBeforeLeaving(event: BeforeUnloadEvent): void {
  leaving();
  const drafts = unkeptDrafts();
  const stuck = [...sessions.values()].some(
    (session) => session.state === "ready" && session.keeping.state !== "kept" && session.keeping.state !== "keeping",
  );
  if (drafts.length > 0 && (stuck || drafts.some((draft) => !outlives(draft)))) {
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

// Reads the note when it is opened unless this visit holds text of it not kept yet.
export function useOpening(noteId: string): void {
  useEffect(() => {
    freshen(noteId).catch(() => undefined);
  }, [noteId]);
}
