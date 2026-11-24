import { memo, useSyncExternalStore } from "react";
import { percent, type Box } from "./areas";
import type { Marked } from "./marking";
import type { PageBox, PageText, Span } from "./pagelines";
import "./pagemarking.css";

// The reader's highlights and a growing selection as the pages of a PDF draw them.

// How a highlight is drawn: plainly, with a mark that it holds a comment, or apart as the one
// open in the notebook.
export type MarkState = "plain" | "commented" | "opened";

const STATE_WEIGHT: Record<MarkState, number> = { plain: 0, commented: 1, opened: 2 };

function stateOf(marked: Marked, opened: string | undefined): MarkState {
  return marked.id === opened ? "opened" : marked.commented ? "commented" : "plain";
}

// A highlight as it is drawn on one page: the spans of its blocks, and what tells two
// drawings of it apart, its state and its anchors' ranges.
export type PageMark = { key: string; id: string; state: MarkState; spans: Span[] };

// The highlights on each page, in the order of the document.
export function pageMarksOf(
  text: PageText,
  marked: Marked[],
  opened: string | undefined,
): Map<number, PageMark[]> {
  const onPages = new Map<number, PageMark[]>();
  for (const each of marked) {
    const state = stateOf(each, opened);
    const ranges = each.anchors.map(
      (anchor) => `${anchor.block_number}:${anchor.start}-${anchor.end}`,
    );
    const key = `${each.id}|${state}|${ranges.join(",")}`;
    // Every page of the blocks it runs through: where their lines break is read again as their
    // pages are drawn, and a stretch at a break may then fall on the next page.
    const pages = new Set(
      each.anchors.flatMap((anchor) =>
        (text.ofBlocks.get(anchor.block_number) ?? []).map((line) => line.place.page),
      ),
    );
    const spans = each.anchors.map((anchor) => ({
      blockNumber: anchor.block_number,
      start: anchor.start,
      end: anchor.end,
    }));
    for (const page of pages) {
      onPages.set(page, [...(onPages.get(page) ?? []), { key, id: each.id, state, spans }]);
    }
  }
  return onPages;
}

// The blocks the highlights run through, each in the strongest state of those that do, for
// the areas of a translation set over the pages.
export function markedBlocksOf(
  marked: Marked[],
  opened: string | undefined,
): Map<number, MarkState> {
  const blocks = new Map<number, MarkState>();
  for (const each of marked) {
    const state = stateOf(each, opened);
    for (const anchor of each.anchors) {
      const before = blocks.get(anchor.block_number);
      if (before === undefined || STATE_WEIGHT[state] > STATE_WEIGHT[before]) {
        blocks.set(anchor.block_number, state);
      }
    }
  }
  return blocks;
}

function boxStyle(box: Box) {
  return {
    left: percent(box.left),
    top: percent(box.top),
    width: percent(box.width),
    height: percent(box.height),
  };
}

const NO_BOXES: Box[] = [];

function sameBoxes(one: Box[], other: Box[]): boolean {
  return (
    one.length === other.length &&
    one.every((box, index) => {
      const twin = other[index];
      return (
        twin !== undefined &&
        box.left === twin.left &&
        box.top === twin.top &&
        box.width === twin.width &&
        box.height === twin.height
      );
    })
  );
}

function sameMarks(one: PageMark[], other: PageMark[]): boolean {
  return one.length === other.length && one.every((mark, index) => mark.key === other[index]?.key);
}

// The selection growing on the pages, kept outside React's state so that a move of the pointer
// draws again only the pages, and the areas, whose part of it changed.
export class SelectionDrawing {
  private onPages = new Map<number, Box[]>();
  private blocks: ReadonlySet<number> = new Set();
  private listeners = new Set<() => void>();

  readonly subscribe = (notify: () => void): (() => void) => {
    this.listeners.add(notify);
    return () => {
      this.listeners.delete(notify);
    };
  };

  boxesOn(page: number): Box[] {
    return this.onPages.get(page) ?? NO_BOXES;
  }

  holds(blockNumber: number): boolean {
    return this.blocks.has(blockNumber);
  }

  // Shows the boxes given, and the blocks whose areas are drawn as selected. A page whose boxes
  // are as they were keeps them, so that nothing of it is drawn again.
  show(boxes: PageBox[], blocks: number[]): void {
    const grouped = new Map<number, Box[]>();
    for (const { page, box } of boxes) {
      grouped.set(page, [...(grouped.get(page) ?? []), box]);
    }
    const next = new Map<number, Box[]>();
    grouped.forEach((pageBoxes, page) => {
      const before = this.onPages.get(page);
      next.set(page, before !== undefined && sameBoxes(before, pageBoxes) ? before : pageBoxes);
    });
    const nextBlocks = new Set(blocks);
    const sameBlocks =
      nextBlocks.size === this.blocks.size &&
      [...nextBlocks].every((block) => this.blocks.has(block));
    this.onPages = next;
    if (!sameBlocks) {
      this.blocks = nextBlocks;
    }
    this.listeners.forEach((notify) => notify());
  }

  clear(): void {
    this.show([], []);
  }
}

// The part of a selection on one page.
export function SelectionLayer({
  drawing,
  page,
  under,
}: {
  drawing: SelectionDrawing;
  page: number;
  under: boolean;
}) {
  const boxes = useSyncExternalStore(drawing.subscribe, () => drawing.boxesOn(page));
  if (boxes.length === 0) {
    return null;
  }
  return (
    <div className={under ? "page-selection under" : "page-selection"}>
      {boxes.map((box, index) => (
        <div key={index} style={boxStyle(box)} />
      ))}
    </div>
  );
}

const MarkBoxes = memo(
  function MarkBoxes({
    mark,
    text,
    page,
  }: {
    mark: PageMark;
    text: PageText;
    page: number;
    // How many drawings of the page have ended, so that its lines are cut anew from the next.
    drawings: number;
  }) {
    return mark.spans
      .flatMap((span) => text.coveredBoxes(span))
      .filter((covered) => covered.page === page)
      .map(({ box }, index) => (
        <div
          key={index}
          className="page-mark"
          data-state={mark.state}
          data-highlight={mark.id}
          style={boxStyle(box)}
        />
      ));
  },
  (before, after) =>
    before.mark.key === after.mark.key &&
    before.text === after.text &&
    before.page === after.page &&
    before.drawings === after.drawings,
);

// The reader's highlights on one page, each drawn again only when it changes or the page is
// drawn anew.
export const MarksLayer = memo(
  function MarksLayer({
    marks,
    text,
    page,
    drawings,
    under,
  }: {
    marks: PageMark[];
    text: PageText;
    page: number;
    drawings: number;
    under: boolean;
  }) {
    if (marks.length === 0) {
      return null;
    }
    return (
      <div className={under ? "page-marks under" : "page-marks"}>
        {marks.map((mark) => (
          <MarkBoxes key={mark.key} mark={mark} text={text} page={page} drawings={drawings} />
        ))}
      </div>
    );
  },
  (before, after) =>
    before.under === after.under &&
    before.text === after.text &&
    before.page === after.page &&
    before.drawings === after.drawings &&
    sameMarks(before.marks, after.marks),
);

export function useSelectedBlock(drawing: SelectionDrawing, blockNumber: number): boolean {
  return useSyncExternalStore(drawing.subscribe, () => drawing.holds(blockNumber));
}
