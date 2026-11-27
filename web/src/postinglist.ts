import { useCallback, useRef, useState } from "react";
import {
  listPostings,
  readSubscription,
  Refused,
  requestReason,
  type Posting,
  type Subscription,
} from "./client";
import { isBeingRead } from "./entry";
import { useFollowed, type Followed } from "./following";

// How many postings are read at a time: the newest when a subscription is opened, and as many
// older ones each time the reader asks for more.
export const POSTINGS_AT_A_TIME = 50;
// The most postings the service gives in one answer.
const MOST_AT_ONCE = 200;
// How many of the newest postings are looked through for the one whose item is open, when a
// subscription is opened at that item.
const SOUGHT_AMONG = 1000;
const NOT_FOUND = 404;

// An open subscription as the list column shows it: the subscription, and the postings read of
// it so far, the newest first, which run without a gap from the newest to the oldest read; or
// that it is gone, removed elsewhere.
export type OpenSubscription =
  | { kind: "open"; subscription: Subscription; postings: Posting[] }
  | { kind: "gone" };

export type OlderReading =
  | { state: "idle" }
  | { state: "reading" }
  | { state: "failed"; reason: string };
const IDLE: OlderReading = { state: "idle" };

// Whether what is listed is under way: a poll, a posting that waits to be handed to the library,
// or an item being read.
function isUnsettled(open: OpenSubscription): boolean {
  return (
    open.kind === "open" &&
    (open.subscription.poll.kind === "underway" ||
      open.postings.some(
        ({ admission }) =>
          admission.kind === "awaited" ||
          (admission.kind === "admitted" && isBeingRead(admission.item)),
      ))
  );
}

// The postings of numbers from the floor up, the newest first, read page by page from the
// newest; each page asks for as many as are held, and a page more, so that one page does in
// most readings.
async function postingsDownTo(
  subscriptionId: string,
  floor: number,
  held: number,
): Promise<Posting[]> {
  const read: Posting[] = [];
  const limit = Math.min(MOST_AT_ONCE, held + POSTINGS_AT_A_TIME);
  let before: number | undefined;
  for (;;) {
    const page = await listPostings(subscriptionId, before, limit);
    read.push(...page.filter((posting) => posting.number >= floor));
    const last = page.at(-1);
    if (page.length < limit || last === undefined || last.number <= floor) {
      return read;
    }
    before = last.number;
  }
}

// The newest postings, read until the one whose item is sought is among them, or as many as
// are looked through have been read.
async function postingsSeeking(subscriptionId: string, sought: string): Promise<Posting[]> {
  const read: Posting[] = [];
  let before: number | undefined;
  for (;;) {
    const page = await listPostings(subscriptionId, before, MOST_AT_ONCE);
    read.push(...page);
    const last = page.at(-1);
    const found = page.some(
      ({ admission }) => admission.kind === "admitted" && admission.item.id === sought,
    );
    if (found || page.length < MOST_AT_ONCE || last === undefined || read.length >= SOUGHT_AMONG) {
      return found ? read : read.slice(0, POSTINGS_AT_A_TIME);
    }
    before = last.number;
  }
}

// Postings read again from the newest down to a floor, in place of those held from that floor
// up, above those held below it, which were read meanwhile.
function withOlder(fresh: Posting[], held: Posting[], floor: number): Posting[] {
  return [...fresh, ...held.filter((posting) => posting.number < floor)];
}

export type FollowedSubscription = Followed<OpenSubscription> & {
  older: OlderReading;
  readOlder: () => void;
};

// An open subscription, followed: read again every 1.5 seconds while something on it is under
// way and every minute otherwise, while it is on show. A reading again keeps the place the
// reader went down to: it reads every posting from the newest down to the oldest held. The
// first reading, of a subscription opened at an item, reads on until that item's posting is
// among those read.
export function useOpenSubscription(
  subscriptionId: string,
  sought: string | undefined,
  shown: boolean,
): FollowedSubscription {
  const held = useRef<Posting[] | undefined>(undefined);
  const load = useCallback(async (): Promise<OpenSubscription> => {
    const before = held.current;
    const floor = before?.at(-1)?.number;
    try {
      const [subscription, fresh] = await Promise.all([
        readSubscription(subscriptionId),
        before === undefined || floor === undefined
          ? sought === undefined
            ? listPostings(subscriptionId, undefined, POSTINGS_AT_A_TIME)
            : postingsSeeking(subscriptionId, sought)
          : postingsDownTo(subscriptionId, floor, before.length),
      ]);
      const postings = floor === undefined ? fresh : withOlder(fresh, held.current ?? [], floor);
      return { kind: "open", subscription, postings };
    } catch (error) {
      if (error instanceof Refused && error.status === NOT_FOUND) {
        return { kind: "gone" };
      }
      throw error;
    }
  }, [subscriptionId, sought]);
  const followed = useFollowed(load, subscriptionId, isUnsettled, shown);
  held.current =
    followed.loaded.state === "ready" && followed.loaded.value.kind === "open"
      ? followed.loaded.value.postings
      : undefined;

  const [older, setOlder] = useState<OlderReading>(IDLE);
  const { replace } = followed;
  const readOlder = useCallback(() => {
    const oldest = held.current?.at(-1);
    if (oldest === undefined) {
      return;
    }
    setOlder({ state: "reading" });
    listPostings(subscriptionId, oldest.number, POSTINGS_AT_A_TIME).then(
      (page) => {
        setOlder(IDLE);
        replace((open) =>
          open.kind === "open"
            ? {
                ...open,
                postings: [
                  ...open.postings,
                  ...page.filter((posting) => posting.number < oldest.number),
                ],
              }
            : open,
        );
      },
      (error: unknown) => setOlder({ state: "failed", reason: requestReason(error) }),
    );
  }, [subscriptionId, replace]);
  return { ...followed, older, readOlder };
}
