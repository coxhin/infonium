import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import "./cuttext.css";

// One observer for every cut text, which says when its box changes size, as it does when the
// notebook comes on show.
const sizeWatchers = new Map<Element, () => void>();
const sizes = new ResizeObserver((changes) =>
  changes.forEach((changed) => sizeWatchers.get(changed.target)?.()),
);

function watchSize(element: Element, changed: () => void): () => void {
  sizeWatchers.set(element, changed);
  sizes.observe(element);
  return () => {
    sizes.unobserve(element);
    sizeWatchers.delete(element);
  };
}

export type Cut = {
  // Drawn around the text, which it cuts after the given lines until the whole is asked for.
  box: RefObject<HTMLSpanElement | null>;
  whole: boolean;
  // The control that shows the whole text or cuts it again; nothing while the text fits.
  toggle: ReactNode;
};

// A text cut after a few lines, with the rest in reach of a press.
export function useCut(text: string): Cut {
  const box = useRef<HTMLSpanElement>(null);
  const [whole, setWhole] = useState(false);
  const [cut, setCut] = useState(false);
  const watched = useRef<{ element: HTMLElement; stop: () => void }>(undefined);
  // The text may be drawn later than the hook is made, or drawn anew, as a comment is when its
  // field closes: whichever element holds it is watched, and measured again when it changes.
  useLayoutEffect(() => {
    const element = whole ? null : box.current;
    if (watched.current?.element === element) {
      return;
    }
    watched.current?.stop();
    watched.current = undefined;
    if (element !== null) {
      const measure = () => setCut(element.scrollHeight > element.clientHeight + 1);
      watched.current = { element, stop: watchSize(element, measure) };
    }
  });
  useLayoutEffect(() => {
    const element = box.current;
    if (element !== null && !whole) {
      setCut(element.scrollHeight > element.clientHeight + 1);
    }
  }, [text]);
  useEffect(() => () => watched.current?.stop(), []);
  const toggle = (cut || whole) && (
    <button className="cut-toggle" aria-expanded={whole} onClick={() => setWhole(!whole)}>
      {whole ? "收起" : "展开全部"}
    </button>
  );
  return { box, whole, toggle };
}

// The text itself, cut after this many lines.
export function CutText({
  cut,
  lines,
  className,
  children,
}: {
  cut: Cut;
  lines: number;
  className: string;
  children: string;
}) {
  return (
    <span
      ref={cut.box}
      className={`cut-text ${className}`}
      data-whole={cut.whole}
      style={{ WebkitLineClamp: cut.whole ? "none" : lines }}
    >
      {children}
    </span>
  );
}
