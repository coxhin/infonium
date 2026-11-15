import { useEffect, useState } from "react";

const WATCH_INTERVAL_MS = 1500;

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

export type Watched<Value> = { loaded: Loaded<Value>; interrupted: boolean };

// Like useLoaded, and loads again at intervals for as long as the value is unsettled. The
// value on show stays until the next one arrives. A load that fails while a value is on show
// leaves the value there, marks the watch interrupted, and is tried again, as the service may
// be on its way back. A first load that fails is a failure and is not tried again.
export function useWatched<Value>(
  load: () => Promise<Value>,
  key: string,
  unsettled: (value: Value) => boolean,
): Watched<Value> {
  const [watched, setWatched] = useState<Watched<Value>>({
    loaded: { state: "loading" },
    interrupted: false,
  });
  useEffect(() => {
    let current = true;
    let onShow = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    function watch() {
      load().then(
        (value) => {
          if (current) {
            onShow = true;
            setWatched({ loaded: { state: "ready", value }, interrupted: false });
            timer = unsettled(value) ? setTimeout(watch, WATCH_INTERVAL_MS) : undefined;
          }
        },
        (error: unknown) => {
          if (current && onShow) {
            setWatched((shown) => ({ loaded: shown.loaded, interrupted: true }));
            timer = setTimeout(watch, WATCH_INTERVAL_MS);
          } else if (current) {
            setWatched({
              loaded: { state: "failed", reason: reasonOf(error) },
              interrupted: false,
            });
          }
        },
      );
    }
    setWatched({ loaded: { state: "loading" }, interrupted: false });
    watch();
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [key]);
  return watched;
}
