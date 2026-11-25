import "./glowing.css";

// How a passage pointed at is shown where it stands: it glows for a moment and fades away, so
// that the reader's eye is led to it and the page is left as it was. Words are made to glow
// by the browser's highlights, here; boxes on a PDF's page by elements that fade by
// themselves, as their stylesheet says (pageglow.tsx), over the same time.

// The highlight a glow begins in, and those it fades through, each lighter than the last.
const GLOWING = "anchor";
const FADING = ["anchor-fading-1", "anchor-fading-2", "anchor-fading-3", "anchor-fading-4"];
// How long a glow stays whole, and how long each of its fading steps lasts, in milliseconds.
// The boxes of a page take the same time: 1,400 ms whole, then 500 ms fading.
const WHOLE = 1400;
const STEP = 100;
// The glow is drawn above the reader's highlights (textmarks.ts), so that it stays the stronger
// where the two lie over each other.
const GLOW_PRIORITY = 10;

let glowing: Highlight | undefined;
let timers: number[] = [];

// Ends the glow on show, if one is.
export function endGlow(): void {
  timers.forEach((timer) => window.clearTimeout(timer));
  timers = [];
  glowing = undefined;
  for (const name of [GLOWING, ...FADING]) {
    CSS.highlights.delete(name);
  }
}

// Makes the words of the ranges glow, and ends the glow before it.
export function glowOver(ranges: Range[]): void {
  endGlow();
  if (ranges.length === 0) {
    return;
  }
  const highlight = new Highlight(...ranges);
  highlight.priority = GLOW_PRIORITY;
  glowing = highlight;
  CSS.highlights.set(GLOWING, highlight);
  [GLOWING, ...FADING].forEach((name, step) => {
    timers.push(
      window.setTimeout(
        () => {
          CSS.highlights.delete(name);
          const next = FADING[step];
          if (next === undefined) {
            glowing = undefined;
          } else {
            CSS.highlights.set(next, highlight);
          }
        },
        WHOLE + step * STEP,
      ),
    );
  });
}

// Adds words to the glow on show, which fade with it: words of the passage that come on show
// after it began, as the translation of a page drawn later. A glow that has faded is not lit
// again.
export function glowAlso(ranges: Range[]): void {
  for (const range of ranges) {
    glowing?.add(range);
  }
}
