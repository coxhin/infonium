import { useEffect, useRef, useState } from "react";
import {
  anchorsOf,
  readRevisions,
  readStanding,
  type Anchor,
  type Anchors,
  type Standing,
  type Statement,
} from "./client";
import { itemLink, linkNow, revisionLink, useLink } from "./links";
import { reasonOf } from "./loading";
import { revisionTold } from "./revisionwords";
import type { Seat, Unshown } from "./seat";
import "./citing.css";

// How citations reach the source: showing a passage there, by its anchors, which fails at once
// when the source does not hold it, and taking the passage off show again.
export type Pointer = { show: (anchors: Anchors) => void; hide: () => void };

// Said while the service is asked where a passage of another revision stands now.
const ASKING = "这段文字出自这一篇的另一次读取，正在查找它在现在的版本里的位置…";
// Said, quietly, while a passage of another revision is shown where it stands now in words
// that differ a little from those it was cited or marked in.
const CHANGED_WORDS = "这段文字出自这一篇的另一次读取，现在的版本里措辞略有不同，显示的是现在的样子。";
const UNNUMBERED = "这一篇较早的一次读取";

// How the column offers to show a passage that the document the item holds now has lost: in
// the revision it is of.
export type Opening = { label: string; open: () => void };

// What the column last pointed at. A passage pointed at, asked about, or lost belongs to the
// life of the reader it was pointed at in. A passage that opens an item waits for that item's
// reader, opened by the link the window is at, and is shown through it once it is on show;
// where it is known already where the passage stands there, it is shown there. It is given up
// when the reader selects an item that is neither the one it was pressed in nor the one it
// opens.
type Pointing =
  | { state: "nowhere" }
  | {
      state: "shown";
      anchors: Anchors;
      said: string | undefined;
      item: string | undefined;
      life: number | undefined;
    }
  | { state: "asking"; anchors: Anchors; item: string | undefined; life: number | undefined }
  | {
      state: "lost";
      reason: string;
      opening: Opening | undefined;
      item: string | undefined;
      life: number | undefined;
    }
  | {
      state: "awaiting";
      anchors: Anchors;
      at: Anchors | undefined;
      said: string | undefined;
      itemId: string;
      from: string | undefined;
    };

const NOWHERE: Pointing = { state: "nowhere" };

// The one way the column points at passages, shared by the digest, the chat and the notebook,
// so that one passage at most is pressed and one reason at most is said. A passage is given by
// its anchors: a citation's one, or one for each block a marked passage runs through. A
// passage of the document on show is shown in it; a passage of another revision of the item is
// shown where it stands in the document the item holds now, once the service has said where.
export type Citing = {
  point: (anchors: Anchors) => void;
  // The passage is shown by opening its item at it; it is pressed once the item's reader is
  // on show and shows it.
  awaitOpening: (anchors: Anchors, itemId: string) => void;
  fail: (reason: string) => void;
  hide: () => void;
  // Whether the passage on show begins with this anchor: one of the same place in the same
  // document, since a panel drawn again while the reader returns to what the item holds now
  // holds the same citations in other objects.
  isPressed: (anchor: Anchor) => boolean;
  // Whether the passage being asked about begins with this anchor.
  isAsking: (anchor: Anchor) => boolean;
  lost: string | undefined;
  // Beside a lost passage, the way to show it in the revision it is of, where there is one.
  opening: Opening | undefined;
  // What is said quietly of the passage on show or asked about.
  said: string | undefined;
};

// Where a passage of another revision stands in the document the item holds now, as the
// column tells it: found at these anchors, lost from a revision named so, or not known.
type Answer =
  | { kind: "found"; at: Anchors; exact: boolean }
  | { kind: "lost"; told: string; revision: number | undefined }
  | { kind: "failed"; reason: string };

