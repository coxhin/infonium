import { useEffect, useState } from "react";
import { markPassage } from "./highlights";
import { reasonOf } from "./loading";
import type { Selected } from "./marking";
import "./selectionactions.css";

// What is offered on a passage the reader has selected in a view of the reader, where the
// selection ends: to mark it as a highlight. A view hands the passage over through its marking.
export function SelectionActions({
  itemId,
  selected,
}: {
  itemId: string;
  selected: Selected | undefined;
}) {
  const [problem, setProblem] = useState<string>();
  useEffect(() => setProblem(undefined), [selected]);
  if (selected === undefined) {
    return null;
  }
  const { anchors, at, clear } = selected;

  async function mark() {
    try {
      await markPassage(itemId, { anchors }, "");
      clear();
    } catch (error) {
      setProblem(`没有标上：${reasonOf(error)}`);
    }
  }

  return (
    <div
      className="selection-actions"
      role="toolbar"
      aria-label="对选中的文字"
      style={{ left: at.right, top: at.bottom }}
      // A press on the actions is not a press in the text, which would let the selection go.
      onMouseDown={(event) => event.preventDefault()}
    >
      <button onClick={() => void mark()}>高亮</button>
      {problem !== undefined && <span className="status failed">{problem}</span>}
    </div>
  );
}
