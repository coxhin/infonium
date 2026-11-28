import type { DragEvent } from "react";

// What a drag in this page carries: a subscription or a hand-added item, and the folder it is
// in. The browser lets a drag's data be read only where it is dropped, so the page also keeps
// what it is dragging, for the places it passes over to tell whether they take it.
export type Dragged =
  | { kind: "subscription"; id: string; folder: string | null }
  | { kind: "item"; id: string; folder: string | null };

const DRAGGED_TYPE = "application/x-infonium-placing";

let dragged: Dragged | undefined;

export function startDrag(event: DragEvent, what: Dragged): void {
  dragged = what;
  event.dataTransfer.setData(DRAGGED_TYPE, what.id);
  event.dataTransfer.effectAllowed = "all";
}

export function endDrag(): void {
  dragged = undefined;
}

// What a drag over a place carries, where it is one of this page's.
export function draggedOver(event: DragEvent): Dragged | undefined {
  return event.dataTransfer.types.includes(DRAGGED_TYPE) ? dragged : undefined;
}