// The revision a lost passage is of, as a sentence names it, and its number where the
// service tells it.
async function lostFrom(itemId: string, anchors: Anchors): Promise<Answer> {
  try {
    const { revisions } = await readRevisions(itemId);
    const revision = revisions.find((held) => held.document === anchors[0].document_id);
    return revision === undefined
      ? { kind: "lost", told: UNNUMBERED, revision: undefined }
      : { kind: "lost", told: revisionTold(revision), revision: revision.number };
  } catch (error) {
    return {
      kind: "lost",
      told: `${UNNUMBERED}（读不到是第几次：${reasonOf(error)}）`,
      revision: undefined,
    };
  }
}

async function answerOf(itemId: string, anchors: Anchors): Promise<Answer> {
  let standing: Standing;
  try {
    standing = await readStanding(itemId, { anchors });
  } catch (error) {
    return { kind: "failed", reason: reasonOf(error) };
  }
  return standing.kind === "found"
    ? { kind: "found", at: anchorsOf(standing.passage), exact: standing.exact }
    : lostFrom(itemId, anchors);
}

function samePassage(one: Anchors, other: Anchors): boolean {
  return (
    one.length === other.length &&
    one.every(
      (anchor, index) =>
        anchor.document_id === other[index]?.document_id &&
        anchor.block_number === other[index].block_number &&
        anchor.start === other[index].start &&
        anchor.end === other[index].end,
    )
  );
}

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
    case "asking":
    case "lost":
      return pointing.item === selected && pointing.life === life ? pointing : NOWHERE;
  }
}

function saidOf(pointing: Pointing): string | undefined {
  switch (pointing.state) {
    case "shown":
      return pointing.said;
    case "asking":
      return ASKING;
    default:
      return undefined;
  }
}

