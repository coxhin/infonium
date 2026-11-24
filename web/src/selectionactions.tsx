import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type RefObject,
} from "react";
import type { Anchors } from "./client";
import { markPassage, openHighlight } from "./highlights";
import { reasonOf } from "./loading";
import type { Selected } from "./marking";
import { noteDown, showNote, type Noted } from "./notes";
import "./selectionactions.css";

// What is offered on a passage the reader has selected in a view of the reader: to mark it, to
// mark it and note something on it, or to put it into the note being written. A view hands
// the passage over through its marking, and keeps the selection until an action has used it.

// A press on an element of this class is a press on the actions, never one in the text.
export const SELECTION_ACTIONS_CLASS = "selection-actions";

// Space kept between the selection and the actions, in pixels. Where the pointer is a finger,
// the handles of the system's selection hang below the selection and its own menu stands above
// it, so the actions keep further down.
const GAP = 8;
const HANDLES_ROOM = 32;
const WINDOW_MARGIN = 8;
const FINGER = "(pointer: coarse)";

type Place = { left: number; top: number };

// Where the actions stand: below the selection's last line, else beside its end, else above
// it, and always wholly inside the window.
function placeBy(at: DOMRect, width: number, height: number): Place {
  const below = at.bottom + (matchMedia(FINGER).matches ? HANDLES_ROOM : GAP);
  const right = innerWidth - WINDOW_MARGIN - width;
  const bottom = innerHeight - WINDOW_MARGIN - height;
  const within = (value: number, highest: number) =>
    Math.max(WINDOW_MARGIN, Math.min(value, highest));
  if (below <= bottom) {
    return { left: within(at.right - width / 2, right), top: below };
  }
  if (at.right + GAP <= right) {
    return { left: at.right + GAP, top: within(at.top, bottom) };
  }
  return { left: within(at.right - width / 2, right), top: within(at.top - GAP - height, bottom) };
}

// Keeps the element inside the window by the rectangle it is to stand by, once its size is
// known, and again whenever what it says changes its size.
function usePlaced(at: DOMRect | undefined): {
  own: RefObject<HTMLDivElement | null>;
  style: CSSProperties;
} {
  const own = useRef<HTMLDivElement>(null);
  const [place, setPlace] = useState<Place>();
  useLayoutEffect(() => {
    const element = own.current;
    if (at === undefined || element === null) {
      return;
    }
    const next = placeBy(at, element.offsetWidth, element.offsetHeight);
    if (next.left !== place?.left || next.top !== place.top) {
      setPlace(next);
    }
  });
  return {
    own,
    style:
      place === undefined
        ? { left: at?.right ?? 0, top: at?.bottom ?? 0, visibility: "hidden" }
        : { left: place.left, top: place.top },
  };
}

// Puts the element away on Escape and on a press outside it, and moves the focus into it
// on Tab from outside it, so that a reader who selected with keys reaches it next.
function useAway(own: RefObject<HTMLDivElement | null>, on: boolean, away: () => void) {
  useEffect(() => {
    if (!on) {
      return;
    }
    const outside = (target: EventTarget | null) =>
      !(target instanceof Node && own.current?.contains(target) === true);
    const keyed = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        away();
      } else if (
        event.key === "Tab" &&
        !event.shiftKey &&
        !event.altKey &&
        !event.ctrlKey &&
        !event.metaKey &&
        outside(document.activeElement)
      ) {
        const first = own.current?.querySelector("button");
        if (first !== null && first !== undefined) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    const pressed = (event: PointerEvent) => {
      if (outside(event.target)) {
        away();
      }
    };
    addEventListener("keydown", keyed, true);
    addEventListener("pointerdown", pressed, true);
    return () => {
      removeEventListener("keydown", keyed, true);
      removeEventListener("pointerdown", pressed, true);
    };
  }, [on, away]);
}

// Said once an excerpt has gone into a note: which note took it, with a way to see it there.
function NotedSaying({ noted, at, onAway }: { noted: Noted; at: DOMRect; onAway: () => void }) {
  const { own, style } = usePlaced(at);
  useAway(own, true, onAway);
  return (
    <div
      ref={own}
      className={`${SELECTION_ACTIONS_CLASS} noted`}
      role="status"
      style={style}
    >
      <span>已记到笔记「{noted.title}」</span>
      <button
        onClick={() => {
          showNote(noted.noteId);
          onAway();
        }}
      >
        打开笔记
      </button>
      <button className="quiet" aria-label="关闭" onClick={onAway}>
        ×
      </button>
    </div>
  );
}

export function SelectionActions({
  itemId,
  selected,
}: {
  itemId: string;
  selected: Selected | undefined;
}) {
  // What is said and done is of one passage: a view hands the same passage over again, with
  // where it stands now, after a scroll.
  const [problem, setProblem] = useState<{ of: Anchors; reason: string }>();
  const [awayFrom, setAwayFrom] = useState<Anchors>();
  const [working, setWorking] = useState(false);
  const [noted, setNoted] = useState<{ noted: Noted; at: DOMRect }>();
  const anchors = selected?.anchors;
  const shown = selected !== undefined && awayFrom !== anchors;
  const { own, style } = usePlaced(shown ? selected.at : undefined);
  const away = useCallback(() => setAwayFrom(anchors), [anchors]);
  useAway(own, shown, away);
  useEffect(() => {
    if (selected !== undefined) {
      setNoted(undefined);
    }
  }, [anchors]);

  if (!shown) {
    return noted === undefined ? null : (
      <NotedSaying noted={noted.noted} at={noted.at} onAway={() => setNoted(undefined)} />
    );
  }
  const { anchors: chosen, at, clear } = selected;

  // Each action uses the selection and lets it go only once it has done what it does; one
  // that fails says why and leaves the selection as it is.
  async function act(doing: () => Promise<void>, failed: string) {
    if (working) {
      return;
    }
    setWorking(true);
    setProblem(undefined);
    try {
      await doing();
      clear();
    } catch (error) {
      setProblem({ of: chosen, reason: `${failed}：${reasonOf(error)}` });
    } finally {
      setWorking(false);
    }
  }

  const passage = { anchors: chosen };
  const mark = () =>
    act(async () => {
      await markPassage(itemId, passage, "");
    }, "没有标上");
  const comment = () =>
    act(async () => {
      const made = await markPassage(itemId, passage, "");
      openHighlight({ itemId, highlightId: made.id, commenting: true });
    }, "没有标上");
  const note = () =>
    act(async () => {
      const done = await noteDown({ kind: "quote", cited: { anchors: chosen, itemId } });
      setNoted({ noted: done, at });
    }, "没有记到笔记");

  return (
    <div
      ref={own}
      className={SELECTION_ACTIONS_CLASS}
      role="toolbar"
      aria-label="对选中的文字"
      aria-busy={working}
      style={style}
      // A press on the actions is not a press in the text, which would let the selection go.
      onMouseDown={(event) => event.preventDefault()}
    >
      <button onClick={() => void mark()} title="把选中的文字标为高亮">
        高亮
      </button>
      <button onClick={() => void comment()} title="标为高亮，并在笔记本里写批注">
        批注
      </button>
      <button onClick={() => void note()} title="把选中的文字摘进正在写的笔记">
        记到笔记
      </button>
      {problem !== undefined && problem.of === chosen && (
        <span className="status failed" role="alert">
          {problem.reason}
        </span>
      )}
    </div>
  );
}
