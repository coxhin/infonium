import { useCallback, useEffect, useRef, useState } from "react";

export const WATCH_INTERVAL_MS = 1500;
// How often a list on show is read again while nothing on it is under way, so that what came
// meanwhile, as postings a scheduled poll brought, appears without a reload.
export const LIST_INTERVAL_MS = 60_000;
// How long a refreshed value that the service refused, saying why, waits to be asked again: a
// refusal the service explains, as that of a schedule that stopped, lasts, and is not asked
// about every 1.5 seconds.
const REFUSED_INTERVAL_MS = 60_000;

export type Loaded<Value> =
  | { state: "loading" }
  | { state: "ready"; value: Value }
  | { state: "failed"; reason: string };

export function reasonOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// Loads again whenever the key changes; a result that arrives for an earlier key is dropped.
export function useLoaded<Value>(load: () => Promise<Value>, key: string): Loaded<Value> {
  const [loaded, setLoaded] = useState<Loaded<Value>>({ state: "loading" });
  useEffect(() => {
    let current = true;
    setLoaded({ state: "loading" });
    load().then(
      (value) => current && setLoaded({ state: "ready", value }),
      (error: unknown) => current && setLoaded({ state: "failed", reason: reasonOf(error) }),
    );
    return () => {
      current = false;
    };
  }, [key]);
  return loaded;
}

export type Watched<Value> = {
  loaded: Loaded<Value>;
  interrupted: boolean;
  // While the watch is interrupted, why, where the service said so in refusing a refreshed
  // value; nothing where it did not answer.
  refusal: string | undefined;
  // Watches again from a value the service has just given, as when it was asked to start
  // what makes the value unsettled; the value is on show at once and stays on show meanwhile.
  watchFrom: (value: Value) => void;
  // Reads the value again now, while the value on show stays on show; after a first load that
  // failed, it is tried again.
  readNow: () => void;
  // Puts on show a value made from the one on show, as a list with an entry read apart; the
  // watch goes on, closely from then on where that value is unsettled. A change that gives back
  // the value on show leaves it as it is, and draws nothing anew.
  replace: (change: (value: Value) => Value) => void;
  // Puts on show what the service answered a press with, made into the value on show at the
  // moment it arrives, and reads the value again at once: a load under way was asked before
  // the answer, and is dropped.
  answer: (change: (value: Value) => Value) => void;
};

// How a value is read again: after a reading of it fails, and, where an interval is given, while
// it is settled. A reading that went unanswered, as the caller tells such a failure, is tried
// again after 1.5 seconds, the first among them; one the service refused, saying why, after a
// minute. A settled value is read again after the interval the value read last gives, while the
// page is visible, and at once when a page that was hidden past it is shown again.
export type Rereading<Value> = {
  interval?: (value: Value) => number;
  unanswered: (error: unknown) => boolean;
};

type Watch<Value> = {
  loaded: Loaded<Value>;
  interrupted: boolean;
  refusal: string | undefined;
};

// Like useLoaded, and loads again at intervals for as long as the value is unsettled, and, with
// rereading that gives an interval, while it is settled. The value on show stays until the next
// one arrives. A load that fails while a value is on show leaves the value there, marks the
// watch interrupted, and is tried again, as the service may be on its way back. Without
// rereading, a first load that fails is a failure, not tried again. With rereading, one that
// went unanswered marks the watch interrupted and is tried again after 1.5 seconds, and one the
// service refused, saying why, is a failure on show until it is tried again after a minute.
export function useWatched<Value>(
  load: () => Promise<Value>,
  key: string,
  unsettled: (value: Value) => boolean,
  rereading?: Rereading<Value>,
): Watched<Value> {
  const [watched, setWatched] = useState<Watch<Value>>({
    loaded: { state: "loading" },
    interrupted: false,
    refusal: undefined,
  });
  const [round, setRound] = useState(0);
  // The key whose value is on show, which a watch begun again for the same key keeps.
  const shownFor = useRef<string>(undefined);
  // Watches closely from now on, where the watch waits to refresh a settled value.
  const hurry = useRef<() => void>(undefined);
  const latestUnsettled = useRef(unsettled);
  latestUnsettled.current = unsettled;
  useEffect(() => {
    let current = true;
    let onShow = shownFor.current === key;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Whether the timer waits to refresh a settled value, and whether that time came while
    // the page was hidden.
    let waiting = false;
    let overdue = false;
    function refreshWhenVisible() {
      if (document.visibilityState === "visible") {
        watch();
      } else {
        overdue = true;
      }
    }
    function onVisibility() {
      if (overdue && document.visibilityState === "visible") {
        overdue = false;
        watch();
      }
    }
    hurry.current = () => {
      if (waiting) {
        clearTimeout(timer);
        waiting = false;
        overdue = false;
        timer = setTimeout(watch, WATCH_INTERVAL_MS);
      }
    };
    function watch() {
      waiting = false;
      load().then(
        (value) => {
          if (current) {
            onShow = true;
            shownFor.current = key;
            setWatched({
              loaded: { state: "ready", value },
              interrupted: false,
              refusal: undefined,
            });
            if (unsettled(value)) {
              timer = setTimeout(watch, WATCH_INTERVAL_MS);
            } else if (rereading?.interval !== undefined) {
              waiting = true;
              timer = setTimeout(refreshWhenVisible, rereading.interval(value));
            }
          }
        },
        (error: unknown) => {
          if (!current) {
            return;
          }
          if (onShow || rereading?.unanswered(error) === true) {
            const refusal =
              rereading === undefined || rereading.unanswered(error)
                ? undefined
                : reasonOf(error);
            setWatched((before) => ({ loaded: before.loaded, interrupted: true, refusal }));
            timer = setTimeout(
              watch,
              refusal === undefined ? WATCH_INTERVAL_MS : REFUSED_INTERVAL_MS,
            );
            return;
          }
          setWatched({
            loaded: { state: "failed", reason: reasonOf(error) },
            interrupted: false,
            refusal: undefined,
          });
          // A first reading that the service refused, saying why, is asked again as a later
          // reading is.
          if (rereading !== undefined) {
            timer = setTimeout(watch, REFUSED_INTERVAL_MS);
          }
        },
      );
    }
    if (!onShow) {
      setWatched({ loaded: { state: "loading" }, interrupted: false, refusal: undefined });
    }
    if (rereading?.interval !== undefined) {
      document.addEventListener("visibilitychange", onVisibility);
    }
    watch();
    return () => {
      current = false;
      clearTimeout(timer);
      hurry.current = undefined;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [key, round]);
  // A value put on show here may be unsettled though the value loaded last was not.
  useEffect(() => {
    if (watched.loaded.state === "ready" && latestUnsettled.current(watched.loaded.value)) {
      hurry.current?.();
    }
  }, [watched.loaded]);
  const watchFrom = useCallback(
    (value: Value) => {
      shownFor.current = key;
      setWatched({ loaded: { state: "ready", value }, interrupted: false, refusal: undefined });
      setRound((before) => before + 1);
    },
    [key],
  );
  const readNow = useCallback(() => setRound((before) => before + 1), []);
  const replace = useCallback(
    (change: (value: Value) => Value) =>
      setWatched((before) => {
        if (before.loaded.state !== "ready") {
          return before;
        }
        const value = change(before.loaded.value);
        return value === before.loaded.value
          ? before
          : { ...before, loaded: { state: "ready", value } };
      }),
    [],
  );
  const answer = useCallback(
    (change: (value: Value) => Value) => {
      replace(change);
      readNow();
    },
    [replace, readNow],
  );
  return { ...watched, watchFrom, readNow, replace, answer };
}
