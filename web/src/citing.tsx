import { useState } from "react";
import type { Anchor } from "./client";
import { reasonOf } from "./loading";
import "./citing.css";

// How citations reach the source: showing an anchor's passage there, which fails when the
// source no longer holds it, and taking the passage off show again.
export type Pointer = { show: (anchor: Anchor) => void; hide: () => void };

// What the column last pointed at. A passage stands for the document it was pointed at in, so
// that what was pointed at in one document shows as nothing in another.
type Pointing =
  | { state: "nowhere" }
  | { state: "shown"; anchor: Anchor }
  | { state: "lost"; reason: string; document: string | undefined };

// The one way the column points at passages, shared by the digest and the chat, so that one
// chip at most is pressed and one reason at most is said.
export type Citing = {
  point: (anchor: Anchor) => void;
  // The passage is shown by opening another item at it; the chip is pressed once it is there.
  pointedElsewhere: (anchor: Anchor) => void;
  fail: (reason: string) => void;
  isPressed: (anchor: Anchor) => boolean;
  lost: string | undefined;
};

export function useCiting(pointer: Pointer, documentId: string | undefined): Citing {
  const [pointing, setPointing] = useState<Pointing>({ state: "nowhere" });
  return {
    point(anchor) {
      try {
        pointer.show(anchor);
        setPointing({ state: "shown", anchor });
      } catch (error) {
        pointer.hide();
        setPointing({ state: "lost", reason: reasonOf(error), document: documentId });
      }
    },
    pointedElsewhere(anchor) {
      setPointing({ state: "shown", anchor });
    },
    fail(reason) {
      setPointing({ state: "lost", reason, document: documentId });
    },
    isPressed(anchor) {
      return (
        pointing.state === "shown" &&
        pointing.anchor === anchor &&
        anchor.document_id === documentId
      );
    },
    lost:
      pointing.state === "lost" && pointing.document === documentId ? pointing.reason : undefined,
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
