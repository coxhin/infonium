import katex, { type KatexOptions } from "katex";
import "katex/dist/katex.min.css";

// KaTeX and the stylesheet it sets formulas in. This module is fetched apart from the rest of
// the app, by formula.tsx, when a PDF is first opened or a formula is first to be shown.

// The largest size, in ems, that a formula may give to anything it sizes itself: a rule, a
// space, a raise, the gap between rows. A paper's formula sizes its spaces in a few ems.
const LARGEST_SIZE = 10;

const OPTIONS = {
  // HTML alone: a hidden copy in MathML beside it would be copied with every selection across
  // the formula. The element says the formula's LaTeX to a screen reader instead.
  output: "html",
  throwOnError: true,
  // LaTeX that KaTeX sets though LaTeX itself would not, such as a letter it has no metrics
  // for, is set without a word to the console.
  strict: "ignore",
  // Behind the refusal in formula.tsx: a command that needs trust is drawn as text and does
  // nothing.
  trust: false,
  maxSize: LARGEST_SIZE,
} as const satisfies KatexOptions;

// The HTML of a formula as KaTeX sets it, or nothing where KaTeX refuses it.
export function drawn(latex: string, display: boolean): string | undefined {
  try {
    return katex.renderToString(latex, { ...OPTIONS, displayMode: display });
  } catch {
    return undefined;
  }
}
