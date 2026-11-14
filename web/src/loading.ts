import { useEffect, useState } from "react";

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
