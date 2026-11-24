import { useSyncExternalStore } from "react";

// What the column tells a screen reader of things that arrive without the reader asking: a
// highlight opened from the source, an excerpt a note took. Each saying is told once, and the
// same words said again are told again.
type Said = { text: string; saying: number };

const listeners = new Set<() => void>();
let said: Said = { text: "", saying: 0 };

function onChange(notify: () => void): () => void {
  listeners.add(notify);
  return () => {
    listeners.delete(notify);
  };
}

export function say(text: string): void {
  said = { text, saying: said.saying + 1 };
  listeners.forEach((notify) => notify());
}

// Drawn once, in the column. A saying is a node of its own, so that a screen reader hears
// words that are the same as the last.
export function Heard() {
  const { text, saying } = useSyncExternalStore(onChange, () => said);
  return (
    <p className="heard-aloud" role="status">
      <span key={saying}>{text}</span>
    </p>
  );
}
