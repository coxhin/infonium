import type { Anchors } from "./client";

// The seam between the reader's views and the reader's highlights: what a view is told to draw,
// and how it hands over a passage the reader selects in it.

// A highlight that stands in the document on show: where its passage lies there, an anchor for
// each block it runs through, and whether the reader noted something on it.
export type Marked = { id: string; anchors: Anchors; commented: boolean };

// A passage the reader has selected in a view, of the document on show: its anchors, each with
// the words of its block that it covers; where on the screen the selection ends, in the
// window's coordinates, for what is offered on it to stand by; and how the view takes the
// selection off show again.
export type Selected = { anchors: Anchors; at: DOMRect; clear: () => void };

export type Marking = {
  // The highlights to draw, in the order of the document.
  marked: Marked[];
  // The highlight that is open in the notebook, which a view draws apart from the others.
  opened: string | undefined;
  // A view says that the reader has selected a passage in it, or that the selection is gone.
  onSelect: (selected: Selected | undefined) => void;
  // A view says that the reader pressed a highlight it draws.
  onPress: (highlightId: string) => void;
};

// What a view is told where nothing can be marked, as in an earlier revision of a source.
export const NO_MARKING: Marking = {
  marked: [],
  opened: undefined,
  onSelect: () => undefined,
  onPress: () => undefined,
};
