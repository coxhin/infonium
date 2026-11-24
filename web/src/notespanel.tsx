import {
  Component,
  lazy,
  Suspense,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import type { Opener } from "./chat";
import type { Citing } from "./citing";
import type { NoteEntry } from "./client";
import { reasonOf } from "./loading";
import { failureText, onSessions, strandedNotes } from "./notekeeping";
import {
  dropNote,
  makeNote,
  noteLeft,
  noteName,
  openNote,
  rereadNotes,
  useFirstWords,
  useNotes,
  useOpenedNote,
} from "./notelist";
import type { Seat } from "./seat";
import "./notes.css";

// The editor is fetched when a note is first opened.
const NoteEditor = lazy(() => import("./noteeditor"));

const CHANGED_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  month: "numeric",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});
const CHANGED_IN_ANOTHER_YEAR = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium" });

type Doing = { state: "idle" } | { state: "underway" } | { state: "failed"; reason: string };
const IDLE: Doing = { state: "idle" };

function changedText(changed: string): string {
  const date = new Date(changed);
  return date.getFullYear() === new Date().getFullYear()
    ? CHANGED_FORMAT.format(date)
    : CHANGED_IN_ANOTHER_YEAR.format(date);
}

function sourcesText(count: number): string {
  return count === 0 ? "没有引文" : `引自 ${count} 篇`;
}

// What a note is called in the list, and whether that is its title or stands for one.
function useNoteName(entry: NoteEntry): { name: string; titled: boolean } {
  const words = useFirstWords(entry);
  if (entry.title.trim() !== "") {
    return { name: entry.title, titled: true };
  }
  if (words.state === "ready") {
    return { name: words.value === "" ? "空白笔记" : words.value, titled: false };
  }
  return { name: "无题笔记", titled: false };
}

function NoteRow({ entry, here }: { entry: NoteEntry; here: boolean }) {
  // Removing takes a second press, which confirms the first.
  const [confirming, setConfirming] = useState(false);
  const { name, titled } = useNoteName(entry);
  const [removing, setRemoving] = useState<Doing>(IDLE);

  function remove() {
    if (!confirming) {
      setConfirming(true);
      return;
    }
    setRemoving({ state: "underway" });
    dropNote(entry.id).catch((error: unknown) => {
      setConfirming(false);
      setRemoving({ state: "failed", reason: failureText(error) });
    });
  }

  return (
    <li data-here={here} data-note={entry.id}>
      <button className="open" onClick={() => openNote(entry.id)}>
        <span className={titled ? "name" : "name untitled"}>{name}</span>
        <small>
          {changedText(entry.changed)} · {sourcesText(entry.items.length)}
          {here && <span className="here"> · 引自本篇</span>}
        </small>
      </button>
      <button
        className="remove"
        aria-label={confirming ? `确认删除笔记“${name}”` : `删除笔记“${name}”`}
        disabled={removing.state === "underway"}
        onClick={remove}
        onBlur={() => setConfirming(false)}
      >
        {removing.state === "underway" ? "删除中…" : confirming ? "确认删除" : "删除"}
      </button>
      {removing.state === "failed" && (
        <p className="status failed" role="alert">
          没有删除：{removing.reason}
        </p>
      )}
    </li>
  );
}

function NoteList({ seat }: { seat: Seat | undefined }) {
  const notes = useNotes();
  const stranded = useSyncExternalStore(onSessions, strandedNotes);
  const [making, setMaking] = useState<Doing>(IDLE);
  const list = useRef<HTMLDivElement>(null);
  // Back from a note, the keyboard is given to that note's row, or else to the first control.
  const ready = notes.state === "ready";
  useEffect(() => {
    const leftNote = noteLeft();
    if (ready && leftNote !== undefined && document.activeElement === document.body) {
      const row = list.current?.querySelector<HTMLButtonElement>(
        `[data-note="${leftNote}"] .open`,
      );
      (row ?? list.current?.querySelector("button"))?.focus();
    }
  }, [ready]);

  function make() {
    setMaking({ state: "underway" });
    makeNote().then(
      () => setMaking(IDLE),
      (error: unknown) => setMaking({ state: "failed", reason: failureText(error) }),
    );
  }

  return (
    <div className="notes" ref={list}>
      <header>
        <button onClick={make} disabled={making.state === "underway"}>
          {making.state === "underway" ? "新建中…" : "新建笔记"}
        </button>
      </header>
      {making.state === "failed" && (
        <p className="status failed" role="alert">
          没有新建：{making.reason}
        </p>
      )}
      {notes.state === "loading" && <p className="status">正在读取笔记…</p>}
      {notes.state === "failed" && (
        <p className="status failed">
          笔记没有读到：{notes.reason}
          <button onClick={rereadNotes}>重试</button>
        </p>
      )}
      {notes.state === "ready" && notes.value.length === 0 && (
        <p className="status">还没有笔记。新建一篇，或把摘要、对话和高亮里的内容记到笔记。</p>
      )}
      {stranded.length > 0 && (
        <ul aria-label="在别处删除、这里还有未保存内容的笔记" className="stranded">
          {stranded.map(({ noteId, text }) => (
            <li key={noteId} data-note={noteId}>
              <button className="open" onClick={() => openNote(noteId)}>
                <span className="name">{noteName(text.title, text.markdown)}</span>
                <small>在别处删除了，这里写的还没有保存</small>
              </button>
            </li>
          ))}
        </ul>
      )}
      {notes.state === "ready" && notes.value.length > 0 && (
        <ul aria-label="笔记">
          {notes.value.map((entry) => (
            <NoteRow
              key={entry.id}
              entry={entry}
              here={seat !== undefined && entry.items.includes(seat.itemId)}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

// An editor that could not be fetched, or failed while it was on show, is replaced by the
// reason, with the way back to the list.
class EditorBoundary extends Component<{ children: ReactNode }, { reason: string | undefined }> {
  state: { reason: string | undefined } = { reason: undefined };

  static getDerivedStateFromError(error: unknown): { reason: string } {
    return { reason: reasonOf(error) };
  }

  render(): ReactNode {
    return this.state.reason === undefined ? (
      this.props.children
    ) : (
      <div className="notes">
        <button onClick={() => openNote(undefined)}>← 笔记列表</button>
        <p className="status failed">编辑器出错：{this.state.reason}</p>
      </div>
    );
  }
}

// The notes of the library, in the notebook: a list of them, and the one that is open, in its
// editor. A link of a note that leads to a passage is shown through the column's citing where
// the passage lies in the item on show, and by opening its item otherwise.
export function NotesPanel({
  seat,
  citing,
  onOpen,
}: {
  seat: Seat | undefined;
  citing: Citing;
  onOpen: Opener;
}) {
  const opened = useOpenedNote();
  if (opened === undefined) {
    return <NoteList seat={seat} />;
  }
  return (
    <EditorBoundary key={opened}>
      <Suspense fallback={<p className="status notes-waiting">正在载入编辑器…</p>}>
        <NoteEditor noteId={opened} seat={seat} citing={citing} onOpen={onOpen} />
      </Suspense>
    </EditorBoundary>
  );
}
