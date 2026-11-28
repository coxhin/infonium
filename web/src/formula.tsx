import { useEffect, useMemo, useSyncExternalStore } from "react";
import type { ItemDocument } from "./client";
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

// How KaTeX sets a formula: its HTML, or nothing where KaTeX refuses it.
type Setting = (latex: string, display: boolean) => string | undefined;

// KaTeX, with its stylesheet, is a fifth of the app's script, and most of what the app shows
// holds no formula. It is fetched apart from the rest, once, the first time it is wanted.
let fetchedSetting: Setting | undefined;
let fetching: Promise<void> | undefined;
const waiting = new Set<() => void>();

// Fetches KaTeX unless it is here or on its way. What this returns settles once KaTeX is here
// or could not be fetched. A fetch that failed is not made again: the browser keeps a module's
// failure until the page is loaded again, and the formulas are shown as their LaTeX till then.
export function fetchFormulaSetting(): Promise<void> {
  if (fetching === undefined) {
    // The import stands alone: the build wraps an import, with what is chained on it, in its
    // fetch of the stylesheet the module needs, and a failure of that fetch would pass by
    // handlers chained here.
    const module = import("./formuladrawing");
    fetching = module.then(
      ({ drawn }) => {
        fetchedSetting = drawn;
        for (const told of waiting) {
          told();
        }
      },
      () => undefined,
    );
  }
  return fetching;
}

function onFetched(told: () => void): () => void {
  waiting.add(told);
  return () => waiting.delete(told);
}

function settingNow(): Setting | undefined {
  return fetchedSetting;
}

// Settles once a document can be shown with its formulas set: at once for one that holds no
// formula, else once KaTeX is here or could not be fetched. Shown before that, each formula would
// change from its LaTeX to its drawing, and what the reader looks at would move.
export function formulasReadyFor(itemDocument: ItemDocument): Promise<void> {
  const holdsFormula = itemDocument.blocks.some(
    (block) => "marks" in block && block.marks.some((mark) => mark.kind === "formula"),
  );
  return holdsFormula ? fetchFormulaSetting() : Promise.resolve();
}

// The HTML of a formula as KaTeX sets it, or nothing where it cannot be set. A formula is a
// document's text, and no text may take down the reader that shows it, so a fault in setting
// it, of any kind, leaves it shown as its LaTeX. A formula KaTeX refuses as written is tried
// once more with its scripts taken out of its text; one it sets as written is never altered.
export function drawingOf(setting: Setting, latex: string, display: boolean): string | undefined {
  if (piecesOf(latex).some((piece) => REFUSED_COMMANDS.has(piece))) {
    return undefined;
  }
  const asWritten = setting(latex, display);
  if (asWritten !== undefined) {
    return asWritten;
  }
  const repaired = withScriptsOutOfText(latex);
  return repaired === undefined || repaired === latex ? undefined : setting(repaired, display);
}

// A formula, drawn at once with the rest of its block as one element that says its LaTeX of
// itself, to the walk that resolves anchors and to a screen reader. One that cannot be set is
// its LaTeX, and says it too. So is one shown before KaTeX is here, which the reader of a
// document waits for; a formula shown elsewhere fetches KaTeX itself and is set once it is here.
export function Formula({ latex, display }: { latex: string; display: boolean }) {
  const setting = useSyncExternalStore(onFetched, settingNow);
  const drawing = useMemo(
    () => (setting === undefined ? undefined : drawingOf(setting, latex, display)),
    [setting, latex, display],
  );
  useEffect(() => {
    if (setting === undefined) {
      void fetchFormulaSetting();
    }
  }, [setting]);
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
