import { useEffect, useSyncExternalStore } from "react";
import {
  anchorsOf,
  commentHighlight,
  makeHighlight,
  readHighlights,
  readLibraryHighlights,
  Refused,
  removeHighlight,
  type Highlight,
  type ItemHighlights,
  type Passage,
} from "./client";
import { reasonOf, type Loaded } from "./loading";
import type { Marked } from "./marking";

// The highlights of the items the app has shown, kept apart from any one view: the reader
// draws them, the notebook lists them, and either changes them. An item's highlights say where
// they stand in the document the item holds now, so they are read again when that document is
// another, and again whenever the item is opened or the notebook shows them, since another tab
// may have changed them. A change is shown at once, and taken back when the service refuses it.
// Behind says that the list on show could not be read again when it was last asked for, so
// that it may not hold what the service holds now.
type Shelf = { documentId: string; loaded: Loaded<Highlight[]>; behind: boolean };

// The highlight the reader has opened: pressed in the source or chosen in the notebook, which
// shows it; commenting says that the reader means to note something on it.
export type Opened = { itemId: string; highlightId: string; commenting: boolean };

// How the reads and the changes of one list stand, so that an answer that says less than what
// is on show is dropped. Every read and every change is numbered in the order it began. A read
// is kept only if no change was under way when it began, no change began after it, and no read
// that began after it has been kept: a read that began before a change, or while one was
// awaited, may hold the list as it was before that change.
type Pace = {
  // Changes shown at once that the service has not answered yet.
  awaited: number;
  // The number of the latest change shown.
  changed: number;
  // The number of the latest read kept.
  kept: number;
  // The number of the latest read on its way that began with no change awaited, if one is.
  underWay: number | undefined;
};

const LOADING: Loaded<Highlight[]> = { state: "loading" };
const LIBRARY_LOADING: Loaded<ItemHighlights[]> = { state: "loading" };
// The pace of every item's list; an item's id is hexadecimal, so it is never this key.
const LIBRARY = "library";
const shelves = new Map<string, Shelf>();
const paces = new Map<string, Pace>();
const listeners = new Set<() => void>();
let counted = 0;
let opened: Opened | undefined;
// Every item's highlights, as the notebook lists them with no item open.
let library: Loaded<ItemHighlights[]> = LIBRARY_LOADING;
let libraryBehind = false;
// The service answers a change of a highlight that is not in the library, as one removed in
// another tab, with this status.
const NOT_FOUND = 404;

function tell(): void {
  listeners.forEach((notify) => notify());
}

