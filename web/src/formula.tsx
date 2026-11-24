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

// The pieces of a formula's text as KaTeX reads them: a command, which is a backslash and the
// letters and @ that follow it, or a backslash and one other character, as \\ is; else one
// character. A comment is read as text, so a command in it is found as well.
function piecesOf(latex: string): string[] {
  return latex.match(/\\(?:[A-Za-z@]+|[^])|[^]/gu) ?? [];
}

// A document is untrusted, and so is every formula in it. None may load an address, set a class,
// an id, a style or an attribute of its own (KaTeX draws such a command, untrusted, as red
// text), define a command, which is the one way a formula's text grows past its own length as
// it is expanded, or write to the console. A formula that writes one of these commands is shown
// as its LaTeX; the formulas of papers write none of them. They are refused in the formula's
// text, not inside KaTeX, where \tag defines a command of KaTeX's own.
const REFUSED_COMMANDS = new Set([
  "\\href",
  "\\url",
  "\\includegraphics",
  "\\htmlClass",
  "\\htmlId",
  "\\htmlStyle",
  "\\htmlData",
  "\\def",
  "\\gdef",
  "\\edef",
  "\\xdef",
  "\\global",
  "\\long",
  "\\let",
  "\\futurelet",
  "\\newcommand",
  "\\renewcommand",
  "\\providecommand",
  "\\message",
  "\\errmessage",
  "\\show",
]);

// The commands that set their group as text, in which a script cannot stand.
const TEXT_COMMANDS = new Set([
  "\\text",
  "\\textrm",
  "\\textsf",
  "\\texttt",
  "\\textnormal",
  "\\textbf",
  "\\textmd",
  "\\textit",
  "\\textup",
  "\\emph",
]);
const SCRIPTS = new Set(["_", "^"]);
const SPACE = /^\s$/u;

// The formula with its scripts taken out of its text: the engine that reads PDFs writes
// \text{where \text{head} _{i} = …}, which neither LaTeX nor KaTeX sets, for
// \text{where \text{head}}_{i} = …. A script closes every group of text it stands in, a group
// within a group as well, and what followed it inside them stands after them, outside. A space
// before the script is the engine's, and would part the script from what it is set on. Nothing
// where the braces do not match.
export function withScriptsOutOfText(latex: string): string | undefined {
  const groups: { text: boolean; closed: boolean }[] = [];
  const written: string[] = [];
  let opensText = false;
  for (const piece of piecesOf(latex)) {
    if (piece === "{") {
      groups.push({ text: opensText, closed: false });
    } else if (piece === "}") {
      const group = groups.pop();
      if (group === undefined) {
        return undefined;
      }
      if (group.closed) {
        opensText = false;
        continue;
      }
    } else if (SCRIPTS.has(piece)) {
      const left = groups.slice(openTextFrom(groups));
      if (left.length > 0) {
        while (SPACE.test(written.at(-1) ?? "")) {
          written.pop();
        }
      }
      for (const group of left) {
        written.push("}");
        group.closed = true;
      }
    }
    if (!SPACE.test(piece)) {
      opensText = TEXT_COMMANDS.has(piece);
    }
    written.push(piece);
  }
  return groups.length === 0 ? written.join("") : undefined;
}

// Where the open groups of text begin that the innermost place stands in, one within another:
// after the last group that is no text, or is closed already.
function openTextFrom(groups: { text: boolean; closed: boolean }[]): number {
  let from = groups.length;
  while (from > 0 && groups[from - 1]?.text === true && groups[from - 1]?.closed === false) {
    from -= 1;
  }
  return from;
}

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

function drawn(latex: string, display: boolean): string | undefined {
  try {
    return katex.renderToString(latex, { ...OPTIONS, displayMode: display });
  } catch {
    return undefined;
  }
}

// The HTML of a formula as KaTeX sets it, or nothing where it cannot be set. A formula is a
// document's text, and no text may take down the reader that shows it, so a fault in setting
// it, of any kind, leaves it shown as its LaTeX. A formula KaTeX refuses as written is tried
// once more with its scripts taken out of its text; one it sets as written is never altered.
export function drawingOf(latex: string, display: boolean): string | undefined {
  if (piecesOf(latex).some((piece) => REFUSED_COMMANDS.has(piece))) {
    return undefined;
  }
  const asWritten = drawn(latex, display);
  if (asWritten !== undefined) {
    return asWritten;
  }
  const repaired = withScriptsOutOfText(latex);
  return repaired === undefined || repaired === latex ? undefined : drawn(repaired, display);
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
