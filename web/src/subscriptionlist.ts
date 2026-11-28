import { useEffect, useMemo, useRef } from "react";
import { isUnanswered, listSubscriptions, type ItemEntry, type Subscription } from "./client";
import { isBeingRead } from "./entry";
import { useWatched, type Loaded, type Watched } from "./loading";
import { isPollDue, untilNextPoll } from "./nextpoll";
import { usePolledPostings } from "./polledpostings";

export function withSubscription(listed: Subscription[], read: Subscription): Subscription[] {
  return listed.map((held) => (held.id === read.id ? read : held));
}

// When the latest poll of each subscription settled, of those whose latest poll settled.
function settledPolls(listed: Subscription[]): ReadonlyMap<string, string> {
  return new Map(
    listed.flatMap(({ id, poll }) => (poll.kind === "settled" ? [[id, poll.at] as const] : [])),
  );
}

// The owner's subscriptions, read again closely while a poll is under way or its time has come,
// and while an item that a subscription brought is being read, since its count of new items
// changes when it is read; and read again at once when an item is told as seen. A poll that
// settles may have brought items, so the list of items is read again then, and again as each
// posting it found arrives.
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

  // The one record of each subscription's latest poll that settled, by when, as the list on show
  // last told it; none before the list is first read. A poll that settled anew since is told to
  // the list of items, and what it found, for a subscription that reads what it finds, is
  // followed as it arrives.
  const settled = useRef<ReadonlyMap<string, string>>(undefined);
  const follow = usePolledPostings(onPolled);
  useEffect(() => {
    if (subscriptions.loaded.state !== "ready") {
      return;
    }
    const listed = subscriptions.loaded.value;
    const before = settled.current;
    settled.current = settledPolls(listed);
    if (before === undefined) {
      return;
    }
    const anew = listed.flatMap(({ id, follows, poll }) =>
      poll.kind === "settled" && before.get(id) !== poll.at ? [{ id, follows, poll }] : [],
    );
    if (anew.length === 0) {
      return;
    }
    onPolled();
    follow(
      anew
        .filter(({ follows, poll }) => poll.found > 0 && follows === "reads")
        .map(({ id, poll }) => ({ subscriptionId: id, at: poll.at, found: poll.found })),
    );
  }, [subscriptions.loaded]);

  return subscriptions;
}
