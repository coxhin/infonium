import { useLayoutEffect, useRef, useState } from "react";
import type { Opener } from "./chat";
import type { Citing } from "./citing";
import { ItemHighlights, LibraryHighlights } from "./highlightlists";
import { useOpenedHighlight } from "./highlights";
import { useShownNote } from "./notes";
import { NotesPanel } from "./notespanel";
import type { Seat } from "./seat";
import "./notebook.css";

type Part = "highlights" | "notes";
const PART_NAMES: Record<Part, string> = { highlights: "高亮", notes: "笔记" };

// Runs when the value becomes another one, not when the component is made with it.
function useChanged<Value>(value: Value, changed: (value: Value) => void): void {
  const seen = useRef(value);
  useLayoutEffect(() => {
    if (seen.current !== value) {
      seen.current = value;
      changed(value);
    }
  }, [value]);
}

// What the column asks of the app when the notebook is asked for from elsewhere: a highlight
// opened from the source, a note asked to be shown. The column comes on show with the notebook.
export function useNotebookAsked(onAsked: () => void): void {
  const opened = useOpenedHighlight();
  const shownNote = useShownNote();
  useChanged(opened, (next) => next !== undefined && onAsked());
  useChanged(shownNote, (next) => next !== undefined && onAsked());
}

// The notebook beside the reader: the reader's highlights, of the item on show or of every
// item, and the notes. It lives as long as the column, so the part chosen stays while items
// come and go.
export function NotebookPanel({
  selected,
  seat,
  shown,
  citing,
  onOpen,
}: {
  selected: string | undefined;
  seat: Seat | undefined;
  shown: boolean;
  citing: Citing;
  onOpen: Opener;
}) {
  const [part, setPart] = useState<Part>("highlights");
  const opened = useOpenedHighlight();
  const shownNote = useShownNote();
  useChanged(opened, (next) => next !== undefined && setPart("highlights"));
  useChanged(shownNote, (next) => next !== undefined && setPart("notes"));
  const highlightsShown = shown && part === "highlights";

  return (
    <div className="notebook">
      <div className="parts" role="group" aria-label="笔记本的内容">
        {(Object.keys(PART_NAMES) as Part[]).map((name) => (
          <button key={name} aria-pressed={part === name} onClick={() => setPart(name)}>
            {PART_NAMES[name]}
          </button>
        ))}
      </div>
      <div className="notebook-part" hidden={part !== "highlights"}>
        {selected === undefined ? (
          <LibraryHighlights citing={citing} shown={highlightsShown} onOpen={onOpen} />
        ) : (
          <ItemHighlights
            key={selected}
            itemId={selected}
            seat={seat}
            citing={citing}
            shown={highlightsShown}
          />
        )}
      </div>
      <div className="notebook-part" hidden={part !== "notes"}>
        <NotesPanel seat={seat} citing={citing} onOpen={onOpen} />
      </div>
    </div>
  );
}
