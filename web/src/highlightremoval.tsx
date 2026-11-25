import { useLayoutEffect, useRef, useState, useSyncExternalStore } from "react";
import { requestReason, type Highlight } from "./client";
import { unmark } from "./highlights";

// Why the service refused to remove a highlight, by highlight. The entry leaves the list as
// soon as its removal is asked for, and is drawn anew when the refusal puts it back, so the
// reason is kept apart from it.
const refusals = new Map<string, string>();
const refusalListeners = new Set<() => void>();

function refuse(highlightId: string, reason: string | undefined): void {
  if (reason === undefined) {
    refusals.delete(highlightId);
  } else {
    refusals.set(highlightId, reason);
  }
  refusalListeners.forEach((notify) => notify());
}

function onRefusal(notify: () => void): () => void {
  refusalListeners.add(notify);
  return () => {
    refusalListeners.delete(notify);
  };
}

type Removing = "idle" | "confirming";

// Removing a highlight takes its comment with it, so it is asked for twice. Removed says that
// the entry has gone from the list, for the keyboard to be put where the reader goes on.
export function RemoveControl({
  highlight,
  onRemoved,
}: {
  highlight: Highlight;
  onRemoved: () => void;
}) {
  const [removing, setRemoving] = useState<Removing>("idle");
  const problem = useSyncExternalStore(onRefusal, () => refusals.get(highlight.id));
  const removeButton = useRef<HTMLButtonElement>(null);
  const cancelled = useRef(false);

  useLayoutEffect(() => {
    if (cancelled.current && removing === "idle") {
      cancelled.current = false;
      removeButton.current?.focus();
    }
  }, [removing]);

  function cancel() {
    cancelled.current = true;
    setRemoving("idle");
  }

  async function confirm() {
    refuse(highlight.id, undefined);
    onRemoved();
    try {
      await unmark(highlight.item, highlight.id);
    } catch (error) {
      refuse(highlight.id, `没有移除：${requestReason(error)}`);
    }
  }

  return (
    <>
      {removing === "idle" && (
        <button
          ref={removeButton}
          className="quiet"
          aria-label="移除这条高亮"
          onClick={() => {
            refuse(highlight.id, undefined);
            setRemoving("confirming");
          }}
        >
          移除
        </button>
      )}
      {removing === "confirming" && (
        <span
          className="confirm"
          role="group"
          aria-label="确认移除高亮"
          onKeyDown={(event) => event.key === "Escape" && cancel()}
        >
          移除这条高亮{highlight.comment === "" ? "" : "和它的批注"}？
          <button className="danger" onClick={() => void confirm()}>
            移除
          </button>
          <button autoFocus onClick={cancel}>
            取消
          </button>
        </span>
      )}
      {problem !== undefined && (
        <span className="status failed" role="alert">
          {problem}
        </span>
      )}
    </>
  );
}
