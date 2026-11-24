import { memo, useSyncExternalStore } from "react";
import type { Box } from "./areas";
import type { PageBox, PageMark } from "./pagemarks";
import "./pagemarks.css";

function percent(fraction: number): string {
  return `${fraction * 100}%`;
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

function sameMarks(one: PageMark[], other: PageMark[]): boolean {
  return one.length === other.length && one.every((mark, index) => mark.key === other[index]?.key);
}

const MarkBoxes = memo(function MarkBoxes({ mark }: { mark: PageMark }) {
  return mark.boxes.map((box, index) => (
    <div
      key={index}
      className="page-mark"
      data-state={mark.state}
      data-highlight={mark.id}
      style={boxStyle(box)}
    />
  ));
});

// The reader's highlights on one page, each drawn again only when it changes.
export const MarksLayer = memo(
  function MarksLayer({ marks, under }: { marks: PageMark[]; under: boolean }) {
    if (marks.length === 0) {
      return null;
    }
    return (
      <div className={under ? "page-marks under" : "page-marks"}>
        {marks.map((mark) => (
          <MarkBoxes key={mark.key} mark={mark} />
        ))}
      </div>
    );
  },
  (before, after) => before.under === after.under && sameMarks(before.marks, after.marks),
);

export function useSelectedBlock(
  drawing: SelectionDrawing | undefined,
  blockNumber: number,
): boolean {
  return useSyncExternalStore(
    drawing?.subscribe ?? noSubscription,
    () => drawing?.holds(blockNumber) ?? false,
  );
}

function noSubscription(): () => void {
  return () => undefined;
}
