import { useEffect, useSyncExternalStore } from "react";
import {
  anchorsOf,
  commentHighlight,
  makeHighlight,
  readHighlights,
  readLibraryHighlights,
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
// another. A change is shown at once, and taken back when the service refuses it.
type Shelf = { documentId: string; loaded: Loaded<Highlight[]> };

// The highlight the reader has opened: pressed in the source or chosen in the notebook, which
// shows it; commenting says that the reader means to note something on it.
export type Opened = { itemId: string; highlightId: string; commenting: boolean };

const LOADING: Loaded<Highlight[]> = { state: "loading" };
const LIBRARY_LOADING: Loaded<ItemHighlights[]> = { state: "loading" };
const shelves = new Map<string, Shelf>();
const listeners = new Set<() => void>();
let opened: Opened | undefined;
// Every item's highlights, as the notebook lists them with no item open.
let library: Loaded<ItemHighlights[]> = LIBRARY_LOADING;

function tell(): void {
  listeners.forEach((notify) => notify());
}

function onChange(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

// Reads an item's highlights, and keeps them unless the item has come to show another
// document since. What is on show stays until the reading is in.
async function read(itemId: string, documentId: string): Promise<void> {
  let loaded: Loaded<Highlight[]>;
  try {
    loaded = { state: "ready", value: await readHighlights(itemId) };
  } catch (error) {
    loaded = { state: "failed", reason: reasonOf(error) };
  }
  if (shelves.get(itemId)?.documentId === documentId) {
    shelves.set(itemId, { documentId, loaded });
    tell();
  }
}

function reread(itemId: string): Promise<void> {
  const shelf = shelves.get(itemId);
  return shelf === undefined ? Promise.resolve() : read(itemId, shelf.documentId);
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
      documentId: shelf.documentId,
      loaded: { state: "ready", value: change.item(shelf.loaded.value) },
    });
  }
  if (library.state === "ready") {
    library = { state: "ready", value: change.library(library.value) };
  }
  tell();
}

// Shows a change before the service has answered it, and takes it back if the service
// refuses it; the refusal goes on to the caller, which says why.
async function shownAtOnce(
  itemId: string,
  change: Change,
  restore: Change,
  request: () => Promise<unknown>,
): Promise<void> {
  apply(itemId, change);
  try {
    await request();
  } catch (error) {
    apply(itemId, restore);
    throw error;
  }
}

// The highlights of an item as they stand in the document it shows; loading until both are
// known and the highlights are read.
export function useHighlights(
  itemId: string | undefined,
  documentId: string | undefined,
): Loaded<Highlight[]> {
  const loaded = useSyncExternalStore(onChange, () =>
    itemId === undefined ? LOADING : (shelves.get(itemId)?.loaded ?? LOADING),
  );
  useEffect(() => {
    if (
      itemId !== undefined &&
      documentId !== undefined &&
      shelves.get(itemId)?.documentId !== documentId
    ) {
      shelves.set(itemId, { documentId, loaded: LOADING });
      tell();
      void read(itemId, documentId);
    }
  }, [itemId, documentId]);
  return loaded;
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
    let current = true;
    readLibraryHighlights().then(
      (value) => {
        if (current) {
          library = { state: "ready", value };
          tell();
        }
      },
      (error: unknown) => {
        if (current) {
          library = { state: "failed", reason: reasonOf(error) };
          tell();
        }
      },
    );
    return () => {
      current = false;
    };
  }, [shown]);
  return loaded;
}

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
export async function markPassage(
  itemId: string,
  passage: Passage,
  comment: string,
): Promise<Highlight> {
  const made = await makeHighlight(itemId, passage, comment);
  const added = (highlights: Highlight[]) => [...highlights, made];
  apply(itemId, { item: added, library: inItem(itemId, added) });
  await reread(itemId);
  return made;
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
    { item: writing, library: inItem(itemId, writing) },
    { item: restoring, library: inItem(itemId, restoring) },
    () => commentHighlight(itemId, highlightId, text),
  );
  await reread(itemId);
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
  const left = (highlights: Highlight[]) =>
    highlights.filter((held) => held.id !== highlightId);
  await shownAtOnce(
    itemId,
    {
      item: left,
      library: (held) =>
        inItem(itemId, left)(held).filter((kept) => kept.highlights.length > 0),
    },
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
  if (opened?.itemId === itemId && opened.highlightId === highlightId) {
    opened = undefined;
  }
  await reread(itemId);
}

export function openHighlight(next: Opened | undefined): void {
  opened = next;
  tell();
}

export function useOpenedHighlight(): Opened | undefined {
  return useSyncExternalStore(onChange, () => opened);
}
