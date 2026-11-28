import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

// Where the keyboard is, for the lists of the page that hand it on when the row that held it
// goes: a row moved, dissolved, seen or removed is taken from the page with the keyboard in it,
// and the browser leaves the keyboard nowhere.

// The element the keyboard was on last, and how many presses of the pointer the page has had. A
// press makes the element none until the keyboard lands on one: a press on the reader's text
// leaves the keyboard on the page itself, where its keys scroll what was pressed, and that is
// the reader's doing, not a row that went.
let lastHeld: Element | null = null;
let presses = 0;
document.addEventListener("focusin", (event) => {
  if (event.target instanceof Element) {
    lastHeld = event.target;
  }
});
document.addEventListener(
  "pointerdown",
  () => {
    lastHeld = null;
    presses += 1;
  },
  true,
);

// Whether the keyboard has nowhere to be: the element that held it went from the page with it.
export function keyboardIsNowhere(): boolean {
  const onPage = document.activeElement === null || document.activeElement === document.body;
  return onPage && lastHeld !== null && !lastHeld.isConnected;
}

// Whether the element stands in a popup over the page, as a menu or a question.
function isInPopup(element: EventTarget | null): boolean {
  return element instanceof Element && element.closest(".popup") !== null;
}

// The part of a container that holds the keyboard, as the container names its parts, while the
// keyboard is in the container, or in a popup opened from it; nothing once the keyboard, or a
// press, goes elsewhere, so that what the reader does there is never undone.
export function useKeyboardHolder<Part>(
  container: RefObject<HTMLElement | null>,
  partOf: (element: Element) => Part | undefined,
): RefObject<Part | undefined> {
  const holder = useRef<Part>(undefined);
  const latestPartOf = useRef(partOf);
  latestPartOf.current = partOf;
  useEffect(() => {
    const elsewhere = (target: EventTarget | null) =>
      !(target instanceof Node && container.current?.contains(target) === true) &&
      !isInPopup(target);
    const focused = (event: FocusEvent) => {
      if (event.target instanceof Element && container.current?.contains(event.target) === true) {
        holder.current = latestPartOf.current(event.target);
      } else if (elsewhere(event.target)) {
        holder.current = undefined;
      }
    };
    const pressed = (event: PointerEvent) => {
      if (elsewhere(event.target)) {
        holder.current = undefined;
      }
    };
    document.addEventListener("focusin", focused);
    document.addEventListener("pointerdown", pressed, true);
    return () => {
      document.removeEventListener("focusin", focused);
      document.removeEventListener("pointerdown", pressed, true);
    };
  }, [container]);
  return holder;
}

// A row the keyboard is asked to land on, as the row of a subscription just made: a new value
// each time it is asked, which knows how many presses the page had had then.
export type Landing = { key: string; askedAfter: number };

export function landingOn(key: string): Landing {
  return { key, askedAfter: presses };
}

// Whether a press of the reader's came after the landing was asked for: the keyboard is the
// reader's from then on, and the landing is dropped.
export function isOverruled(landing: Landing): boolean {
  return presses > landing.askedAfter;
}

// Hands on each landing asked for anew, once what it was asked with is drawn; one there already
// when the caller is first drawn was asked of another, and is not handed on.
export function useOnLanding(
  landing: Landing | undefined,
  landed: (landing: Landing) => void,
): void {
  const seen = useRef(landing);
  useLayoutEffect(() => {
    if (landing !== undefined && landing !== seen.current) {
      landed(landing);
    }
    seen.current = landing;
  });
}
