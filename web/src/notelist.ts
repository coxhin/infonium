import { useEffect, useSyncExternalStore } from "react";
import {
  listNotes,
  readNote,
  Refused,
  removeNote,
  type Note,
  type NoteEntry,
} from "./client";
import type { Loaded } from "./loading";
import { firstWords } from "./notemarkdown";
import {
  blankAndUnborn,
  failureText,
  flushWriter,
  forget,
  onBorn,
  onKept,
  onSessions,
  sessionOf,
  startNote,
  textOf,
} from "./notekeeping";

// The notes of the library as the notebook lists them, and which of them is open.

const LOADING: Loaded<never> = { state: "loading" };
const NOT_FOUND = 404;

const listeners = new Set<() => void>();
let notes: Loaded<NoteEntry[]> = LOADING;
let listReadings = 0;
let opened: string | undefined;
let openings = 0;
let left: string | undefined;
let justMade: string | undefined;
// The first words of notes without a title, read for the list, by the note and when it was
// changed.
const firstWordsRead = new Map<string, { changed: string; words: Loaded<string> }>();

function tell(): void {
  listeners.forEach((notify) => notify());
}

function onChange(notify: () => void): () => void {
  listeners.add(notify);
  const unsubscribe = onSessions(notify);
  return () => {
    listeners.delete(notify);
    unsubscribe();
  };
}

// What a note is called where it is listed or named: its title, or its first words.
export function noteName(title: string, markdown: string): string {
  return title.trim() || firstWords(markdown) || "空白笔记";
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
  tell();
}

onKept(listed);

// A note started here and left with nothing written in it is not kept anywhere.
export function openNote(noteId: string | undefined): void {
  flushWriter();
  if (opened !== undefined && opened !== noteId && blankAndUnborn(opened)) {
    forget(opened);
  }
  left = opened ?? left;
  opened = noteId;
  openings += 1;
  tell();
}

// A note started here goes by its own id once it is kept, and stays open as it was.
onBorn((unborn, noteId) => {
  opened = opened === unborn ? noteId : opened;
  left = left === unborn ? noteId : left;
  justMade = justMade === unborn ? noteId : justMade;
  tell();
});

// How many times a note was opened: an editor is made for each opening, and not again when the
// open note comes to go by its own id.
export function useOpenings(): number {
  return useSyncExternalStore(onChange, () => openings);
}

export function openedNote(): string | undefined {
  return opened;
}

export function useOpenedNote(): string | undefined {
  return useSyncExternalStore(onChange, () => opened);
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

function heldMarkdown(noteId: string): string | undefined {
  const session = sessionOf(noteId);
  return session?.state === "ready" ? textOf(session).markdown : undefined;
}

// What a note without a title is called in the list: its first words, from what is written in
// it this visit or read for the purpose.
export function useFirstWords(entry: NoteEntry): Loaded<string> {
  const words = useSyncExternalStore(
    onChange,
    () => heldMarkdown(entry.id) ?? firstWordsRead.get(entry.id),
  );
  useEffect(() => {
    const read = firstWordsRead.get(entry.id);
    if (
      entry.title !== "" ||
      heldMarkdown(entry.id) !== undefined ||
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

// Makes an empty note, at the first edition, and opens it.
// Opens a new note, to be written in; it comes to be in the library when it first holds a title
// or text.
export function makeNote(): void {
  const noteId = startNote();
  justMade = noteId;
  openNote(noteId);
}

// Removes a note. One that was removed elsewhere already is gone as asked.
export async function dropNote(noteId: string): Promise<void> {
  try {
    await removeNote(noteId);
  } catch (error) {
    if (!(error instanceof Refused && error.status === NOT_FOUND)) {
      throw error;
    }
  }
  forget(noteId);
  firstWordsRead.delete(noteId);
  if (notes.state === "ready") {
    notes = { state: "ready", value: notes.value.filter((entry) => entry.id !== noteId) };
  }
  if (opened === noteId) {
    opened = undefined;
  }
  tell();
}
