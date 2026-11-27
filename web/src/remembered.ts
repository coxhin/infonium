import { useCallback, useState } from "react";
import {
  ALL,
  MEDIA_CHOICES,
  keyOf,
  selectionOfKey,
  type MediaChoice,
  type Order,
  type Selection,
} from "./sidebarselection";

// What the browser remembers of the sidebar: the selection, the media, the order, the folders
// folded, and whether the sidebar is folded. Each is read once, when the app opens, and kept
// again whenever it changes. Another window of the app keeps choices of its own: a selection
// made in one does not move the other.
const SELECTION_KEY = "infonium.selection";
const MEDIA_KEY = "infonium.media";
const ORDER_KEY = "infonium.sidebarOrder";
const FOLDED_FOLDERS_KEY = "infonium.foldedFolders";
const SIDEBAR_KEY = "infonium.sidebar";

const ORDERS: Order[] = ["name", "new"];

// Whether the reader folded the sidebar or unfolded it; none until they choose, when the width
// of the window decides.
export type Folding = "folded" | "unfolded" | undefined;
const FOLDINGS: Folding[] = ["folded", "unfolded"];

function useRemembered<Value>(
  key: string,
  read: (stored: string | null) => Value,
  write: (value: Value) => string | undefined,
): [Value, (value: Value) => void] {
  const [value, setValue] = useState(() => read(localStorage.getItem(key)));
  const keep = useCallback(
    (chosen: Value) => {
      setValue(chosen);
      const written = write(chosen);
      if (written === undefined) {
        localStorage.removeItem(key);
      } else {
        localStorage.setItem(key, written);
      }
    },
    [key],
  );
  return [value, keep];
}

function oneOf<Value extends string | undefined>(
  choices: readonly Value[],
  fallback: Value,
): (stored: string | null) => Value {
  return (stored) => choices.find((choice) => choice === stored) ?? fallback;
}

function foldedOf(stored: string | null): ReadonlySet<string> {
  try {
    const read: unknown = JSON.parse(stored ?? "[]");
    return new Set(
      Array.isArray(read) ? read.filter((id): id is string => typeof id === "string") : [],
    );
  } catch {
    // A value another version of the app kept, or one changed by hand, folds nothing.
    return new Set();
  }
}

export function useRememberedSelection(): [Selection, (selection: Selection) => void] {
  return useRemembered(SELECTION_KEY, (stored) => selectionOfKey(stored ?? "") ?? ALL, keyOf);
}

export function useRememberedMedia(): [MediaChoice, (media: MediaChoice) => void] {
  return useRemembered(MEDIA_KEY, oneOf(MEDIA_CHOICES, "any"), (media) => media);
}

export function useRememberedOrder(): [Order, (order: Order) => void] {
  return useRemembered(ORDER_KEY, oneOf(ORDERS, "name"), (order) => order);
}

export function useFoldedFolders(): [ReadonlySet<string>, (folded: ReadonlySet<string>) => void] {
  return useRemembered(FOLDED_FOLDERS_KEY, foldedOf, (folded) => JSON.stringify([...folded]));
}

export function useRememberedFolding(): [Folding, (folding: Folding) => void] {
  return useRemembered(SIDEBAR_KEY, oneOf(FOLDINGS, undefined), (folding) => folding);
}
