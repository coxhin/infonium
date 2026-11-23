import { createElement, useCallback, useMemo, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import "./formula.css";

// Formulas are set as MathML, which the browser draws itself, from the LaTeX a document
// holds. The LaTeX read here is the part of it that papers write inline and in short displays;
// a formula that uses anything else is shown as its LaTeX. The reading builds elements, never
// markup, so nothing of the formula's text reaches the page but as text.

function table(entries: Record<string, string>): ReadonlyMap<string, string> {
  return new Map(Object.entries(entries));
}

const GREEK = table({
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ϵ", varepsilon: "ε", zeta: "ζ",
  eta: "η", theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ", lambda: "λ", mu: "μ", nu: "ν",
  xi: "ξ", pi: "π", varpi: "ϖ", rho: "ρ", varrho: "ϱ", sigma: "σ", varsigma: "ς", tau: "τ",
  upsilon: "υ", phi: "ϕ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω", Gamma: "Γ", Delta: "Δ",
  Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ", Upsilon: "Υ", Phi: "Φ", Psi: "Ψ",
  Omega: "Ω",
});
const OPERATORS = table({
  cdot: "⋅", times: "×", div: "÷", pm: "±", mp: "∓", leq: "≤", le: "≤", geq: "≥", ge: "≥",
  neq: "≠", ne: "≠", approx: "≈", sim: "∼", simeq: "≃", equiv: "≡", propto: "∝", in: "∈",
  notin: "∉", subset: "⊂", subseteq: "⊆", supset: "⊃", cup: "∪", cap: "∩", to: "→",
  rightarrow: "→", leftarrow: "←", Rightarrow: "⇒", Leftarrow: "⇐", leftrightarrow: "↔",
  mapsto: "↦", sum: "∑", prod: "∏", int: "∫", oint: "∮", infty: "∞", partial: "∂", nabla: "∇",
  forall: "∀", exists: "∃", ldots: "…", cdots: "⋯", dots: "…", vdots: "⋮", ddots: "⋱",
  top: "⊤", perp: "⊥", circ: "∘", ast: "∗", star: "⋆", oplus: "⊕", otimes: "⊗", odot: "⊙",
  langle: "⟨", rangle: "⟩", lvert: "|", rvert: "|", vert: "|", Vert: "‖", mid: "∣",
  wedge: "∧", vee: "∨", neg: "¬", lfloor: "⌊", rfloor: "⌋", lceil: "⌈", rceil: "⌉",
  prime: "′", imath: "ı", jmath: "ȷ", ll: "≪", gg: "≫", backslash: "∖", setminus: "∖",
  emptyset: "∅", "{": "{", "}": "}", "|": "‖", "%": "%", "#": "#", "&": "&", _: "_", $: "$",
});
const FUNCTIONS = new Set([
  "log", "ln", "exp", "sin", "cos", "tan", "max", "min", "arg", "lim", "sup", "inf", "det",
  "dim", "ker", "deg", "gcd", "Pr", "argmax", "argmin", "softmax", "sinh", "cosh", "tanh",
]);
const VARIANTS = table({
  mathrm: "normal", mathbf: "bold", mathit: "italic", mathcal: "script", mathbb: "double-struck",
  mathsf: "sans-serif", mathtt: "monospace", boldsymbol: "bold-italic", bm: "bold-italic",
  mathscr: "script", mathfrak: "fraktur", operatorname: "normal", textbf: "bold",
});
const TEXTS = new Set(["text", "textrm", "textit", "mbox"]);
const ACCENTS = table({
  hat: "^", widehat: "^", bar: "¯", overline: "¯", tilde: "~", widetilde: "~", vec: "→",
  dot: "˙", ddot: "¨",
});
const SPACES = table({
  ",": "0.17em", ":": "0.22em", ";": "0.28em", "!": "-0.17em", quad: "1em", qquad: "2em",
  " ": "0.25em",
});
const SIZES = new Set(["big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr"]);
const OPERATOR_CHARACTERS = new Set([..."+-=<>,;:!/|()[]*'.?"]);
// Tables whose cells are read as a grid; the columns' alignment of an array is passed over.
const TABLES = new Set([
  "array", "matrix", "pmatrix", "bmatrix", "vmatrix", "cases", "aligned", "align", "align*",
  "split", "gathered", "gather", "gather*", "equation", "equation*",
]);
const WITH_COLUMNS = new Set(["array"]);
// Two things set one over the other: \overset{above}{base}, and \stackrel likewise.
const OVER = new Set(["overset", "stackrel"]);
// The deepest nesting followed; the formulas of papers nest a few levels.
const DEEPEST = 64;
const TAG_SPACE = "2em";
const TILDE_SPACE = "0.25em";

class UnreadFormula extends Error {}

type Token = { command: string } | { character: string };

function ends(token: Token): boolean {
  return "command" in token
    ? token.command === "right" || token.command === "\\" || token.command === "end"
    : token.character === "&";
}

function tokens(latex: string): Token[] {
  const found: Token[] = [];
  for (let at = 0; at < latex.length; ) {
    const character = latex[at] ?? "";
    if (character === "\\") {
      const name = /^\\([A-Za-z]+|.)/.exec(latex.slice(at));
      if (name === null || name[1] === undefined) {
        throw new UnreadFormula("a backslash ends the formula");
      }
      found.push({ command: name[1] });
      at += name[0].length;
    } else {
      found.push({ character });
      at += 1;
    }
  }
  return found;
}

// A MathML element. Its children are given one by one, so they need no keys.
function math(tag: string, props: Record<string, string> | null, ...children: ReactNode[]): ReactNode {
  return createElement(tag, props, ...children);
}

// Reads LaTeX into MathML elements, from tokens, one construct at a time.
class Reading {
  private at = 0;
  private depth = 0;

  constructor(private readonly found: Token[]) {}

  private peek(): Token | undefined {
    return this.found[this.at];
  }

  private skipSpace(): void {
    let next = this.peek();
    while (next !== undefined && "character" in next && /\s/.test(next.character)) {
      this.at += 1;
      next = this.peek();
    }
  }

  private deeper<Value>(read: () => Value): Value {
    this.depth += 1;
    if (this.depth > DEEPEST) {
      throw new UnreadFormula("the formula nests deeper than is followed");
    }
    const value = read();
    this.depth -= 1;
    return value;
  }

  // A sequence until the group, or the formula, ends.
  sequence(closing?: string): ReactNode {
    return this.deeper(() => {
      const parts: ReactNode[] = [];
      for (;;) {
        this.skipSpace();
        const next = this.peek();
        if (next === undefined) {
          if (closing !== undefined) {
            throw new UnreadFormula("a group is not closed");
          }
          break;
        }
        if ("character" in next && next.character === closing) {
          this.at += 1;
          break;
        }
        // Outside a group, a sequence also ends where a \left's part, a cell or a row of a
        // table ends.
        if (closing === undefined && ends(next)) {
          break;
        }
        parts.push(this.scripted());
      }
      return parts.length === 1 ? parts[0] : math("mrow", null, ...parts);
    });
  }

  // Whether every token has been read.
  finished(): boolean {
    this.skipSpace();
    return this.peek() === undefined;
  }

  // A table: rows parted by \\, cells by &, until \end and its name.
  private table(): ReactNode {
    const rows: ReactNode[] = [];
    for (;;) {
      const cells: ReactNode[] = [];
      for (;;) {
        cells.push(math("mtd", null, this.sequence()));
        const next = this.peek();
        if (next !== undefined && "character" in next && next.character === "&") {
          this.at += 1;
          continue;
        }
        break;
      }
      rows.push(math("mtr", null, ...cells));
      const next = this.peek();
      this.at += 1;
      if (next === undefined) {
        throw new UnreadFormula("a table is not ended");
      }
      if ("command" in next && next.command === "end") {
        this.rawGroup();
        return math("mtable", null, ...rows);
      }
      if (!("command" in next) || next.command !== "\\") {
        throw new UnreadFormula("a table's row ends in what is not read");
      }
    }
  }

  // An atom with what is written below and above it.
  private scripted(): ReactNode {
    const base = this.atom();
    let below: ReactNode | undefined;
    let above: ReactNode | undefined;
    for (;;) {
      this.skipSpace();
      const next = this.peek();
      if (next !== undefined && "character" in next && next.character === "_") {
        this.at += 1;
        below = this.argument();
      } else if (next !== undefined && "character" in next && next.character === "^") {
        this.at += 1;
        above = this.argument();
      } else if (next !== undefined && "character" in next && next.character === "'") {
        this.at += 1;
        above = math("mo", null, "′");
      } else {
        break;
      }
    }
    if (below !== undefined && above !== undefined) {
      return math("msubsup", null, base, below, above);
    }
    if (below !== undefined) {
      return math("msub", null, base, below);
    }
    if (above !== undefined) {
      return math("msup", null, base, above);
    }
    return base;
  }

  // One atom, or one group in braces.
  private argument(): ReactNode {
    this.skipSpace();
    return this.deeper(() => this.atom());
  }

  // The raw text of a group in braces, for text and names.
  private rawGroup(): string {
    this.skipSpace();
    const open = this.peek();
    if (open === undefined || !("character" in open) || open.character !== "{") {
      const single = this.peek();
      this.at += 1;
      return single !== undefined && "character" in single ? single.character : "";
    }
    this.at += 1;
    let depth = 1;
    let text = "";
    for (let next = this.peek(); next !== undefined; next = this.peek()) {
      this.at += 1;
      if ("character" in next) {
        depth += next.character === "{" ? 1 : next.character === "}" ? -1 : 0;
        if (depth === 0) {
          return text;
        }
        text += next.character;
      } else {
        text += OPERATORS.get(next.command) ?? GREEK.get(next.command) ?? next.command;
      }
    }
    throw new UnreadFormula("a group is not closed");
  }

  private delimiter(): ReactNode {
    this.skipSpace();
    const next = this.peek();
    this.at += 1;
    if (next === undefined) {
      throw new UnreadFormula("a delimiter is missing");
    }
    if ("character" in next) {
      return next.character === "." ? null : math("mo", { stretchy: "true" }, next.character);
    }
    const symbol = OPERATORS.get(next.command);
    if (symbol === undefined) {
      throw new UnreadFormula(`\\${next.command} is no delimiter`);
    }
    return math("mo", { stretchy: "true" }, symbol);
  }

  private atom(): ReactNode {
    const next = this.peek();
    if (next === undefined) {
      throw new UnreadFormula("something is missing");
    }
    this.at += 1;
    if ("character" in next) {
      return this.character(next.character);
    }
    return this.command(next.command);
  }

  private character(character: string): ReactNode {
    if (character === "{") {
      return this.sequence("}");
    }
    if (/[0-9]/.test(character)) {
      let number = character;
      for (
        let more = this.peek();
        more !== undefined && "character" in more && /[0-9.]/.test(more.character);
        more = this.peek()
      ) {
        number += more.character;
        this.at += 1;
      }
      return math("mn", null, number);
    }
    if (/\p{L}/u.test(character)) {
      return math("mi", null, character);
    }
    if (OPERATOR_CHARACTERS.has(character)) {
      return math("mo", null, character);
    }
    if (character === "~") {
      return math("mspace", { width: TILDE_SPACE });
    }
    throw new UnreadFormula(`${character} is not read`);
  }

  private command(name: string): ReactNode {
    if (name === "frac" || name === "dfrac" || name === "tfrac") {
      return math("mfrac", null, this.argument(), this.argument());
    }
    if (name === "sqrt") {
      this.skipSpace();
      const index = this.peek();
      if (index !== undefined && "character" in index && index.character === "[") {
        this.at += 1;
        const degree = this.sequence("]");
        return math("mroot", null, this.argument(), degree);
      }
      return math("msqrt", null, this.argument());
    }
    if (name === "left") {
      return this.deeper(() => {
        const opening = this.delimiter();
        const inside = this.sequence();
        const right = this.peek();
        if (right === undefined || !("command" in right) || right.command !== "right") {
          throw new UnreadFormula("\\left has no \\right");
        }
        this.at += 1;
        return math("mrow", null, opening, inside, this.delimiter());
      });
    }
    if (name === "begin") {
      const environment = this.rawGroup();
      if (!TABLES.has(environment)) {
        throw new UnreadFormula(`${environment} is not read`);
      }
      if (WITH_COLUMNS.has(environment)) {
        this.rawGroup();
      }
      return this.deeper(() => this.table());
    }
    if (name === "tag") {
      return math("mrow", null, math("mspace", { width: TAG_SPACE }), math("mtext", null, `(${this.rawGroup()})`));
    }
    if (OVER.has(name)) {
      const above = this.argument();
      return math("mover", null, this.argument(), above);
    }
    if (name === "underset") {
      const below = this.argument();
      return math("munder", null, this.argument(), below);
    }
    if (TEXTS.has(name)) {
      return math("mtext", null, this.rawGroup());
    }
    const variant = VARIANTS.get(name);
    if (variant !== undefined) {
      this.skipSpace();
      // \operatorname* sets its limits above and below, which matters only in a display.
      const starred = this.peek();
      if (starred !== undefined && "character" in starred && starred.character === "*") {
        this.at += 1;
        this.skipSpace();
      }
      const group = this.peek();
      if (group !== undefined && "character" in group && group.character === "{") {
        this.at += 1;
        return math("mstyle", { mathvariant: variant }, this.sequence("}"));
      }
      return math("mstyle", { mathvariant: variant }, this.argument());
    }
    const accent = ACCENTS.get(name);
    if (accent !== undefined) {
      return math("mover", { accent: "true" }, this.argument(), math("mo", null, accent));
    }
    if (name === "underline") {
      return math("munder", null, this.argument(), math("mo", null, "_"));
    }
    const greek = GREEK.get(name);
    if (greek !== undefined) {
      return math("mi", null, greek);
    }
    const symbol = OPERATORS.get(name);
    if (symbol !== undefined) {
      return math("mo", null, symbol);
    }
    if (FUNCTIONS.has(name)) {
      return math("mi", null, name);
    }
    const space = SPACES.get(name);
    if (space !== undefined) {
      return math("mspace", { width: space });
    }
    if (SIZES.has(name)) {
      return this.argument();
    }
    throw new UnreadFormula(`\\${name} is not read`);
  }
}

// The MathML of a formula, or nothing where it cannot be set. A formula is a document's text,
// and no text may take down the reader that shows it, so a fault in reading it, of any kind,
// leaves it shown as its LaTeX.
export function mathOf(latex: string, display: boolean): ReactNode | undefined {
  try {
    const reading = new Reading(tokens(latex));
    const body = reading.sequence();
    if (!reading.finished()) {
      return undefined;
    }
    return math("math", { display: display ? "block" : "inline" }, body);
  } catch {
    return undefined;
  }
}

// What tells a formula's drawing apart from text: the code that resolves anchors passes over
// what lies in an element carrying it, and reads the formula's LaTeX beside it instead.
export const DRAWN_ATTRIBUTE = "data-drawn";

// A formula: its LaTeX, kept in the page as text but out of sight, so that a block's element
// still holds exactly its text and a selection or a copy yields it, and beside it the formula
// as it is set. The drawing stands in a shadow root of its own, which neither a selection nor
// the walk of a block's text enters. One that cannot be set is its LaTeX, in sight.
export function Formula({ latex, display }: { latex: string; display: boolean }) {
  const drawn = useMemo(() => mathOf(latex, display), [latex, display]);
  const [root, setRoot] = useState<ShadowRoot>();
  const host = useCallback((element: HTMLSpanElement | null) => {
    if (element !== null) {
      setRoot(element.shadowRoot ?? element.attachShadow({ mode: "open" }));
    }
  }, []);
  if (drawn === undefined) {
    return <code className="formula">{latex}</code>;
  }
  return (
    <span className={display ? "formula display" : "formula"} title={latex}>
      <span className="formula-source" aria-hidden="true">
        {latex}
      </span>
      <span ref={host} {...{ [DRAWN_ATTRIBUTE]: "" }}>
        {root !== undefined && createPortal(drawn, root)}
      </span>
    </span>
  );
}
