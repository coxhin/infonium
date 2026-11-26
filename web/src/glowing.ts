import "./glowing.css";

// How a passage pointed at is shown where it stands: it glows for a moment and fades away, so
// that the reader's eye is led to it and the page is left as it was. Words are made to glow
// by the browser's highlights, here; boxes on a PDF's page by elements that fade by
// themselves (pageglow.tsx), over the same times.

// How long a glow stays whole, and how long it then takes to fade, in milliseconds.
export const GLOW_WHOLE = 1400;
export const GLOW_FADE = 500;
export const GLOW_LASTS = GLOW_WHOLE + GLOW_FADE;

// The highlight a glow begins in, and those it fades through, each lighter than the last.
const GLOWING = "anchor";
const FADING = ["anchor-fading-1", "anchor-fading-2", "anchor-fading-3", "anchor-fading-4"];
const STEP = GLOW_FADE / FADING.length;
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
        GLOW_WHOLE + step * STEP,
      ),
    );
  });
}

// Puts the glow on show over these ranges instead, as far as it has faded: the words it lay
// over drawn anew, as a translation's text is when its links arrive, or more of them come on
// show. A glow that has faded is not lit again.
export function relight(ranges: Range[]): void {
  if (glowing === undefined) {
    return;
  }
  glowing.clear();
  for (const range of ranges) {
    glowing.add(range);
  }
}
