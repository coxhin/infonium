import { useEffect, useMemo, useRef } from "react";
import { isUnanswered, listSubscriptions, type ItemEntry, type Subscription } from "./client";
import { isBeingRead } from "./entry";
import { useWatched, type Loaded, type Watched } from "./loading";
import { isPollDue, untilNextPoll } from "./nextpoll";

// The subscription the list column shows: which one; the item it was opened at, whose posting
// it looks for, if any; and whether it was opened as the answer to subscribing to a feed the
// list held already.
export type SubscriptionPlace = {
  subscriptionId: string;
  sought: string | undefined;
  again: boolean;
};

export function withSubscription(listed: Subscription[], read: Subscription): Subscription[] {
  return listed.map((held) => (held.id === read.id ? read : held));
}

// The owner's subscriptions, read again closely while a poll is under way or its time has come,
// and while an item that a subscription brought is being read, since its count of new items
// changes when it is read; and read again at once when an item is told as seen. A poll that
// settles may have brought items, so the list of items is read again then.
export function useSubscriptions(
  entries: Loaded<ItemEntry[]>,
  seen: ReadonlySet<string>,
  onPolled: () => void,
): Watched<Subscription[]> {
  const readingSubscribed = useMemo(
    () =>
      entries.state === "ready"
        ? entries.value
            .filter((entry) => entry.origin.kind === "subscribed" && isBeingRead(entry))
            .map((entry) => entry.id)
            .join(" ")
        : "",
    [entries],
  );
  const subscriptions = useWatched(
    listSubscriptions,
    "subscriptions",
    (listed) =>
      readingSubscribed !== "" ||
      listed.some(
        (subscription) =>
          subscription.poll.kind === "underway" || isPollDue(subscription, Date.now()),
      ),
    {
      interval: (listed) => untilNextPoll(listed, Date.now()),
      shown: true,
      unanswered: isUnanswered,
    },
  );

  const told = useRef({ readingSubscribed, seen: seen.size });
  useEffect(() => {
    if (told.current.readingSubscribed !== readingSubscribed || told.current.seen !== seen.size) {
      told.current = { readingSubscribed, seen: seen.size };
      subscriptions.readNow();
    }
  }, [readingSubscribed, seen.size]);

  // The latest poll of each subscription, by when it was; one that settles anew changes it.
  const polls =
    subscriptions.loaded.state === "ready"
      ? subscriptions.loaded.value
          .map(({ id, poll }) => (poll.kind === "underway" ? "" : `${id}@${poll.at}`))
          .join(" ")
      : undefined;
  const polled = useRef(polls);
  useEffect(() => {
    if (polled.current !== undefined && polls !== undefined && polled.current !== polls) {
      onPolled();
    }
    polled.current = polls;
  }, [polls]);

  return subscriptions;
}
