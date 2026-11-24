import type { Anchors } from "./client";

// What is put into a note from elsewhere in the app, and the one way to put it there.

// A passage that an excerpt rests on, and the item it lies in.
export type Cited = { anchors: Anchors; itemId: string };

// A statement infonium wrote, of a digest or an answer, with the passages it cites; or a
// passage quoted from a source.
export type Excerpt =
  | { kind: "statement"; text: string; cited: Cited[] }
  | { kind: "quote"; cited: Cited };

const UNBUILT = "笔记本还没有做好，记不下来。";

// Puts an excerpt at the end of the note being written, with links to its passages, and makes
// a note for it when none is being written.
export function noteDown(excerpt: Excerpt): Promise<void> {
  return Promise.reject(new Error(`${UNBUILT}（${excerpt.kind}）`));
}
