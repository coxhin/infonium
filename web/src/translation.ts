import { useCallback, useMemo, useRef, useState } from "react";
import {
  ensureTranslation,
  readTranslation,
  retranslateBlock,
  translateAnew,
  type TranslatedBlock,
  type Translation,
} from "./client";
import { reasonOf, useWatched, type Loaded } from "./loading";

// How one block's second translation is going.
export type Again = { state: "underway" } | { state: "failed"; reason: string };

// What is asked of the service to begin a round of watching: that the translation go on from
// where it is, or that it start again from its first block.
type Asking = "ensure" | "anew";
type Round = { asking: Asking; count: number };

export type TranslationState = {
  // Nothing is loaded for an item until its translation is first wanted.
  loaded: Loaded<Translation> | undefined;
  interrupted: boolean;
  // The blocks translated so far by number. A block keeps its object for as long as its
  // translation stays the same, so that a view drawn from it need not be drawn again.
  blocks: ReadonlyMap<number, TranslatedBlock>;
  again: ReadonlyMap<number, Again>;
  goOn: () => void;
  anew: () => void;
  translateAgain: (blockNumber: number) => void;
};

const NO_BLOCKS: ReadonlyMap<number, TranslatedBlock> = new Map();

function isUnderway(translation: Translation | null): boolean {
  return translation?.progress.kind === "translating";
}

function sameBlock(first: TranslatedBlock, second: TranslatedBlock): boolean {
  return (
    first.model === second.model &&
    JSON.stringify(first.segments) === JSON.stringify(second.segments)
  );
}

// Keeps the object of every block whose translation has not changed, so that a translation
// that arrives in pieces changes only the blocks of the latest piece.
function steadied(
  before: ReadonlyMap<number, TranslatedBlock>,
  arrived: TranslatedBlock[],
  translatedAgain: ReadonlyMap<number, TranslatedBlock>,
): Map<number, TranslatedBlock> {
  const blocks = new Map<number, TranslatedBlock>();
  for (const block of arrived) {
    const kept = before.get(block.block_number);
    blocks.set(block.block_number, kept !== undefined && sameBlock(kept, block) ? kept : block);
  }
  // A block translated again is newer than any reading of the translation that was under way
  // when it came back; one read later holds it too.
  for (const [number, block] of translatedAgain) {
    blocks.set(number, block);
  }
  return blocks;
}

// The translation of an item's document: asked for the first time it is wanted, then watched
// while it is under way, whatever is wanted afterwards.
export function useTranslation(itemId: string, wanted: boolean): TranslationState {
  const [asked, setAsked] = useState(wanted);
  if (wanted && !asked) {
    setAsked(true);
  }
  const [round, setRound] = useState<Round>({ asking: "ensure", count: 0 });
  const [translatedAgain, setTranslatedAgain] = useState<ReadonlyMap<number, TranslatedBlock>>(
    new Map(),
  );
  const [again, setAgain] = useState<ReadonlyMap<number, Again>>(new Map());
  const key = asked ? `${itemId}:${round.count}` : `${itemId}:unasked`;

  // The first load of a round asks the service to translate; the loads after it only read.
  const begun = useRef<string>(undefined);
  function load(): Promise<Translation | null> {
    if (!asked) {
      return Promise.resolve(null);
    }
    if (begun.current !== key) {
      begun.current = key;
      return round.asking === "anew" ? translateAnew(itemId) : ensureTranslation(itemId);
    }
    return readTranslation(itemId);
  }
  const watched = useWatched(load, key, isUnderway);

  // While a new round begins, the translation on show stays until the round's first answer
  // takes its place; that of a translation started again holds none of the earlier blocks.
  const shown = useRef<Translation | null>(null);
  if (watched.loaded.state === "ready") {
    shown.current = watched.loaded.value;
  }
  const latest = shown.current;

  const steady = useRef<ReadonlyMap<number, TranslatedBlock>>(NO_BLOCKS);
  const blocks = useMemo(() => {
    steady.current = steadied(steady.current, latest?.blocks ?? [], translatedAgain);
    return steady.current;
  }, [latest, translatedAgain]);

  const translateAgain = useCallback(
    (blockNumber: number) => {
      setAgain((before) => new Map(before).set(blockNumber, { state: "underway" }));
      retranslateBlock(itemId, blockNumber).then(
        (block) => {
          setTranslatedAgain((before) => new Map(before).set(blockNumber, block));
          setAgain((before) => {
            const after = new Map(before);
            after.delete(blockNumber);
            return after;
          });
        },
        (error: unknown) =>
          setAgain((before) =>
            new Map(before).set(blockNumber, { state: "failed", reason: reasonOf(error) }),
          ),
      );
    },
    [itemId],
  );

  let loaded: Loaded<Translation> | undefined;
  if (!asked) {
    loaded = undefined;
  } else if (watched.loaded.state === "failed") {
    loaded = watched.loaded;
  } else if (latest !== null) {
    loaded = { state: "ready", value: latest };
  } else {
    loaded = { state: "loading" };
  }

  return {
    loaded,
    interrupted: watched.interrupted,
    blocks,
    again,
    goOn: () => setRound({ asking: "ensure", count: round.count + 1 }),
    anew: () => {
      setTranslatedAgain(new Map());
      setAgain(new Map());
      setRound({ asking: "anew", count: round.count + 1 });
    },
    translateAgain,
  };
}
