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
// it is handed in a text at a time, and the text is sent to the service, one request at a time
// and the latest text winning. A draft is numbered when it is sent, one after the edition the
// service last showed this visit, so that the service refuses a draft that another writer
// overtook; nothing else moves the number, so that this visit never runs ahead of the service.

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
  // A text the service did not answer for is sent again by itself; one it refused is not.
  | { state: "unkept"; reason: string; away: boolean }
  | { state: "overtaken" }
  | { state: "removed" };

type Waiter = { draft: number; resolve: () => void; reject: (error: Error) => void };

// A request on its way: the edition it was sent at, the text it carries, the number of the text
// handed in last when it was sent, and whether it carries an excerpt that waits for its fate.
type OnItsWay = { edition: number; text: Text; number: number; excerpt: boolean };

// A note read: the note as the service last gave it, every passage it has linked to since and
// those the service refused, the edition the service last showed this visit, the newest text
// handed in and not known to be kept, what the service is known to keep, how many texts were
// handed in and which of them carries an excerpt, the requests on their way, who waits for a
// text to be kept, and a reading again under way. Each reading of the note is told from the one
// before, so that an editor of it is made anew.
export type Ready = {
  state: "ready";
  reading: number;
  note: Note;
  linked: ReadonlyMap<string, Linked>;
  refused: ReadonlySet<string>;
  shown: number;
  latest: Text | undefined;
  kept: Text;
  keeping: Keeping;
  handedIn: number;
  excerpt: number | undefined;
  onItsWay: OnItsWay[];
  waiters: Waiter[];
  rereading: Promise<Ready | undefined> | undefined;
  retry: Retry;
};

// How often a text the service did not answer for was sent again since one was last kept, and
// when it goes next.
type Retry = { attempt: number; timer: ReturnType<typeof setTimeout> | undefined };

export type Session =
  | { state: "loading"; reading: number; read: Promise<Ready | undefined> }
  | { state: "failed"; reason: string; gone: boolean }
  | Ready;

// The editor of the open note: what is written in it now, and the excerpt put at its end once
// the service has kept it. It hands in what was typed since it last did.
export type Writer = {
  text: () => Text;
  append: (markdown: string) => void;
  // Answers whether it still holds text not handed in, as a word an input method composes.
  flush: (leaving: boolean) => boolean;
};

const NOT_FOUND = 404;
const CONFLICT = 409;
const STALE = 412;
const KEPT: Keeping = { state: "kept" };
// The service keeps a title of up to this many characters (TITLE_CHARACTERS in note.py).
export const TITLE_CHARACTERS = 200;
// What the title of a text kept apart as a new note ends in, and is when it had none.
const KEPT_APART = "（另存）";
const UNTITLED = "无题";
const PASSAGE_ID_CHARACTERS = "0123456789abcdefghijklmnopqrstuvwxyz";
const PASSAGE_ID_LENGTH = 8;
// The browser sends a request that outlives the page only up to this size.
const LEAVING_BYTES = 64 * 1024;
const QUOTED_WORDS = 16;
// When a text the service did not answer for goes again: after the first wait, doubled each
// time, up to the last.
const RETRY_FIRST_MS = 2000;
const RETRY_LAST_MS = 30_000;
export const GONE = "这篇笔记已经不在了。";
export const UNREACHED = "连不上服务";
export const OVERTAKEN = "这篇笔记在别处改过，这里写的没有保存。";
export const REMOVED = "这篇笔记在别处删除了，这里写的没有保存。";
// What a gateway answers when the service behind it does not; the requests about notes call no
// model, so the service itself never answers them so (as requestReason in client.ts says).
const GATEWAY_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);
// What the reader is told of the refusals a reader can act on, by the service's status.
const REFUSALS: Record<number, string | undefined> = {
  404: "引文所在的原文已经不在库里了",
  409: "有一段引文与原文对不上",
  422: "标题或正文超出了服务能保存的长度",
};

// A text overtaken by one kept elsewhere, or of a note removed elsewhere; the session says
// which, and what was written is held there.
export class NotSent extends Error {}

const sessions = new Map<string, Session>();
const listeners = new Set<() => void>();
const keptListeners = new Set<(note: Note) => void>();
let writer: { noteId: string; writer: Writer } | undefined;
let watching = false;
let readCount = 0;
let stranded: ReadonlyArray<{ noteId: string; text: Text }> | undefined;

