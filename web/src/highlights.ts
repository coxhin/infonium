import { useEffect, useSyncExternalStore } from "react";
import {
  anchorsOf,
  commentHighlight,
  makeHighlight,
  readHighlights,
  removeHighlight,
  type Highlight,
  type Passage,
} from "./client";
import { reasonOf, type Loaded } from "./loading";
import type { Marked } from "./marking";

// The highlights of the items the app has shown, kept apart from any one view: the reader
// draws them, the notebook lists them, and either changes them. An item's highlights say where
// they stand in the document the item holds now, so they are read again when that document is
// another.
type Shelf = { documentId: string; loaded: Loaded<Highlight[]> };

// The highlight the reader has opened: pressed in the source or chosen in the notebook, which
// shows it; commenting says that the reader means to note something on it.
export type Opened = { itemId: string; highlightId: string; commenting: boolean };

const LOADING: Loaded<Highlight[]> = { state: "loading" };
const shelves = new Map<string, Shelf>();
const listeners = new Set<() => void>();
let opened: Opened | undefined;

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

export async function markPassage(
  itemId: string,
  passage: Passage,
  comment: string,
): Promise<Highlight> {
  const made = await makeHighlight(itemId, passage, comment);
  await reread(itemId);
  return made;
}

export async function noteOnHighlight(
  itemId: string,
  highlightId: string,
  text: string,
): Promise<void> {
  await commentHighlight(itemId, highlightId, text);
  await reread(itemId);
}

export async function unmark(itemId: string, highlightId: string): Promise<void> {
  await removeHighlight(itemId, highlightId);
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
