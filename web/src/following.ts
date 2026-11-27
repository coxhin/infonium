import { useCallback, useEffect, useRef, useState } from "react";
import { reasonOf, type Loaded } from "./loading";

// As often as the list of items has always been read while one of them is being read
// (useWatched).
const BUSY_INTERVAL_MS = 1500;
// How often a list on show is read again while nothing on it is under way, so that what came
// meanwhile, as a scheduled poll brings it, appears without a reload.
const QUIET_INTERVAL_MS = 60_000;

export type Followed<Value> = {
  loaded: Loaded<Value>;
  interrupted: boolean;
  // Reads again now, while the value on show stays on show; after a failed first reading, it
  // tries again.
  readNow: () => void;
  // Puts a value the service has just given on show, and follows on from it.
  watchFrom: (value: Value) => void;
  // Puts a value on show that was made here from what the service said, as a list with one of
  // its entries read again; the following goes on, and closely once that value is unsettled.
  replace: (change: (value: Value) => Value) => void;
};

type Follow<Value> = { loaded: Loaded<Value>; interrupted: boolean };

// Like useWatched, and reads the value again also while it is settled: every minute while the
// page is visible, and at once when a page that was hidden past its minute comes on show again.
// A value that is not shown is not followed, and is read again as soon as it is shown. The
// load and the test of being unsettled are the latest given, so that they may read what was
// drawn since the following began. A value read for one key stays on show while it is read
// again for the same key.
export function useFollowed<Value>(
  load: () => Promise<Value>,
  key: string,
  unsettled: (value: Value) => boolean,
  shown: boolean,
): Followed<Value> {
  const [follow, setFollow] = useState<Follow<Value>>({
    loaded: { state: "loading" },
    interrupted: false,
  });
  const [round, setRound] = useState(0);
  const latestLoad = useRef(load);
  const latestUnsettled = useRef(unsettled);
  latestLoad.current = load;
  latestUnsettled.current = unsettled;
  const shownFor = useRef<string>(undefined);
  // Follows closely from now on, where the following waits its minute.
  const hurry = useRef<() => void>(undefined);
  useEffect(() => {
    if (!shown) {
      return undefined;
    }
    let current = true;
    let onShow = shownFor.current === key;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let quiet = false;
    let overdue = false;
    function readWhenVisible() {
      if (document.visibilityState === "visible") {
        read();
      } else {
        overdue = true;
      }
    }
    function onVisibility() {
      if (overdue && document.visibilityState === "visible") {
        overdue = false;
        read();
      }
    }
    hurry.current = () => {
      if (quiet) {
        clearTimeout(timer);
        quiet = false;
        overdue = false;
        timer = setTimeout(read, BUSY_INTERVAL_MS);
      }
    };
    function read() {
      quiet = false;
      latestLoad.current().then(
        (value) => {
          if (!current) {
            return;
          }
          onShow = true;
          shownFor.current = key;
          setFollow({ loaded: { state: "ready", value }, interrupted: false });
          quiet = !latestUnsettled.current(value);
          timer = quiet
            ? setTimeout(readWhenVisible, QUIET_INTERVAL_MS)
            : setTimeout(read, BUSY_INTERVAL_MS);
        },
        (error: unknown) => {
          if (current && onShow) {
            setFollow((before) => ({ loaded: before.loaded, interrupted: true }));
            timer = setTimeout(read, BUSY_INTERVAL_MS);
          } else if (current) {
            setFollow({ loaded: { state: "failed", reason: reasonOf(error) }, interrupted: false });
          }
        },
      );
    }
    if (!onShow) {
      setFollow({ loaded: { state: "loading" }, interrupted: false });
    }
    document.addEventListener("visibilitychange", onVisibility);
    read();
    return () => {
      current = false;
      clearTimeout(timer);
      hurry.current = undefined;
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [key, round, shown]);
  // A value put on show here, as a list with one of its entries read again, may be unsettled
  // though the value read last was not.
  useEffect(() => {
    if (follow.loaded.state === "ready" && latestUnsettled.current(follow.loaded.value)) {
      hurry.current?.();
    }
  }, [follow.loaded]);
  const readNow = useCallback(() => setRound((before) => before + 1), []);
  const watchFrom = useCallback(
    (value: Value) => {
      shownFor.current = key;
      setFollow({ loaded: { state: "ready", value }, interrupted: false });
      setRound((before) => before + 1);
    },
    [key],
  );
  const replace = useCallback(
    (change: (value: Value) => Value) =>
      setFollow((before) =>
        before.loaded.state === "ready"
          ? { ...before, loaded: { state: "ready", value: change(before.loaded.value) } }
          : before,
      ),
    [],
  );
  return { ...follow, readNow, watchFrom, replace };
}
