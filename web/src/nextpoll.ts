import type { Subscription } from "./client";
import { LIST_INTERVAL_MS, WATCH_INTERVAL_MS } from "./loading";

// A poll whose time has come is waited for as one under way is followed, every 1.5 seconds,
// until it has begun, and so settled or failed; but no longer than this after its time, since a
// poll is begun within seconds of it, and one not begun by then is waited for at the minute.
const POLL_AWAITED_MS = 120_000;
// A list is read again this long after the time of the next poll it shows, which is then past.
const PAST_DUE_MS = 1000;

// The time of a subscription's next poll, where one is to come by the schedule: none while a
// poll is under way, and none for a subscription paused.
function dueOf(subscription: Subscription): number | undefined {
  return subscription.follows === "paused" || subscription.poll.kind === "underway"
    ? undefined
    : Date.parse(subscription.poll.due);
}

// Whether the subscription's next poll is due: its time has come, a little while ago at most.
export function isPollDue(subscription: Subscription, now: number): boolean {
  const due = dueOf(subscription);
  return due !== undefined && due <= now && now - due < POLL_AWAITED_MS;
}

// How long a list of subscriptions, settled, waits to be read again: a minute, or until just
// after the time of the earliest poll to come, where that is sooner.
export function untilNextPoll(subscriptions: Subscription[], now: number): number {
  const ahead = subscriptions
    .map(dueOf)
    .filter((due): due is number => due !== undefined && due > now)
    .map((due) => due - now + PAST_DUE_MS);
  return Math.max(WATCH_INTERVAL_MS, Math.min(LIST_INTERVAL_MS, ...ahead));
}
