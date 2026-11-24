import { useEffect, useState } from "react";
import { readRevisions, type ItemRevisions } from "./client";
import { reasonOf, type Loaded } from "./loading";

// The revisions of an item, read again whenever the key changes. The revisions read before
// stay on show until the next ones arrive; a read that fails says why, unless revisions are
// on show already, which then stay.
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