function onChange(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

function paceOf(key: string): Pace {
  const held = paces.get(key);
  if (held !== undefined) {
    return held;
  }
  const pace: Pace = { awaited: 0, changed: 0, kept: 0, underWay: undefined };
  paces.set(key, pace);
  return pace;
}

// Reads a list, and hands what was read on unless an answer is due that says more. A read
// that fails while a list is on show leaves the list there.
async function readList<Value>(
  key: string,
  request: () => Promise<Value>,
  keep: (loaded: Loaded<Value>) => void,
): Promise<void> {
  const pace = paceOf(key);
  counted += 1;
  const begun = counted;
  const clean = pace.awaited === 0;
  if (clean) {
    pace.underWay = begun;
  }
  let loaded: Loaded<Value>;
  try {
    loaded = { state: "ready", value: await request() };
  } catch (error) {
    loaded = { state: "failed", reason: reasonOf(error) };
  }
  if (pace.underWay === begun) {
    pace.underWay = undefined;
  }
  if (clean && begun > pace.changed && begun > pace.kept) {
    pace.kept = begun;
    keep(loaded);
  }
}

// Whether a read on its way will say all that is on show, so that another is not needed.
function readingAfresh(key: string): boolean {
  const pace = paceOf(key);
  return pace.underWay !== undefined && pace.underWay > pace.changed && pace.awaited === 0;
}

// Reads an item's highlights, and keeps them unless the item has come to show another
// document since. What is on show stays until the reading is in.
function read(itemId: string, documentId: string): Promise<void> {
  return readList(
    itemId,
    () => readHighlights(itemId),
    (loaded) => {
      const shelf = shelves.get(itemId);
      if (shelf?.documentId !== documentId) {
        return;
      }
      const behind = loaded.state === "failed" && shelf.loaded.state === "ready";
      shelves.set(itemId, { documentId, loaded: behind ? shelf.loaded : loaded, behind });
      tell();
    },
  );
}

function reread(itemId: string): Promise<void> {
  const shelf = shelves.get(itemId);
  return shelf === undefined || readingAfresh(itemId)
    ? Promise.resolve()
    : read(itemId, shelf.documentId);
}

function readLibrary(): Promise<void> {
  return readList(LIBRARY, readLibraryHighlights, (loaded) => {
    libraryBehind = loaded.state === "failed" && library.state === "ready";
    library = libraryBehind ? library : loaded;
    tell();
  });
}

// The list of every item's highlights is read again after a change only once it has been read.
function rereadLibrary(): Promise<void> {
  return library.state === "loading" || readingAfresh(LIBRARY)
    ? Promise.resolve()
    : readLibrary();
}

// Reads an item's highlights again, as when the notebook comes to show them, unless a read
// that will say all that is on show is on its way.
export function readHighlightsAgain(itemId: string): void {
  void reread(itemId);
}

// Where a change is shown: in the item's list, and in the list of every item's highlights.
type Change = {
  item: (highlights: Highlight[]) => Highlight[];
  library: (held: ItemHighlights[]) => ItemHighlights[];
};

function inItem(
  itemId: string,
  changed: (highlights: Highlight[]) => Highlight[],
): (held: ItemHighlights[]) => ItemHighlights[] {
  return (held) =>
    held.map((group) =>
      group.item === itemId ? { ...group, highlights: changed(group.highlights) } : group,
    );
}

function apply(itemId: string, change: Change): void {
  const shelf = shelves.get(itemId);
  if (shelf?.loaded.state === "ready") {
    shelves.set(itemId, {
      ...shelf,
      loaded: { state: "ready", value: change.item(shelf.loaded.value) },
    });
  }
  if (library.state === "ready") {
    library = { state: "ready", value: change.library(library.value) };
  }
  tell();
}

// Marks a change as begun in both lists, so that a read that began before it is dropped.
function begin(itemId: string): void {
  counted += 1;
  for (const key of [itemId, LIBRARY]) {
    paceOf(key).changed = counted;
  }
}

// Whether the service refused a change because the highlight is no longer in the library.
export function highlightGone(error: unknown): boolean {
  return error instanceof Refused && error.status === NOT_FOUND;
}

// Takes a highlight out of both lists, and an item with no highlights left out of the
// library's.
function removal(itemId: string, highlightId: string): Change {
  const left = (highlights: Highlight[]) =>
    highlights.filter((held) => held.id !== highlightId);
  return {
    item: left,
    library: (held) => inItem(itemId, left)(held).filter((kept) => kept.highlights.length > 0),
  };
}

// Shows a change before the service has answered it, and takes it back if the service
// refuses it; the refusal goes on to the caller, which says why. A highlight the service no
// longer holds is taken out of the lists at once, whatever comes of the reading that follows.
// Either way both lists are read again once the service has answered, since a refusal may mean
// that the list on show was already wrong, as when another tab removed the highlight.
async function shownAtOnce(
  itemId: string,
  highlightId: string,
  change: Change,
  restore: Change,
  request: () => Promise<unknown>,
): Promise<void> {
  const keys = [itemId, LIBRARY];
  begin(itemId);
  keys.forEach((key) => (paceOf(key).awaited += 1));
  apply(itemId, change);
  try {
    await request();
  } catch (error) {
    apply(itemId, highlightGone(error) ? removal(itemId, highlightId) : restore);
    throw error;
  } finally {
    keys.forEach((key) => (paceOf(key).awaited -= 1));
    void rereadLibrary();
    void reread(itemId);
  }
}

// The highlights of an item as they stand in the document it shows; loading until both are
// known and the highlights are read. They are read again each time a view of them is made.
export function useHighlights(
  itemId: string | undefined,
  documentId: string | undefined,
): Loaded<Highlight[]> {
  const loaded = useSyncExternalStore(onChange, () =>
    itemId === undefined ? LOADING : (shelves.get(itemId)?.loaded ?? LOADING),
  );
  useEffect(() => {
    if (itemId === undefined || documentId === undefined) {
      return undefined;
    }
    if (shelves.get(itemId)?.documentId === documentId) {
      void reread(itemId);
    } else {
      shelves.set(itemId, { documentId, loaded: LOADING, behind: false });
      tell();
      void read(itemId, documentId);
    }
    return watch(itemId);
  }, [itemId, documentId]);
  return loaded;
}

// Whether an item's list on show, or the library's where no item is given, could not be read
// again when it was last asked for.
export function useHighlightsBehind(itemId: string | undefined): boolean {
  return useSyncExternalStore(onChange, () =>
    itemId === undefined ? libraryBehind : (shelves.get(itemId)?.behind ?? false),
  );
}

// Every item that has highlights, with them; read again each time the list comes on show,
// as highlights of items not on show may have changed since. What was read stays on show
// while it is read again.
export function useLibraryHighlights(shown: boolean): Loaded<ItemHighlights[]> {
  const loaded = useSyncExternalStore(onChange, () => library);
  useEffect(() => {
    if (!shown) {
      return undefined;
    }
    void readLibrary();
    return watch(LIBRARY);
  }, [shown]);
  return loaded;
}

// The lists on show, by how many views show each: an item's, or the library's. Another tab
// may change them, so they are read again when the page comes back on show and when its window
// is turned to again; a read is kept by the rule of every read, so it never undoes a change
// made here since.
const watched = new Map<string, number>();

function watch(key: string): () => void {
  watched.set(key, (watched.get(key) ?? 0) + 1);
  return () => {
    const left = (watched.get(key) ?? 1) - 1;
    if (left === 0) {
      watched.delete(key);
    } else {
      watched.set(key, left);
    }
  };
}

function readWatchedAgain(): void {
  if (document.visibilityState !== "visible") {
    return;
  }
  for (const key of watched.keys()) {
    void (key === LIBRARY ? rereadLibrary() : reread(key));
  }
}

document.addEventListener("visibilitychange", readWatchedAgain);
addEventListener("focus", readWatchedAgain);

// The highlights a view draws: those that stand in the document on show.
export function markedOf(highlights: Highlight[]): Marked[] {
  return highlights.flatMap((highlight) =>
    highlight.standing.kind === "found"
      ? [
          {
            id: highlight.id,
            anchors: anchorsOf(highlight.standing.passage),
            commented: highlight.comment !== "",
          },
        ]
      : [],
  );
}

// The new highlight is shown at the end of the list until the list, read again, puts it in
// its place.
// What marking a passage came to: the highlight the service holds for it, and whether it held
// it already, as when the very passage was marked before. A highlight held already is not
// added to a list again.
export type PassageMarked = { highlight: Highlight; already: boolean };

function holds(highlights: Highlight[], highlightId: string): boolean {
  return highlights.some((highlight) => highlight.id === highlightId);
}

export async function markPassage(
  itemId: string,
  passage: Passage,
  comment: string,
): Promise<PassageMarked> {
  const made = await makeHighlight(itemId, passage, comment);
  const shelf = shelves.get(itemId)?.loaded;
  const already = shelf?.state === "ready" && holds(shelf.value, made.id);
  const added = (highlights: Highlight[]) =>
    holds(highlights, made.id) ? highlights : [...highlights, made];
  begin(itemId);
  apply(itemId, { item: added, library: inItem(itemId, added) });
  void rereadLibrary();
  await reread(itemId);
  return { highlight: made, already };
}

// Openings of a highlight that the reader marked again, which the notebook says was marked
// already.
const markedAgain = new WeakSet<Opened>();

export function openMarkedAgain(next: Opened): void {
  markedAgain.add(next);
  openHighlight(next);
}

export function wasMarkedAgain(opening: Opened): boolean {
  return markedAgain.has(opening);
}

export async function noteOnHighlight(
  itemId: string,
  highlightId: string,
  text: string,
): Promise<void> {
  let before: string | undefined;
  const commented = (comment: (was: string) => string) => (highlights: Highlight[]) =>
    highlights.map((highlight) =>
      highlight.id === highlightId
        ? { ...highlight, comment: comment(highlight.comment) }
        : highlight,
    );
  const writing = commented((was) => {
    before ??= was;
    return text;
  });
  const restoring = commented((was) => before ?? was);
  await shownAtOnce(
    itemId,
    highlightId,
    { item: writing, library: inItem(itemId, writing) },
    { item: restoring, library: inItem(itemId, restoring) },
    () => commentHighlight(itemId, highlightId, text),
  );
}

// Where a highlight stood in a list, so that a refusal to remove it puts it back there.
function placeIn(highlights: Highlight[] | undefined, highlightId: string): number {
  return highlights?.findIndex((highlight) => highlight.id === highlightId) ?? -1;
}

function insertedAt<Value>(values: Value[], at: number, value: Value): Value[] {
  const place = Math.max(at, 0);
  return [...values.slice(0, place), value, ...values.slice(place)];
}

function putBack(highlights: Highlight[], at: number, highlight: Highlight): Highlight[] {
  return placeIn(highlights, highlight.id) >= 0
    ? highlights
    : insertedAt(highlights, at, highlight);
}

export async function unmark(itemId: string, highlightId: string): Promise<void> {
  const shelf = shelves.get(itemId)?.loaded;
  const listed = shelf?.state === "ready" ? shelf.value : undefined;
  const groups = library.state === "ready" ? library.value : [];
  const groupAt = groups.findIndex((group) => group.item === itemId);
  const group = groups[groupAt];
  const itemAt = placeIn(listed, highlightId);
  const groupItemAt = placeIn(group?.highlights, highlightId);
  const highlight = listed?.[itemAt] ?? group?.highlights[groupItemAt];
  const removed = shownAtOnce(
    itemId,
    highlightId,
    removal(itemId, highlightId),
    {
      item: (highlights) =>
        highlight === undefined ? highlights : putBack(highlights, itemAt, highlight),
      library: (held) => {
        if (highlight === undefined || group === undefined) {
          return held;
        }
        return held.some((kept) => kept.item === itemId)
          ? inItem(itemId, (highlights) => putBack(highlights, groupItemAt, highlight))(held)
          : insertedAt(held, groupAt, { ...group, highlights: [highlight] });
      },
    },
    () => removeHighlight(itemId, highlightId),
  );
  // A highlight already gone, as one removed in another tab, is removed as was asked.
  await removed.catch((error: unknown) => {
    if (!highlightGone(error)) {
      throw error;
    }
  });
  if (opened?.itemId === itemId && opened.highlightId === highlightId) {
    opened = undefined;
  }
}

export function openHighlight(next: Opened | undefined): void {
  opened = next;
  tell();
}

export function useOpenedHighlight(): Opened | undefined {
  return useSyncExternalStore(onChange, () => opened);
}
