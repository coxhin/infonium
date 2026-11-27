import { useEffect, useLayoutEffect, useRef, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import "./menu.css";

// Where a popup opens: under the control that opened it, or, for one opened by a press of the
// pointer's other button, where that press was. The keyboard goes back to the control when the
// popup is left by a key.
export type Opening = { opener: HTMLElement; at: { x: number; y: number } | undefined };

// How far a popup keeps from the edges of the window, in pixels.
const MARGIN = 8;
const MENU_ITEM = '[role^="menuitem"]:not([aria-disabled="true"])';
// What Tab moves to in a question: its fields and presses, in the order they stand.
const TABBABLE = [
  "button:not(:disabled)",
  "input:not(:disabled)",
  "select:not(:disabled)",
  "textarea:not(:disabled)",
  "a[href]",
  '[tabindex]:not([tabindex="-1"])',
].join(", ");

export function openingAt(opener: HTMLElement): Opening {
  return { opener, at: undefined };
}

// A context menu that a key opened (the context menu key, or Shift+F10 where the browser makes
// one of it) comes at no place of the pointer's, and stands under the control.
export function openingOfContextMenu(opener: HTMLElement, x: number, y: number): Opening {
  return { opener, at: x === 0 && y === 0 ? undefined : { x, y } };
}

// Puts the popup under the place it opens at, or above it where the window has no room below,
// never past an edge of the window. It is placed before it is first drawn, so that it is never
// seen elsewhere, and is visible, and so can take the keyboard, from the start.
function place(popup: HTMLElement, opening: Opening): void {
  popup.style.maxHeight = `${innerHeight - 2 * MARGIN}px`;
  const size = popup.getBoundingClientRect();
  const box = opening.opener.getBoundingClientRect();
  const { x, below, above } =
    opening.at === undefined
      ? { x: box.left, below: box.bottom + 2, above: box.top - 2 }
      : { x: opening.at.x, below: opening.at.y, above: opening.at.y };
  const top =
    below + size.height <= innerHeight - MARGIN ? below : Math.max(MARGIN, above - size.height);
  popup.style.left = `${Math.max(MARGIN, Math.min(x, innerWidth - size.width - MARGIN))}px`;
  popup.style.top = `${top}px`;
}

function menuItems(own: HTMLElement): HTMLElement[] {
  return [...own.querySelectorAll<HTMLElement>(MENU_ITEM)];
}

// Moves the keyboard among the items of a menu: ↑ and ↓ to the one before and after, round the
// ends, Home and End to the first and last.
function movedInMenu(event: KeyboardEvent<HTMLElement>, own: HTMLElement): boolean {
  const items = menuItems(own);
  const at = items.indexOf(document.activeElement as HTMLElement);
  const steps: Record<string, number | undefined> = {
    ArrowDown: at + 1,
    ArrowUp: at - 1,
    Home: 0,
    End: items.length - 1,
  };
  const step = steps[event.key];
  if (step === undefined || items.length === 0) {
    return false;
  }
  items[(step + items.length) % items.length]?.focus();
  return true;
}

// Moves the keyboard in a question, by Tab and Shift+Tab, to the part after or before the one it
// is on, round the ends, so that it stays in the question until the question is left.
function tabbedInQuestion(event: KeyboardEvent<HTMLElement>, own: HTMLElement): void {
  const parts = [...own.querySelectorAll<HTMLElement>(TABBABLE)];
  const at = parts.indexOf(document.activeElement as HTMLElement);
  const step = event.shiftKey ? -1 : 1;
  const next = at < 0 ? (event.shiftKey ? parts.length - 1 : 0) : at + step;
  parts[(next + parts.length) % parts.length]?.focus();
}

// A popup over the page, beside what opened it: a menu, or a question asked before a press is
// sent. The keyboard goes to its first item, or to the button marked autoFocus, or stays in the
// field that took it. Escape leaves it for the control that opened it, and so does Tab a menu;
// in a question, Tab and Shift+Tab go round its parts. A press outside it leaves it where the
// press was.
export function Popup({
  opening,
  role,
  label,
  onLeave,
  children,
}: {
  opening: Opening;
  role: "menu" | "dialog";
  label: string;
  // Back to the opener, or not, where the reader pressed somewhere else.
  onLeave: (refocus: boolean) => void;
  children: ReactNode;
}) {
  const own = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    if (own.current !== null) {
      place(own.current, opening);
    }
  }, [opening]);

  useEffect(() => {
    const element = own.current;
    if (element === null) {
      return undefined;
    }
    // A part that took the keyboard as it opened, as a field, keeps it.
    if (!element.contains(document.activeElement)) {
      const first =
        element.querySelector<HTMLElement>("[data-autofocus]") ?? menuItems(element)[0] ?? element;
      first.focus({ preventScroll: true });
    }
    const pressed = (event: PointerEvent) => {
      if (!(event.target instanceof Node && element.contains(event.target))) {
        onLeave(false);
      }
    };
    // A window that changes its size moves the opener away from where the popup stands.
    const resized = () => onLeave(false);
    addEventListener("pointerdown", pressed, true);
    addEventListener("resize", resized);
    return () => {
      removeEventListener("pointerdown", pressed, true);
      removeEventListener("resize", resized);
    };
  }, []);

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape" || (event.key === "Tab" && role === "menu")) {
      event.preventDefault();
      event.stopPropagation();
      onLeave(true);
      return;
    }
    if (event.key === "Tab" && own.current !== null) {
      event.preventDefault();
      event.stopPropagation();
      tabbedInQuestion(event, own.current);
      return;
    }
    const inField = event.target instanceof HTMLInputElement;
    if (role === "menu" && !inField && own.current !== null && movedInMenu(event, own.current)) {
      event.preventDefault();
    }
  }

  return createPortal(
    <div
      ref={own}
      className={role === "menu" ? "popup menu" : "popup question"}
      role={role}
      aria-label={label}
      aria-modal={role === "dialog" ? true : undefined}
      tabIndex={-1}
      onKeyDown={onKeyDown}
    >
      {children}
    </div>,
    document.body,
  );
}

// An item of a menu, which the keyboard reaches by the arrows, not by Tab.
export function MenuItem({
  onChoose,
  checked,
  disabled,
  children,
}: {
  onChoose: () => void;
  checked?: boolean;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      role={checked === undefined ? "menuitem" : "menuitemradio"}
      aria-checked={checked}
      aria-disabled={disabled}
      tabIndex={-1}
      onClick={() => !disabled && onChoose()}
    >
      {children}
    </button>
  );
}

// Opens a menu from the keyboard as the context menu key and Shift+F10 do, the second as
// browsers that give no contextmenu event for it.
export function isMenuKey(event: KeyboardEvent): boolean {
  return event.key === "ContextMenu" || (event.key === "F10" && event.shiftKey);
}