// Whether a request reached no service, or only a gateway the service behind did not answer.
function unreached(error: unknown): boolean {
  return (
    error instanceof UnreachedService ||
    (error instanceof Refused && GATEWAY_STATUSES.has(error.status))
  );
}

// Why a request about a note failed, as a reader is told it: that the service was not reached,
// or what it refused. Any other failure is a fault of the app, told as it is.
export function failureText(error: unknown): string {
  if (unreached(error)) {
    return UNREACHED;
  }
  if (error instanceof Refused) {
    return REFUSALS[error.status] ?? `服务没有收下（${error.status}）`;
  }
  return error instanceof Error ? error.message : String(error);
}

function tell(): void {
  stranded = undefined;
  listeners.forEach((notify) => notify());
}

// The notes removed elsewhere whose text this visit still holds unkept, which the list offers
// to open, since the service no longer lists them.
export function strandedNotes(): ReadonlyArray<{ noteId: string; text: Text }> {
  stranded ??= [...sessions].flatMap(([noteId, session]) =>
    session.state === "ready" && session.keeping.state === "removed"
      ? [{ noteId, text: textOf(session) }]
      : [],
  );
  return stranded;
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
  return (
    ready.onItsWay.length > 0 || (ready.latest !== undefined && !sameText(ready.latest, ready.kept))
  );
}

function readyFrom(note: Note, reading: number): Ready {
  return {
    state: "ready",
    reading,
    note,
    linked: linkedOf(note, new Map()),
    refused: new Set(),
    shown: note.edition,
    latest: undefined,
    kept: textOfNote(note),
    keeping: KEPT,
    handedIn: 0,
    excerpt: undefined,
    onItsWay: [],
    waiters: [],
    rereading: undefined,
    retry: { attempt: 0, timer: undefined },
  };
}

// A note made just now, which this visit holds as read.
export function adopt(note: Note): void {
  readCount += 1;
  sessions.set(note.id, readyFrom(note, readCount));
  tell();
}

export function forget(noteId: string): void {
  clearTimeout(readyOf(noteId)?.retry.timer);
  sessions.delete(noteId);
  tell();
}

// Reads a note. Read anew, it is loading until it is read. Otherwise a note this visit holds
// already stays on show while it is read again, and
// stays as it is when the service holds the same edition of it, when what was typed meanwhile
// is not kept, or when it cannot be read; so an editor of it is made anew only for another
// edition.
function startReading(noteId: string, anew: boolean): Promise<Ready | undefined> {
  readCount += 1;
  const reading = readCount;
  const read: Promise<Ready | undefined> = readNote(noteId).then(
    (note) => {
      const now = readyOf(noteId);
      if (now !== undefined && (unkept(now) || (!anew && now.note.edition === note.edition))) {
        update(noteId, (held) => ({ ...held, rereading: undefined }));
        return readyOf(noteId);
      }
      const ready = readyFrom(note, reading);
      sessions.set(noteId, ready);
      tell();
      return ready;
    },
    (error: unknown) => {
      const gone = error instanceof Refused && error.status === NOT_FOUND;
      if (!gone && readyOf(noteId) !== undefined) {
        update(noteId, (held) => ({ ...held, rereading: undefined }));
        throw error;
      }
      sessions.set(noteId, { state: "failed", reason: gone ? GONE : failureText(error), gone });
      tell();
      if (gone) {
        return undefined;
      }
      throw error;
    },
  );
  if (anew || readyOf(noteId) === undefined) {
    sessions.set(noteId, { state: "loading", reading, read });
  } else {
    update(noteId, (held) => ({ ...held, rereading: read }));
  }
  tell();
  return read;
}

