import katex, { type KatexOptions } from "katex";
import "katex/dist/katex.min.css";
import { useMemo } from "react";
import "./formula.css";

// Formulas are set by KaTeX, as HTML in its own fonts, from the LaTeX a document holds. A
// formula KaTeX cannot set is shown as its LaTeX.

// What tells a formula in the page from text: the element that draws it carries its LaTeX in
// this attribute, and the code that resolves anchors takes that element for one piece of the
// block's text, its LaTeX, every offset of which lies at the element.
export const FORMULA_ATTRIBUTE = "data-latex";

// A document is untrusted, and so is every formula in it. None may load an address, set a class,
// an id, a style or an attribute of its own (KaTeX draws such a command, untrusted, as red
// text), define a command, which is the one way a formula's text grows past its own length as
// it is expanded, or write to the console. A formula that writes one of these commands is shown
// as its LaTeX; the formulas of papers write none of them. The commands are found in the text
// as KaTeX reads them, a backslash and the letters and @ that follow it. They are not refused
// inside KaTeX, where \tag defines a command of KaTeX's own.
const REFUSED_COMMANDS = new RegExp(
  `\\\\(?:${[
    "href",
    "url",
    "includegraphics",
    "htmlClass",
    "htmlId",
    "htmlStyle",
    "htmlData",
    "def",
    "gdef",
    "edef",
    "xdef",
    "global",
    "long",
    "let",
    "futurelet",
    "newcommand",
    "renewcommand",
    "providecommand",
    "message",
    "errmessage",
    "show",
  ].join("|")})(?![A-Za-z@])`,
);

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
  // Behind the refusal above: a command that needs trust is drawn as text and does nothing.
  trust: false,
  maxSize: LARGEST_SIZE,
} as const satisfies KatexOptions;

// The HTML of a formula as KaTeX sets it, or nothing where it cannot be set. A formula is a
// document's text, and no text may take down the reader that shows it, so a fault in setting
// it, of any kind, leaves it shown as its LaTeX.
export function drawingOf(latex: string, display: boolean): string | undefined {
  if (REFUSED_COMMANDS.test(latex)) {
    return undefined;
  }
  try {
    return katex.renderToString(latex, { ...OPTIONS, displayMode: display });
  } catch {
    return undefined;
  }
}

// A formula, drawn at once with the rest of its block as one element that says its LaTeX of
// itself, to the walk that resolves anchors and to a screen reader. One that cannot be set is
// its LaTeX, and says it too.
export function Formula({ latex, display }: { latex: string; display: boolean }) {
  const drawing = useMemo(() => drawingOf(latex, display), [latex, display]);
  const said = { [FORMULA_ATTRIBUTE]: latex };
  if (drawing === undefined) {
    return (
      <code className="formula" {...said}>
        {latex}
      </code>
    );
  }
  return (
    <span
      {...said}
      className={display ? "formula display" : "formula"}
      role="math"
      aria-label={latex}
      title={latex}
      dangerouslySetInnerHTML={{ __html: drawing }}
    />
  );
}
