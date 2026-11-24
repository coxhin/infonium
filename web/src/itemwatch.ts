import { useEffect, useState } from "react";
import { readEntry, readRevisions, type ItemEntry, type ItemRevisions } from "./client";
import { isBeingRead } from "./entry";
import { reasonOf, type Loaded } from "./loading";

const WATCH_INTERVAL_MS = 1500;

export type WatchedEntry = {
  loaded: Loaded<ItemEntry>;
  // Watches the item again from an entry the service has just given, as when a reading of it
  // has been started; the entry on show stays on show meanwhile.
  watchFrom: (entry: ItemEntry) => void;
};

// An item's entry, read again at intervals for as long as a reading of it is under way. The
// entry on show stays until the next one arrives; a read that fails while one is on show is
// tried again, as the service may be on its way back. A first read that fails is a failure.
export function useWatchedEntry(itemId: string): WatchedEntry {
  const [loaded, setLoaded] = useState<Loaded<ItemEntry>>({ state: "loading" });
  const [round, setRound] = useState(0);
  useEffect(() => {
    let current = true;
    let onShow = loaded.state === "ready";
    let timer: ReturnType<typeof setTimeout> | undefined;
    function watch() {
      readEntry(itemId).then(
        (entry) => {
          if (current) {
            onShow = true;
            setLoaded({ state: "ready", value: entry });
            timer = isBeingRead(entry) ? setTimeout(watch, WATCH_INTERVAL_MS) : undefined;
          }
        },
        (error: unknown) => {
          if (current && onShow) {
            timer = setTimeout(watch, WATCH_INTERVAL_MS);
          } else if (current) {
            setLoaded({ state: "failed", reason: reasonOf(error) });
          }
        },
      );
    }
    watch();
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [itemId, round]);
  return {
    loaded,
    watchFrom(entry) {
      setLoaded({ state: "ready", value: entry });
      setRound((before) => before + 1);
    },
  };
}

// The revisions of an item, read again whenever the key changes. The revisions read before
// stay on show until the next ones arrive, and a read that fails leaves them there.
export function useRevisions(itemId: string, key: string): Loaded<ItemRevisions> {
  const [loaded, setLoaded] = useState<Loaded<ItemRevisions>>({ state: "loading" });
  useEffect(() => {
    let current = true;
    readRevisions(itemId).then(
      (revisions) => current && setLoaded({ state: "ready", value: revisions }),
      (error: unknown) =>
        current &&
        setLoaded((before) =>
          before.state === "ready" ? before : { state: "failed", reason: reasonOf(error) },
        ),
    );
    return () => {
      current = false;
    };
  }, [itemId, key]);
  return loaded;
}
