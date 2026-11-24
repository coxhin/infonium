import { useEffect, useState } from "react";
import type { Anchor, Anchors, Statement } from "./client";
import { reasonOf } from "./loading";
import type { Seat, Unshown } from "./seat";
import "./citing.css";

// How citations reach the source: showing a passage there, by its anchors, which fails at once
// when the source does not hold it, and taking the passage off show again.
export type Pointer = { show: (anchors: Anchors) => void; hide: () => void };

// Said for a passage of an item that was read again since it was cited: the item now shows
// another document, which does not hold it.
const READ_AGAIN = "这一篇后来重新读取过，这段引文出自之前读到的版本，现在找不到了。";

// What the column last pointed at. A passage pointed at, or lost, belongs to the life of the
// reader it was pointed at in. A passage that opens an item waits for that item's reader, and
// is shown through it once it is on show; it is given up when the reader selects an item that
// is neither the one it was pressed in nor the one it opens.
type Pointing =
  | { state: "nowhere" }
  | { state: "shown"; anchors: Anchors; item: string | undefined; life: number | undefined }
  | { state: "lost"; reason: string; item: string | undefined; life: number | undefined }
  | { state: "awaiting"; anchors: Anchors; itemId: string; from: string | undefined };

const NOWHERE: Pointing = { state: "nowhere" };

// The one way the column points at passages, shared by the digest, the chat and the notebook,
// so that one passage at most is pressed and one reason at most is said. A passage is given by
// its anchors: a citation's one, or one for each block a marked passage runs through.
export type Citing = {
  point: (anchors: Anchors) => void;
  // The passage is shown by opening its item at it; it is pressed once the item's reader is
  // on show and shows it.
  awaitOpening: (anchors: Anchors, itemId: string) => void;
  fail: (reason: string) => void;
  hide: () => void;
  // Whether the passage on show begins with this very anchor.
  isPressed: (anchor: Anchor) => boolean;
  lost: string | undefined;
};

// What is on show of a pointing: nothing of one that belongs to an item or a reader gone.
function onShow(
  pointing: Pointing,
  selected: string | undefined,
  life: number | undefined,
): Pointing {
  switch (pointing.state) {
    case "nowhere":
    case "awaiting":
      return pointing;
    case "shown":
    case "lost":
      return pointing.item === selected && pointing.life === life ? pointing : NOWHERE;
  }
}

export function useCiting(
  pointer: Pointer,
  selected: string | undefined,
  seat: Seat | undefined,
  unshown: Unshown | undefined,
): Citing {
  const [pointing, setPointing] = useState<Pointing>(NOWHERE);
  const life = seat?.life;

  function point(anchors: Anchors) {
    if (seat !== undefined && anchors[0].document_id !== seat.documentId) {
      pointer.hide();
      setPointing({ state: "lost", reason: READ_AGAIN, item: selected, life });
      return;
    }
    try {
      pointer.show(anchors);
      setPointing({ state: "shown", anchors, item: selected, life });
    } catch (error) {
      pointer.hide();
      setPointing({ state: "lost", reason: reasonOf(error), item: selected, life });
    }
  }

  // A passage that waits for its item is shown once the item's reader is on show, or said to
  // be lost when the item cannot be shown; it is dropped when the reader turns to another
  // item. What was pointed at in a reader that has gone is dropped.
  useEffect(() => {
    if (pointing.state === "awaiting") {
      if (selected !== pointing.itemId && selected !== pointing.from) {
        setPointing(NOWHERE);
      } else if (seat?.itemId === pointing.itemId) {
        point(pointing.anchors);
      } else if (seat === undefined && unshown?.itemId === pointing.itemId) {
        setPointing({ state: "lost", reason: unshown.reason, item: selected, life: undefined });
      }
    } else if (onShow(pointing, selected, life) !== pointing) {
      setPointing(NOWHERE);
    }
  }, [selected, life, pointing, unshown]);

  const current = onShow(pointing, selected, life);
  return {
    point,
    awaitOpening(anchors, itemId) {
      setPointing({ state: "awaiting", anchors, itemId, from: selected });
    },
    fail(reason) {
      setPointing({ state: "lost", reason, item: selected, life });
    },
    hide() {
      pointer.hide();
      setPointing(NOWHERE);
    },
    isPressed(anchor) {
      return current.state === "shown" && current.anchors[0] === anchor;
    },
    lost: current.state === "lost" ? current.reason : undefined,
  };
}

// What a statement's chips do not tell: that citations the model gave it were not found in
// their sources. A statement whose every citation was lost is a claim whose sources are not
// there, marked plainly. One that kept some says quietly how many more it was given. One that
// has none and lost none is marked only where every statement is meant to cite, as in a
// digest; an answer may rightly say what its sources do not cover. That mark does not say that
// the model gave no citation: a statement kept before the count of lost citations was kept
// reads as having lost none.
export function LostCitations({
  statement,
  citesAll,
}: {
  statement: Statement;
  citesAll: boolean;
}) {
  const lost = statement.lost_citations;
  if (statement.anchors.length === 0 && lost > 0) {
    return (
      <span className="unsourced unfound" title={`模型给出的 ${lost} 处引文在原文里都找不到`}>
        出处找不到
      </span>
    );
  }
  if (statement.anchors.length === 0 && citesAll) {
    return (
      <span className="unsourced" title="这句话没有可以打开的出处">
        无出处
      </span>
    );
  }
  if (lost > 0) {
    return (
      <span className="also-lost" title={`模型还给了 ${lost} 处引文，在原文里找不到`}>
        另 {lost} 处未找到
      </span>
    );
  }
  return null;
}

// A statement's citations as numbered chips, each titled with its quote.
export function Citations({
  anchors,
  citing,
  titleOf,
  onPoint,
}: {
  anchors: Anchor[];
  citing: Citing;
  titleOf: (anchor: Anchor) => string;
  onPoint: (anchor: Anchor) => void;
}) {
  return anchors.map((anchor, anchorIndex) => (
    <button
      key={anchorIndex}
      className="citation"
      title={titleOf(anchor)}
      aria-pressed={citing.isPressed(anchor)}
      onClick={() => onPoint(anchor)}
    >
      {anchorIndex + 1}
    </button>
  ));
}
