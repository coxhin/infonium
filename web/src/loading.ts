import { useEffect, useState } from "react";
import { Refused } from "./client";

const WATCH_INTERVAL_MS = 1500;

export type Loaded<Value> =
  | { state: "loading" }
  | { state: "ready"; value: Value }
  | { state: "failed"; reason: string };

export function reasonOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

// The statuses a gateway answers with when the service behind it does not answer. The service
// itself answers 502 only where a model call failed, which a request that calls no model never
// meets.
const UNANSWERED_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);
const UNANSWERED = "连不上服务";

// Why a request that calls no model failed, as the reader is told it: a service that did not
// answer is said as such rather than by the gateway's bare refusal.
export function requestReason(error: unknown): string {
  return error instanceof Refused && UNANSWERED_STATUSES.has(error.status)
    ? UNANSWERED
    : reasonOf(error);
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
