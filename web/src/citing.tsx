import { useEffect, useState } from "react";
import type { Anchor } from "./client";
import { reasonOf } from "./loading";
import type { Seat } from "./seat";
import "./citing.css";

// How citations reach the source: showing an anchor's passage there, which fails when the
// source no longer holds it, and taking the passage off show again.
export type Pointer = { show: (anchor: Anchor) => void; hide: () => void };

// What the column last pointed at. A passage pointed at, or lost, belongs to the life of the
// reader it was pointed at in. A passage that opens another item waits for that item's reader.
type Pointing =
  | { state: "nowhere" }
  | { state: "shown"; anchor: Anchor; life: number | undefined }
  | { state: "lost"; reason: string; life: number | undefined }
  | { state: "awaiting"; anchor: Anchor; itemId: string };

const NOWHERE: Pointing = { state: "nowhere" };

// The one way the column points at passages, shared by the digest and the chat, so that one
// chip at most is pressed and one reason at most is said.
export type Citing = {
  point: (anchor: Anchor) => void;
  // The passage is shown by opening its item at it; its chip is pressed once the item's
  // reader is on show.
  awaitOpening: (anchor: Anchor, itemId: string) => void;
  fail: (reason: string) => void;
  hide: () => void;
  isPressed: (anchor: Anchor) => boolean;
  lost: string | undefined;
};

// A pointing that outlives its reader is dropped when another reader comes, except the one
// that waits for the reader that comes.
function carriedOver(pointing: Pointing, seat: Seat | undefined): Pointing {
  switch (pointing.state) {
    case "nowhere":
      return pointing;
    case "awaiting":
      if (seat === undefined) {
        return pointing;
      }
      return seat.itemId === pointing.itemId
        ? { state: "shown", anchor: pointing.anchor, life: seat.life }
        : NOWHERE;
    case "shown":
    case "lost":
      return pointing.life === seat?.life ? pointing : NOWHERE;
  }
}

export function useCiting(pointer: Pointer, seat: Seat | undefined): Citing {
  const [pointing, setPointing] = useState<Pointing>(NOWHERE);
  const life = seat?.life;
  useEffect(() => setPointing((held) => carriedOver(held, seat)), [life]);
  const current = carriedOver(pointing, seat);

  return {
    point(anchor) {
      try {
        pointer.show(anchor);
        setPointing({ state: "shown", anchor, life });
      } catch (error) {
        pointer.hide();
        setPointing({ state: "lost", reason: reasonOf(error), life });
      }
    },
    awaitOpening(anchor, itemId) {
      setPointing({ state: "awaiting", anchor, itemId });
    },
    fail(reason) {
      setPointing({ state: "lost", reason, life });
    },
    hide() {
      pointer.hide();
      setPointing(NOWHERE);
    },
    isPressed(anchor) {
      return current.state === "shown" && current.anchor === anchor;
    },
    lost: current.state === "lost" ? current.reason : undefined,
  };
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