// The note as the service holds it now: read again unless this visit holds text of it that is
// not kept yet, which stays as it is. Undefined when the note is not there.
export function freshen(noteId: string): Promise<Ready | undefined> {
  // What is being typed in the note's editor is text not kept yet too.
  const typing = writerOf(noteId)?.flush(false) ?? false;
  const session = sessions.get(noteId);
  if (session?.state === "loading") {
    return session.read;
  }
  if (session?.state === "ready") {
    if (typing || unkept(session)) {
      return Promise.resolve(session);
    }
    if (session.rereading !== undefined) {
      return session.rereading;
    }
  }
  return startReading(noteId, false);
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

function landed(held: Ready, sent: OnItsWay): OnItsWay[] {
  return held.onItsWay.filter((other) => other !== sent);
}

function sentAndKept(noteId: string, sent: OnItsWay, note: Note): void {
  clearTimeout(readyOf(noteId)?.retry.timer);
  update(noteId, (held) => {
    const onItsWay = landed(held, sent);
    return {
      ...held,
      onItsWay,
      note,
      shown: Math.max(held.shown, note.edition),
      kept: sent.text,
      linked: linkedOf(note, held.linked),
      excerpt: held.excerpt === sent.number ? undefined : held.excerpt,
      keeping: held.latest === sent.text && onItsWay.length === 0 ? KEPT : held.keeping,
      retry: { attempt: 0, timer: undefined },
    };
  });
  keptListeners.forEach((notify) => notify(note));
  settle(noteId, sent.number, undefined);
  keep(noteId, false);
}

// The service answers not found both for a note that is not there and for a passage whose
// document is not there, so a note is asked for to tell the two apart.
async function refusal(
  noteId: string,
  error: unknown,
): Promise<"overtaken" | "removed" | "passage" | "other"> {
  if (!(error instanceof Refused)) {
    return "other";
  }
  if (error.status === STALE) {
    return "overtaken";
  }
  if (error.status === CONFLICT) {
    return "passage";
  }
  if (error.status !== NOT_FOUND) {
    return "other";
  }
  try {
    await readNote(noteId);
    return "passage";
  } catch (reading) {
    return reading instanceof Refused && reading.status === NOT_FOUND ? "removed" : "other";
  }
}

// The passages of a text that the service is not known to have taken: those it refused are
// among them.
function newPassages(text: Text, kept: Text): string[] {
  const held = new Set(kept.passages.map((passage) => JSON.stringify(passage)));
  return text.passages
    .filter((passage) => !held.has(JSON.stringify(passage)))
    .map((passage) => passage.id);
}

// The words of a refused passage, as a reader finds its chip by them.
export function refusedWords(ready: Ready): string[] {
  return [...ready.refused].flatMap((passageId) => {
    const linked = ready.linked.get(passageId);
    const quote = linked?.passage.anchors.map((anchor) => anchor.quote).join(" … ") ?? "";
    return linked === undefined
      ? []
      : [`「${[...quote].slice(0, QUOTED_WORDS).join("")}${[...quote].length > QUOTED_WORDS ? "…" : ""}」`];
  });
}

async function refused(noteId: string, sent: OnItsWay, error: unknown): Promise<void> {
  const kind = await refusal(noteId, error);
  const held = readyOf(noteId);
  if (held === undefined) {
    return;
  }
  const others = landed(held, sent);
  // A draft overtaken by a later one of this visit, sent as the page was hidden or left while
  // it was on its way, is no text written elsewhere: what came of the later one stands.
  if (
    kind === "overtaken" &&
    (held.shown >= sent.edition || others.some((other) => other.edition > sent.edition))
  ) {
    update(noteId, (ready) => ({ ...ready, onItsWay: landed(ready, sent) }));
    keep(noteId, false);
    return;
  }
  // A passage refused in a text typed here, as one pasted from elsewhere, does not hold the rest
  // of the note back: the passages the service had not taken before are linked no more, their
  // chips stand as their words, and the text is sent again.
  const suspects = newPassages(sent.text, held.kept);
  if (kind === "passage" && held.excerpt !== sent.number && suspects.length > 0) {
    update(noteId, (ready) => ({
      ...ready,
      onItsWay: landed(ready, sent),
      refused: new Set([...ready.refused, ...suspects]),
      latest: ready.latest === undefined ? undefined : withoutPassages(ready.latest, suspects),
    }));
    keep(noteId, false);
    return;
  }
  const overtaken = kind === "overtaken";
  const removed = kind === "removed";
  const reason = failureText(error);
  const away = unreached(error);
  update(noteId, (ready) => ({
    ...ready,
    onItsWay: landed(ready, sent),
    keeping: overtaken
      ? { state: "overtaken" }
      : removed
        ? { state: "removed" }
        : { state: "unkept", reason, away },
  }));
  if (away) {
    keepLater(noteId);
  }
  const failure = overtaken
    ? new NotSent(OVERTAKEN)
    : removed
      ? new NotSent(REMOVED)
      : new Error(reason);
  // Nothing handed in is sent again by itself, so whatever waited for a later text is not kept.
  settle(noteId, Infinity, failure);
  // A refused text that was overtaken by a newer one of this visit does not hold the newer back.
  const ready = readyOf(noteId);
  if (!overtaken && !removed && ready?.latest !== undefined && ready.latest !== sent.text) {
    update(noteId, (now) => ({ ...now, keeping: { state: "keeping" } }));
    keep(noteId, false);
  }
}

// A text that could not be sent because the service did not answer is sent again by itself:
// soon the first time, then at growing intervals up to half a minute; at once when the browser
// is online again or the page comes back on show (keepAway). It goes as any text does,
// numbered as it is sent, so that a note written elsewhere meanwhile is still told.
function keepLater(noteId: string): void {
  const ready = readyOf(noteId);
  if (ready === undefined) {
    return;
  }
  clearTimeout(ready.retry.timer);
  const attempt = ready.retry.attempt;
  const timer = setTimeout(
    () => keepAway(noteId),
    Math.min(RETRY_FIRST_MS * 2 ** attempt, RETRY_LAST_MS),
  );
  update(noteId, (held) => ({ ...held, retry: { attempt: attempt + 1, timer } }));
}

function keepAway(noteId: string): void {
  const ready = readyOf(noteId);
  if (
    ready?.keeping.state === "unkept" &&
    ready.keeping.away &&
    ready.onItsWay.length === 0
  ) {
    keepAgain(noteId);
  }
}

// Every text the service did not answer for is sent again at once.
function keepAllAway(): void {
  for (const noteId of sessions.keys()) {
    keepAway(noteId);
  }
}

function withoutPassages(text: Text, passageIds: string[]): Text {
  return { ...text, passages: text.passages.filter(({ id }) => !passageIds.includes(id)) };
}

// Sends the latest text of a note unless the service keeps it already, numbered one after the
// edition the service last showed. One request is under way at a time, and the text handed in
// while it is goes next. As the page is left, the latest text is sent at once, one after the
// request on its way, so that it wins over that one and is not lost with the page.
function keep(noteId: string, leaving: boolean): void {
  const ready = readyOf(noteId);
  const latest = ready?.latest;
  if (ready === undefined || latest === undefined) {
    return;
  }
  if (ready.keeping.state === "overtaken" || ready.keeping.state === "removed") {
    return;
  }
  const busy = ready.onItsWay.length > 0;
  if (!busy && sameText(latest, ready.kept)) {
    update(noteId, (held) => ({ ...held, keeping: KEPT }));
    settle(noteId, ready.handedIn, undefined);
    return;
  }
  if (busy && !leaving) {
    return;
  }
  const edition = Math.max(ready.shown, ...ready.onItsWay.map((other) => other.edition)) + 1;
  const sent: OnItsWay = {
    edition,
    text: latest,
    number: ready.handedIn,
    excerpt: ready.excerpt === ready.handedIn,
  };
  const draft: NoteDraft = { ...latest, edition };
  const { reading } = ready;
  clearTimeout(ready.retry.timer);
  update(noteId, (held) => ({
    ...held,
    onItsWay: [...held.onItsWay, sent],
    keeping: { state: "keeping" },
    retry: { ...held.retry, timer: undefined },
  }));
  // A draft too large to outlive the page goes as any other, which the browser asks about.
  rewriteNote(noteId, draft, leaving && outlives(draft)).then(
    (note) => ofReading(noteId, reading) && sentAndKept(noteId, sent, note),
    (error: unknown) => ofReading(noteId, reading) && void refused(noteId, sent, error),
  );
}

// Takes a note's text to be kept; the number it answers tells this text from those handed in
// before it. A text that carries an excerpt is refused as a whole, never in part. A text typed
// into a reading of the note that was given up since is not this note's text any more.
export function handIn(
  noteId: string,
  text: Text,
  leaving: boolean,
  excerpt = false,
  reading?: number,
): number {
  const ready = readyOf(noteId);
  if (ready === undefined || (reading !== undefined && reading !== ready.reading)) {
    return 0;
  }
  watchLeaving();
  const number = ready.handedIn + 1;
  update(noteId, (held) => ({
    ...held,
    handedIn: number,
    excerpt: excerpt ? number : held.excerpt,
    latest: withoutPassages(text, [...held.refused]),
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
  if (ready.keeping.state === "kept" && ready.onItsWay.length === 0) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    update(noteId, (held) => ({
      ...held,
      waiters: [...held.waiters, { draft, resolve, reject }],
    }));
  });
}

// Gives up a text handed in that was not kept, and puts back what the note held before it: kept
// again, or, where the note was overtaken or removed, held as the text the reader sees.
export function withdraw(noteId: string, before: Text): void {
  const ready = readyOf(noteId);
  if (ready === undefined) {
    return;
  }
  const stuck = ready.keeping.state === "overtaken" || ready.keeping.state === "removed";
  if (stuck || sameText(before, ready.kept)) {
    update(noteId, (held) => ({
      ...held,
      excerpt: undefined,
      latest: sameText(before, held.kept) ? undefined : before,
      keeping: stuck ? held.keeping : KEPT,
    }));
    return;
  }
  handIn(noteId, before, false);
}

// The text of an overtaken note takes the place of what the service holds now: the note is
// read, and the text the reader sees is sent one after the edition it holds.
export async function putInPlace(noteId: string): Promise<void> {
  writerOf(noteId)?.flush(false);
  const note = await readNote(noteId);
  // The text here is set against what the service holds now, which is no longer what this
  // visit last knew it to keep.
  update(noteId, (held) => ({
    ...held,
    note,
    shown: Math.max(held.shown, note.edition),
    latest: textOf(held),
    kept: textOfNote(note),
    keeping: { state: "keeping" },
  }));
  keep(noteId, false);
}

// The text of an overtaken or removed note, all that was typed in it, is kept as a new note,
// which is answered; the note it was written in is read as the service holds it.
export async function keepAsNew(noteId: string): Promise<Note> {
  writerOf(noteId)?.flush(true);
  const ready = readyOf(noteId);
  if (ready === undefined) {
    throw new Error(GONE);
  }
  const text = textOf(ready);
  const title = [...(text.title || UNTITLED)].slice(0, TITLE_CHARACTERS - KEPT_APART.length);
  const note = await writeNote({ ...text, title: `${title.join("")}${KEPT_APART}`, edition: 1 });
  adopt(note);
  keptListeners.forEach((notify) => notify(note));
  void letGo(noteId);
  return note;
}

// What this visit holds of a note is given up, and the note is read as the service holds it.
// Until it is read the note is loading, so that nothing is typed into the text given up, and
// what comes of the requests on their way, which belongs to that text, is dropped.
export async function letGo(noteId: string): Promise<void> {
  if (readyOf(noteId) === undefined) {
    return;
  }
  settle(noteId, Infinity, new NotSent(OVERTAKEN));
  await startReading(noteId, true).catch(() => undefined);
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

// The passages a text of a note carries: those of these ids that the note holds and the
// service has not refused.
export function passagesOf(noteId: string, passageIds: string[]): Text["passages"] {
  const ready = readyOf(noteId);
  return passageIds.flatMap((id) => {
    const held = ready?.linked.get(id);
    return held === undefined || ready?.refused.has(id) ? [] : [{ id, passage: held.passage }];
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

function unkeptDrafts(): Text[] {
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
      !session.onItsWay.some((sent) => sent.text === session.latest)
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
    (session) =>
      session.state === "ready" &&
      session.keeping.state !== "kept" &&
      session.keeping.state !== "keeping",
  );
  if (
    drafts.length > 0 &&
    (stuck || drafts.some((text) => !outlives({ ...text, edition: Number.MAX_SAFE_INTEGER })))
  ) {
    event.preventDefault();
  }
}

// Hidden, the page hands in what was typed; back on show, it sends again what the service did
// not answer for.
function onShownOrHidden(): void {
  if (document.visibilityState === "hidden") {
    leaving();
  } else {
    keepAllAway();
  }
}

// The page is watched for being left from the first text written, for the rest of the visit.
function watchLeaving(): void {
  if (!watching) {
    watching = true;
    addEventListener("beforeunload", warnBeforeLeaving);
    addEventListener("pagehide", leaving);
    document.addEventListener("visibilitychange", onShownOrHidden);
    addEventListener("online", keepAllAway);
  }
}

// Reads the note when it is opened unless this visit holds text of it not kept yet.
export function useOpening(noteId: string): void {
  useEffect(() => {
    freshen(noteId).catch(() => undefined);
  }, [noteId]);
}
