import { useSyncExternalStore } from "react";
import "./heard.css";

// What the column tells a screen reader of things that arrive without the reader asking: a
// highlight opened from the source, an excerpt a note took. Each announcement is told once,
// and the same words announced again are told again.
type Announced = { text: string; count: number };

const listeners = new Set<() => void>();
let announced: Announced = { text: "", count: 0 };

function onChange(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

export function announce(text: string): void {
  announced = { text, count: announced.count + 1 };
  listeners.forEach((notify) => notify());
}

// Drawn once, in the column. An announcement is a node of its own, so that a screen reader
// hears words that are the same as the last.
export function Announcer() {
  const { text, count } = useSyncExternalStore(onChange, () => announced);
  return (
    <p className="announced heard" role="status">
      <span key={count}>{text}</span>
    </p>
  );
}
