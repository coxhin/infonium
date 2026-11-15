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

// Like useLoaded, and loads again at intervals for as long as the value is unsettled. The
// value on show stays until the next one arrives. A load that fails is tried again too, as
// the service may be on its way back; a value on show stays meanwhile.
export function useWatched<Value>(
  load: () => Promise<Value>,
  key: string,
  unsettled: (value: Value) => boolean,
): Loaded<Value> {
  const [loaded, setLoaded] = useState<Loaded<Value>>({ state: "loading" });
  useEffect(() => {
    let current = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    function watch() {
      load().then(
        (value) => {
          if (current) {
            setLoaded({ state: "ready", value });
            timer = unsettled(value) ? setTimeout(watch, WATCH_INTERVAL_MS) : undefined;
          }
        },
        (error: unknown) => {
          if (current) {
            setLoaded((shown) =>
              shown.state === "ready" ? shown : { state: "failed", reason: reasonOf(error) },
            );
            timer = setTimeout(watch, WATCH_INTERVAL_MS);
          }
        },
      );
    }
    setLoaded({ state: "loading" });
    watch();
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [key]);
  return loaded;
}
