import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useRememberedFolding } from "./remembered";

// How wide the window is, as the layout of the app tells it: wide enough for four columns, the
// width at which the sidebar is a rail by default, and the width of one column.
export type Width = "wide" | "middle" | "narrow";
const WIDE = matchMedia("(width >= 100rem)");
const NARROW = matchMedia("(width <= 64rem)");

function widthNow(): Width {
  return WIDE.matches ? "wide" : NARROW.matches ? "narrow" : "middle";
}

function onWidthChange(notify: () => void): () => void {
  WIDE.addEventListener("change", notify);
  NARROW.addEventListener("change", notify);
  return () => {
    WIDE.removeEventListener("change", notify);
    NARROW.removeEventListener("change", notify);
  };
}

export const SHORTCUT_NAME = /Mac|iPhone|iPad/.test(navigator.platform) ? "⌘\\" : "Ctrl+\\";

function isShortcut(event: KeyboardEvent): boolean {
  return (event.ctrlKey || event.metaKey) && (event.key === "\\" || event.code === "Backslash");
}

// Where the keyboard goes once the sidebar has changed its form: to the control that changes it
// back, or to the one that opened it.
const UNFOLD_CONTROL = ".rail-unfold";
const FOLD_CONTROL = ".sidebar-fold";

export type SidebarLayout = {
  width: Width;
  // Shown as a column of its own.
  pinned: boolean;
  // Shown over the list column, or in one column over the page.
  overlaid: boolean;
  open: (opener: HTMLElement) => void;
  // Puts the sidebar over the page away; the keyboard goes back where it was when it opened.
  close: () => void;
  fold: () => void;
  unfold: () => void;
};

// The keyboard, where it was in a part of the sidebar that a change of form takes away, goes to
// the control that changes it back; anywhere else, as in the reader, it stays.
function focusSoonFrom(going: string, selector: string) {
  if (document.activeElement?.closest(going) === null) {
    return;
  }
  requestAnimationFrame(() => document.querySelector<HTMLElement>(selector)?.focus());
}

// The sidebar is a column at 100rem and wider and a rail below that, unless the reader folded or
// unfolded it, which the browser remembers and which holds at every width wider than 64rem. Over
// that, the rail or a button opens it over the page, until Escape, a press outside it or a
// selection closes it. In one column it is only ever over the page. Ctrl+\ (⌘\) folds and
// unfolds it, or, in one column, opens and closes it.
export function useSidebarLayout(): SidebarLayout {
  const width = useSyncExternalStore(onWidthChange, widthNow);
  const [folding, setFolding] = useRememberedFolding();
  const [overlaid, setOverlaid] = useState(false);
  const opener = useRef<HTMLElement>(undefined);
  const pinned =
    width !== "narrow" && (folding ?? (width === "wide" ? "unfolded" : "folded")) === "unfolded";

  const open = useCallback((from: HTMLElement) => {
    opener.current = from;
    setOverlaid(true);
  }, []);

  const close = useCallback(() => {
    setOverlaid(false);
    const back = opener.current;
    opener.current = undefined;
    // The keyboard was in the sidebar, which goes: it goes back to what opened it.
    if (back?.isConnected === true && document.activeElement?.closest(".sidebar") !== null) {
      back.focus({ preventScroll: true });
    }
  }, []);

  const fold = useCallback(() => {
    setOverlaid(false);
    if (width === "narrow") {
      opener.current?.focus({ preventScroll: true });
      opener.current = undefined;
      return;
    }
    focusSoonFrom(".sidebar", UNFOLD_CONTROL);
    setFolding("folded");
  }, [width]);

  const unfold = useCallback(() => {
    if (width === "narrow") {
      return;
    }
    focusSoonFrom(".rail, .sidebar", FOLD_CONTROL);
    setOverlaid(false);
    opener.current = undefined;
    setFolding("unfolded");
  }, [width]);

  // The sidebar over the page goes when the window takes another layout.
  useEffect(() => setOverlaid(false), [width, pinned]);

  useEffect(() => {
    const keyed = (event: KeyboardEvent) => {
      if (!isShortcut(event) || event.isComposing) {
        return;
      }
      event.preventDefault();
      if (width === "narrow") {
        if (overlaid) {
          close();
        } else if (document.activeElement instanceof HTMLElement) {
          open(document.activeElement);
        }
      } else if (pinned) {
        fold();
      } else {
        unfold();
      }
    };
    addEventListener("keydown", keyed);
    return () => removeEventListener("keydown", keyed);
  }, [width, pinned, overlaid, open, close, fold, unfold]);

  return { width, pinned, overlaid, open, close, fold, unfold };
}