export function useCiting(
  pointer: Pointer,
  selected: string | undefined,
  seat: Seat | undefined,
  unshown: Unshown | undefined,
): Citing {
  const [pointing, setPointing] = useState<Pointing>(NOWHERE);
  // The pointing as last set, for an answer that arrives later to see whether it is still the
  // one awaited.
  const latest = useRef<Pointing>(NOWHERE);
  const life = seat?.life;
  const link = useLink();

  function settle(next: Pointing) {
    latest.current = next;
    setPointing(next);
  }

  function show(anchors: Anchors, at: Anchors, said: string | undefined) {
    try {
      pointer.show(at);
      settle({ state: "shown", anchors, said, item: selected, life });
    } catch (error) {
      pointer.hide();
      settle({ state: "lost", reason: reasonOf(error), opening: undefined, item: selected, life });
    }
  }

  function awaitOpening(anchors: Anchors, itemId: string) {
    settle({ state: "awaiting", anchors, at: undefined, said: undefined, itemId, from: selected });
  }

  // An earlier revision is on show: the reader returns to what the item holds now, and shows
  // the passage there once it is on show.
  function returnTo(on: Seat, anchors: Anchors, at: Anchors, said: string | undefined) {
    settle({ state: "awaiting", anchors, at, said, itemId: on.itemId, from: selected });
    location.hash = itemLink(on.itemId);
  }

  function openingOf(itemId: string, anchors: Anchors, revision: number): Opening {
    return {
      label: `打开第 ${revision} 次读取的版本，看这段文字`,
      open() {
        awaitOpening(anchors, itemId);
        location.hash = revisionLink(itemId, revision);
      },
    };
  }

  function ask(on: Seat, anchors: Anchors) {
    pointer.hide();
    const asking: Pointing = { state: "asking", anchors, item: selected, life };
    settle(asking);
    void answerOf(on.itemId, anchors).then((answer) => {
      if (latest.current !== asking) {
        return;
      }
      switch (answer.kind) {
        case "found": {
          const said = answer.exact ? undefined : CHANGED_WORDS;
          if (on.documentId === on.holding) {
            show(anchors, answer.at, said);
          } else {
            returnTo(on, anchors, answer.at, said);
          }
          return;
        }
        case "lost":
          settle({
            state: "lost",
            reason: `这段文字出自${answer.told}的版本，现在这一篇里找不到了。`,
            opening:
              answer.revision === undefined
                ? undefined
                : openingOf(on.itemId, anchors, answer.revision),
            item: selected,
            life,
          });
          return;
        case "failed":
          settle({
            state: "lost",
            reason: `查不到这段文字在现在的版本里的位置：${answer.reason}`,
            opening: undefined,
            item: selected,
            life,
          });
          return;
      }
    });
  }

  function point(anchors: Anchors) {
    const documentId = anchors[0].document_id;
    if (seat === undefined || documentId === seat.documentId) {
      show(anchors, anchors, undefined);
    } else if (documentId === seat.holding) {
      returnTo(seat, anchors, anchors, undefined);
    } else {
      // A second press while the answer is on its way waits for that answer.
      const before = latest.current;
      if (
        before.state !== "asking" ||
        before.life !== life ||
        !samePassage(before.anchors, anchors)
      ) {
        ask(seat, anchors);
      }
    }
  }

  // A passage that waits for its item is shown once the item's reader is on show as the link
  // the window is at opens it, or said to be lost when the item cannot be shown; it is dropped
  // when the reader turns to another item. What was pointed at in a reader that has gone is
  // dropped.
  useEffect(() => {
    if (pointing.state === "awaiting") {
      if (selected !== pointing.itemId && selected !== pointing.from) {
        settle(NOWHERE);
      } else if (seat?.itemId === pointing.itemId && seat.link === linkNow()) {
        if (pointing.at === undefined) {
          point(pointing.anchors);
        } else {
          show(pointing.anchors, pointing.at, pointing.said);
        }
      } else if (seat === undefined && unshown?.itemId === pointing.itemId) {
        settle({
          state: "lost",
          reason: unshown.reason,
          opening: undefined,
          item: selected,
          life: undefined,
        });
      }
    } else if (onShow(pointing, selected, life) !== pointing) {
      settle(NOWHERE);
    }
  }, [selected, life, pointing, unshown, link]);

  const current = onShow(pointing, selected, life);
  return {
    point,
    awaitOpening,
    fail(reason) {
      settle({ state: "lost", reason, opening: undefined, item: selected, life });
    },
    hide() {
      pointer.hide();
      settle(NOWHERE);
    },
    isPressed(anchor) {
      return current.state === "shown" && samePassage([current.anchors[0]], [anchor]);
    },
    isAsking(anchor) {
      return current.state === "asking" && samePassage([current.anchors[0]], [anchor]);
    },
    lost: current.state === "lost" ? current.reason : undefined,
    opening: current.state === "lost" ? current.opening : undefined,
    said: saidOf(current),
  };
}

// What the column says of the passage it points at: why it is not shown, with the way to show
// it in its own revision where there is one, or quietly how it is shown.
export function CitingNote({ citing }: { citing: Citing }) {
  if (citing.lost !== undefined) {
    const { opening } = citing;
    return (
      <div className="lost status failed" role="alert">
        {citing.lost}
        {opening !== undefined && (
          <button className="opening" onClick={opening.open}>
            {opening.label}
          </button>
        )}
      </div>
    );
  }
  // The quiet line stays in the page while it says nothing, so that what it comes to say is
  // read out.
  return (
    <p className="lost status said" role="status">
      {citing.said}
    </p>
  );
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

// A statement's citations as numbered chips, each titled with its quote. A chip whose passage
// is being looked for in the document the item holds now is drawn as waiting, never as on
// show.
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
  return anchors.map((anchor, anchorIndex) => {
    const asking = citing.isAsking(anchor);
    return (
      <button
        key={anchorIndex}
        className={asking ? "citation asking" : "citation"}
        title={asking ? `${titleOf(anchor)}\n（正在查找它现在的位置）` : titleOf(anchor)}
        aria-pressed={citing.isPressed(anchor)}
        aria-busy={asking}
        onClick={() => onPoint(anchor)}
      >
        {anchorIndex + 1}
      </button>
    );
  });
}
