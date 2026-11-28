import { useCallback, useEffect, useRef, useState } from "react";
import { isUnanswered, listPostings, Refused, type Posting } from "./client";
import { isBeingRead } from "./entry";
import { useWatched } from "./loading";
import { POSTINGS_AT_A_TIME } from "./subscriptionstate";

const NOT_FOUND = 404;

// A poll that this page saw settle anew, of a subscription that reads what it finds, that found
// postings: the newest of them wait to be admitted to the library, one at a time, after the poll
// settled.
export type SettledPoll = { subscriptionId: string; at: string; found: number };

// The newest postings of the polls followed, and which polls they were read for.
type Followed = { polls: string[]; postings: Posting[] };

function pollKey(poll: SettledPoll): string {
  return `${poll.subscriptionId}@${poll.at}`;
}

// A subscription removed meanwhile has nothing more to admit.
async function newestPostings(polls: SettledPoll[]): Promise<Followed> {
  const pages = await Promise.all(
    polls.map((poll) =>
      listPostings(
        poll.subscriptionId,
        undefined,
        Math.min(poll.found, POSTINGS_AT_A_TIME),
      ).catch((error: unknown) => {
        if (error instanceof Refused && error.status === NOT_FOUND) {
          return [];
        }
        throw error;
      }),
    ),
  );
  return { polls: polls.map(pollKey), postings: pages.flat() };
}

// Whether a posting followed waits to be admitted, or its item is being read.
function isUnsettled(followed: Followed): boolean {
  return followed.postings.some(
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

// The postings that polls found, followed until they have all arrived: the service admits the
// postings that wait one at a time, after the poll has settled, and reads each, so the list of
// items read when the poll settled holds none of the later ones. The newest postings of each such
// subscription are read every 1.5 seconds while one of them waits or its item is being read, and
// onArrived is told whenever one of them is admitted or its reading settles, so that the counts
// follow each item, whether or not its subscription is open. A reading that goes unanswered is
// asked again after 1.5 seconds, and one the service refuses, saying why, after a minute. What it
// gives starts following the postings of polls that settled.
export function usePolledPostings(onArrived: () => void): (polls: SettledPoll[]) => void {
  const [followed, setFollowed] = useState<SettledPoll[]>([]);
  const follow = useCallback((polls: SettledPoll[]) => {
    if (polls.length > 0) {
      setFollowed((now) => [...now, ...polls]);
    }
  }, []);
  const watched = useWatched(
    () => newestPostings(followed),
    `polled postings ${followed.map(pollKey).join(" ")}`,
    isUnsettled,
    { unanswered: isUnanswered },
  );
  const read = watched.loaded.state === "ready" ? watched.loaded.value : undefined;
  const told = useRef("");
  useEffect(() => {
    if (read === undefined) {
      return;
    }
    const words = read.postings.map(admissionWord).join(" ");
    if (words !== told.current) {
      told.current = words;
      onArrived();
    }
    // Polls whose postings have all arrived are done with.
    if (!isUnsettled(read) && read.polls.length > 0) {
      const done = new Set(read.polls);
      setFollowed((now) => now.filter((poll) => !done.has(pollKey(poll))));
    }
  }, [read]);
  return follow;
}
