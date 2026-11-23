import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ensureTranslation,
  RefusedRequest,
  readTranslation,
  retranslateBlock,
  translateAnew,
  type TranslatedBlock,
  type Translation,
} from "./client";
import { reasonOf, useWatched, type Loaded } from "./loading";

// How one block's second translation is going.
export type Again = { state: "underway" } | { state: "failed"; reason: string };

// What a round of watching begins with: reading the translation as it stands, asking the
// service to go on with it, or asking it to start again from the first block.
type Asking = "read" | "ensure" | "anew";
// A translation started again begins an epoch: what was asked of the one before is dropped.
type Round = { asking: Asking; count: number; epoch: number };
type OfEpoch<Value> = { epoch: number; values: ReadonlyMap<number, Value> };

export type TranslationState = {
  // Nothing is loaded for an item until its translation is first wanted.
  loaded: Loaded<Translation> | undefined;
  interrupted: boolean;
  // The blocks translated so far by number. A block keeps its object for as long as its
  // translation stays the same, so that a view drawn from it need not be drawn again.
  blocks: ReadonlyMap<number, TranslatedBlock>;
  again: ReadonlyMap<number, Again>;
  goOn: () => void;
  readAgain: () => void;
  anew: () => void;
  translateAgain: (blockNumber: number) => void;
};

type Kept = { translation: Translation; blocks: ReadonlyMap<number, TranslatedBlock> };

const NO_BLOCKS: ReadonlyMap<number, TranslatedBlock> = new Map();

// Why translating one block again was refused, by the status of the refusal, in words a reader
// understands. A failure of the model keeps the service's own words after it, as they vary.
const NOT_ASKED_STATUS = 409;
const NEVER_TRANSLATED_STATUS = 404;
const MODEL_FAILED_STATUS = 502;
const AGAIN_REFUSALS: Record<number, string> = {
  [NOT_ASKED_STATUS]: "这篇的译文还没有请求过，或被判断为不用翻译，所以没有可以重译的段落。",
  [NEVER_TRANSLATED_STATUS]: "这一段不做翻译（例如代码或公式），所以没法重译。",
  [MODEL_FAILED_STATUS]: "模型这次没有给出可用的译文，可以稍后再试。",
};

function againFailed(error: unknown): string {
  if (error instanceof RefusedRequest) {
    const said = AGAIN_REFUSALS[error.status];
    if (said !== undefined) {
      return error.status === MODEL_FAILED_STATUS ? `${said}服务说：${error.message}` : said;
    }
  }
  return reasonOf(error);
}

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
): ReadonlyMap<number, TranslatedBlock> {
  const blocks = new Map<number, TranslatedBlock>();
  for (const block of arrived) {
    const kept = before.get(block.block_number);
    blocks.set(block.block_number, kept !== undefined && sameBlock(kept, block) ? kept : block);
  }
  return blocks;
}

function withValue<Value>(
  before: OfEpoch<Value>,
  epoch: number,
  number: number,
  value: Value | undefined,
): OfEpoch<Value> {
  if (before.epoch !== epoch) {
    return before;
  }
  const values = new Map(before.values);
  if (value === undefined) {
    values.delete(number);
  } else {
    values.set(number, value);
  }
  return { epoch, values };
}

// The translation of an item's document. It is read the first time it is wanted, and asked
// for only when the service has never been asked for it; then it is watched while it is under
// way, whatever is wanted afterwards. A translation that stopped goes on only when the reader
// asks it to.
export function useTranslation(itemId: string, wanted: boolean): TranslationState {
  const [asked, setAsked] = useState(wanted);
  if (wanted && !asked) {
    setAsked(true);
  }
  const wantedNow = useRef(wanted);
  useEffect(() => {
    wantedNow.current = wanted;
  }, [wanted]);
  const [round, setRound] = useState<Round>({ asking: "read", count: 0, epoch: 0 });
  const [translatedAgain, setTranslatedAgain] = useState<OfEpoch<TranslatedBlock>>({
    epoch: 0,
    values: new Map(),
  });
  const [again, setAgain] = useState<OfEpoch<Again>>({ epoch: 0, values: new Map() });
  const key = asked ? `${itemId}:${round.count}` : `${itemId}:unasked`;

  // A round that goes on or starts again begins with that request; every other load reads,
  // and a translation the service says was never asked for is asked for while it is wanted.
  const begun = useRef<string>(undefined);
  function load(): Promise<Translation | null> {
    if (!asked) {
      return Promise.resolve(null);
    }
    if (begun.current !== key && round.asking !== "read") {
      begun.current = key;
      return round.asking === "anew" ? translateAnew(itemId) : ensureTranslation(itemId);
    }
    return readTranslation(itemId).then((translation) =>
      translation.progress.kind === "unasked" && wantedNow.current
        ? ensureTranslation(itemId)
        : translation,
    );
  }
  const watched = useWatched(load, key, isUnderway);

  // The translation on show stays while a new round begins, until the round's first answer
  // takes its place; that of a translation started again holds none of the earlier blocks.
  const [kept, setKept] = useState<Kept>();
  useEffect(() => {
    const loaded = watched.loaded;
    if (loaded.state === "ready" && loaded.value !== null) {
      const translation = loaded.value;
      setKept((before) => ({
        translation,
        blocks: steadied(before?.blocks ?? NO_BLOCKS, translation.blocks),
      }));
    }
  }, [watched.loaded]);

  // A translation that the service had forgotten while none was on show is asked for once one
  // is on show again.
  useEffect(() => {
    if (wanted && kept?.translation.progress.kind === "unasked") {
      setRound((before) => ({ ...before, asking: "read", count: before.count + 1 }));
    }
  }, [wanted]);

  // A block translated again is newer than any reading of the translation that was under way
  // when it came back; one read later holds it too.
  const blocks = useMemo(() => {
    const read = kept?.blocks ?? NO_BLOCKS;
    if (translatedAgain.values.size === 0) {
      return read;
    }
    const merged = new Map(read);
    translatedAgain.values.forEach((block, number) => merged.set(number, block));
    return merged;
  }, [kept, translatedAgain]);

  const epoch = round.epoch;
  const translateAgain = useCallback(
    (blockNumber: number) => {
      setAgain((before) => withValue(before, epoch, blockNumber, { state: "underway" }));
      retranslateBlock(itemId, blockNumber).then(
        (block) => {
          setTranslatedAgain((before) => withValue(before, epoch, blockNumber, block));
          setAgain((before) => withValue<Again>(before, epoch, blockNumber, undefined));
        },
        (error: unknown) =>
          setAgain((before) =>
            withValue(before, epoch, blockNumber, { state: "failed", reason: againFailed(error) }),
          ),
      );
    },
    [itemId, epoch],
  );

  let loaded: Loaded<Translation> | undefined;
  if (!asked) {
    loaded = undefined;
  } else if (watched.loaded.state === "failed") {
    loaded = watched.loaded;
  } else if (kept !== undefined) {
    loaded = { state: "ready", value: kept.translation };
  } else {
    loaded = { state: "loading" };
  }

  return {
    loaded,
    interrupted: watched.interrupted,
    blocks,
    again: again.values,
    goOn: () => setRound({ ...round, asking: "ensure", count: round.count + 1 }),
    readAgain: () => setRound({ ...round, asking: "read", count: round.count + 1 }),
    anew: () => {
      const next = round.epoch + 1;
      setTranslatedAgain({ epoch: next, values: new Map() });
      setAgain({ epoch: next, values: new Map() });
      setRound({ asking: "anew", count: round.count + 1, epoch: next });
    },
    translateAgain,
  };
}
