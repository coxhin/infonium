import { useEffect, useRef, useState } from "react";
import { listPostings, Refused, type Posting, type Subscription } from "./client";
import { isBeingRead } from "./entry";
import { useWatched, type Loaded } from "./loading";
import { POSTINGS_AT_A_TIME } from "./subscriptionstate";

const NOT_FOUND = 404;

// A poll this page saw settle, of a subscription that reads what it finds, that found postings:
// the newest of them wait to be admitted to the library, one at a time, after the poll settled.
type Arrival = { subscriptionId: string; at: string; found: number };

// The newest postings of each arrival, and which arrivals they were read for.
type Arrived = { arrivals: string[]; postings: Posting[] };

function arrivalKey(arrival: Arrival): string {
  return `${arrival.subscriptionId}@${arrival.at}`;
}

// A subscription removed meanwhile has nothing more to admit.
async function arrivedPostings(arrivals: Arrival[]): Promise<Arrived> {
  const pages = await Promise.all(
    arrivals.map((arrival) =>
      listPostings(
        arrival.subscriptionId,
        undefined,
        Math.min(arrival.found, POSTINGS_AT_A_TIME),
      ).catch((error: unknown) => {
        if (error instanceof Refused && error.status === NOT_FOUND) {
          return [];
        }
        throw error;
      }),
    ),
  );
  return { arrivals: arrivals.map(arrivalKey), postings: pages.flat() };
}

// Whether a posting waits to be admitted, or its item is being read.
function isArriving(arrived: Arrived): boolean {
  return arrived.postings.some(
    ({ admission }) =>
      admission.kind === "awaited" ||
      (admission.kind === "admitted" && isBeingRead(admission.item)),
  );
}

// How a posting stands, as one word that changes when it is admitted and when its item's reading
// settles.
function admissionWord({ number, admission }: Posting): string {
  return admission.kind === "admitted"
    ? `${number}:${admission.item.id}:${admission.item.reading.attempt.kind}`
    : `${number}:${admission.kind}`;
}

// What the polls that this page saw settle found, followed until it has all arrived: the service
// admits the postings that wait one at a time, after the poll has settled, and reads each, so the
// list of items read when the poll settled holds none of the later ones. The newest postings of
// each such subscription are read every 1.5 seconds while one of them waits or its item is being
// read, and the list of items is read again whenever one of them is admitted or its reading
// settles, so that the counts follow each item, whether or not its subscription is open.
export function useArrivals(subscriptions: Loaded<Subscription[]>, onArrived: () => void): void {
  const [arrivals, setArrivals] = useState<Arrival[]>([]);
  // The latest poll of each subscription, by when it settled, as this page last saw them.
  const polled = useRef<ReadonlyMap<string, string>>(undefined);
  useEffect(() => {
    if (subscriptions.state !== "ready") {
      return;
    }
    const before = polled.current;
    const settled = subscriptions.value.flatMap((subscription) =>
      subscription.poll.kind === "settled" ? [{ subscription, poll: subscription.poll }] : [],
    );
    polled.current = new Map(settled.map(({ subscription, poll }) => [subscription.id, poll.at]));
    if (before === undefined) {
      return;
    }
    const anew = settled.filter(
      ({ subscription, poll }) =>
        poll.found > 0 && subscription.follows === "reads" && before.get(subscription.id) !== poll.at,
    );
    if (anew.length > 0) {
      setArrivals((now) => [
        ...now,
        ...anew.map(({ subscription, poll }) => ({
          subscriptionId: subscription.id,
          at: poll.at,
          found: poll.found,
        })),
      ]);
    }
  }, [subscriptions]);

  const watched = useWatched(
    () => arrivedPostings(arrivals),
    `arrivals ${arrivals.map(arrivalKey).join(" ")}`,
    isArriving,
  );
  const arrived = watched.loaded.state === "ready" ? watched.loaded.value : undefined;
  const told = useRef("");
  useEffect(() => {
    if (arrived === undefined) {
      return;
    }
    const words = arrived.postings.map(admissionWord).join(" ");
    if (words !== told.current) {
      told.current = words;
      onArrived();
    }
    // Arrivals that have all arrived are done with.
    if (!isArriving(arrived) && arrived.arrivals.length > 0) {
      const done = new Set(arrived.arrivals);
      setArrivals((now) => now.filter((arrival) => !done.has(arrivalKey(arrival))));
    }
  }, [arrived]);
}
