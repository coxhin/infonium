import { useSyncExternalStore } from "react";
import type { Anchors } from "./client";

// What is put into a note from elsewhere in the app, the one way to put it there, and the one
// way to ask for a note to be shown. The notebook's notes are built on this.

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

const UNBUILT = "笔记还没有做好，记不下来。";

const listeners = new Set<() => void>();
let shown: Shown | undefined;

function onChange(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

// Puts an excerpt at the end of the note being written, with links to its passages, and makes
// a note for it when none is being written; the result says where it went.
export function noteDown(excerpt: Excerpt): Promise<Noted> {
  return Promise.reject(new Error(`${UNBUILT}（${excerpt.kind}）`));
}

// Asks for a note to be shown: the notebook comes on show with the note open in it.
export function showNote(noteId: string): void {
  shown = { noteId, asking: (shown?.asking ?? 0) + 1 };
  listeners.forEach((notify) => notify());
}

export function useShownNote(): Shown | undefined {
  return useSyncExternalStore(onChange, () => shown);
}
