import { useMemo } from "react";
import "./formula.css";

// Formulas are set as MathML, which the browser draws itself, from the LaTeX a document
// holds. The LaTeX read here is the part of it that papers write inline and in short displays;
// a formula that uses anything else is shown as its LaTeX.

const GREEK: Record<string, string> = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ϵ", varepsilon: "ε", zeta: "ζ",
  eta: "η", theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ", lambda: "λ", mu: "μ", nu: "ν",
  xi: "ξ", pi: "π", varpi: "ϖ", rho: "ρ", varrho: "ϱ", sigma: "σ", varsigma: "ς", tau: "τ",
  upsilon: "υ", phi: "ϕ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω", Gamma: "Γ", Delta: "Δ",
  Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ", Upsilon: "Υ", Phi: "Φ", Psi: "Ψ",
  Omega: "Ω",
};
const OPERATORS: Record<string, string> = {
  cdot: "⋅", times: "×", div: "÷", pm: "±", mp: "∓", leq: "≤", le: "≤", geq: "≥", ge: "≥",
  neq: "≠", ne: "≠", approx: "≈", sim: "∼", simeq: "≃", equiv: "≡", propto: "∝", in: "∈",
  notin: "∉", subset: "⊂", subseteq: "⊆", supset: "⊃", cup: "∪", cap: "∩", to: "→",
  rightarrow: "→", leftarrow: "←", Rightarrow: "⇒", Leftarrow: "⇐", leftrightarrow: "↔",
  mapsto: "↦", sum: "∑", prod: "∏", int: "∫", oint: "∮", infty: "∞", partial: "∂", nabla: "∇",
  forall: "∀", exists: "∃", ldots: "…", cdots: "⋯", dots: "…", vdots: "⋮", ddots: "⋱",
  top: "⊤", perp: "⊥", circ: "∘", ast: "∗", star: "⋆", oplus: "⊕", otimes: "⊗", odot: "⊙",
  langle: "⟨", rangle: "⟩", lvert: "|", rvert: "|", vert: "|", Vert: "‖", mid: "∣",
  wedge: "∧", vee: "∨", neg: "¬", lfloor: "⌊", rfloor: "⌋", lceil: "⌈", rceil: "⌉",
  prime: "′", imath: "ı", jmath: "ȷ", ll: "≪", gg: "≫", backslash: "∖", setminus: "∖", emptyset: "∅",
  "{": "{", "}": "}", "|": "‖", "%": "%", "#": "#", "&": "&", _: "_", $: "$",
};
const FUNCTIONS = new Set([
  "log", "ln", "exp", "sin", "cos", "tan", "max", "min", "arg", "lim", "sup", "inf", "det",
  "dim", "ker", "deg", "gcd", "Pr", "argmax", "argmin", "softmax", "sinh", "cosh", "tanh",
]);
const VARIANTS: Record<string, string> = {
  mathrm: "normal", mathbf: "bold", mathit: "italic", mathcal: "script", mathbb: "double-struck",
  mathsf: "sans-serif", mathtt: "monospace", boldsymbol: "bold-italic", bm: "bold-italic",
  mathscr: "script", mathfrak: "fraktur", operatorname: "normal", textbf: "bold",
};
const TEXTS = new Set(["text", "textrm", "textit", "mbox"]);
const ACCENTS: Record<string, string> = {
  hat: "^", widehat: "^", bar: "¯", overline: "¯", tilde: "~", widetilde: "~", vec: "→",
  dot: "˙", ddot: "¨",
};
const SPACES: Record<string, string> = {
  ",": "0.17em", ":": "0.22em", ";": "0.28em", "!": "-0.17em", quad: "1em", qquad: "2em",
  " ": "0.25em",
};
const SIZES = new Set(["big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr"]);
const OPERATOR_CHARACTERS = new Set([..."+-=<>,;:!/|()[]*'.?"]);

class UnreadFormula extends Error {}

// Tables whose cells are read as a grid; the columns' alignment of an array is passed over.
const TABLES = new Set([
  "array", "matrix", "pmatrix", "bmatrix", "vmatrix", "cases", "aligned", "align", "align*",
  "split", "gathered", "gather", "gather*", "equation", "equation*",
]);
const WITH_COLUMNS = new Set(["array"]);
// Two things set one over the other: \overset{above}{base}, and \stackrel likewise.
const OVER = new Set(["overset", "stackrel"]);

function ends(token: Token): boolean {
  return "command" in token
    ? token.command === "right" || token.command === "\\" || token.command === "end"
    : token.character === "&";
}

type Token = { command: string } | { character: string };

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

function escaped(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Reads LaTeX into MathML, from tokens, one construct at a time.
class Reading {
  private at = 0;

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

  // A sequence until the group, or the formula, ends.
  sequence(closing?: string): string {
    const parts: string[] = [];
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
    return parts.length === 1 ? (parts[0] ?? "") : `<mrow>${parts.join("")}</mrow>`;
  }

  // Whether every token has been read.
  finished(): boolean {
    this.skipSpace();
    return this.peek() === undefined;
  }

  // A table: rows parted by \\, cells by &, until \end and its name.
  private table(): string {
    const rows: string[] = [];
    for (;;) {
      const cells: string[] = [];
      for (;;) {
        cells.push(`<mtd>${this.sequence()}</mtd>`);
        const next = this.peek();
        if (next !== undefined && "character" in next && next.character === "&") {
          this.at += 1;
          continue;
        }
        break;
      }
      rows.push(`<mtr>${cells.join("")}</mtr>`);
      const next = this.peek();
      this.at += 1;
      if (next === undefined) {
        throw new UnreadFormula("a table is not ended");
      }
      if ("command" in next && next.command === "end") {
        this.rawGroup();
        return `<mtable>${rows.join("")}</mtable>`;
      }
      if (!("command" in next) || next.command !== "\\") {
        throw new UnreadFormula("a table's row ends in what is not read");
      }
    }
  }

  // An atom with what is written below and above it.
  private scripted(): string {
    const base = this.atom();
    let below: string | undefined;
    let above: string | undefined;
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
        above = "<mo>′</mo>";
      } else {
        break;
      }
    }
    if (below !== undefined && above !== undefined) {
      return `<msubsup>${base}${below}${above}</msubsup>`;
    }
    if (below !== undefined) {
      return `<msub>${base}${below}</msub>`;
    }
    if (above !== undefined) {
      return `<msup>${base}${above}</msup>`;
    }
    return base;
  }

  // One atom, or one group in braces.
  private argument(): string {
    this.skipSpace();
    return this.atom();
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
        text += OPERATORS[next.command] ?? GREEK[next.command] ?? next.command;
      }
    }
    throw new UnreadFormula("a group is not closed");
  }

  private delimiter(): string {
    this.skipSpace();
    const next = this.peek();
    this.at += 1;
    if (next === undefined) {
      throw new UnreadFormula("a delimiter is missing");
    }
    if ("character" in next) {
      return next.character === "." ? "" : `<mo stretchy="true">${escaped(next.character)}</mo>`;
    }
    const symbol = OPERATORS[next.command];
    if (symbol === undefined) {
      throw new UnreadFormula(`\\${next.command} is no delimiter`);
    }
    return `<mo stretchy="true">${escaped(symbol)}</mo>`;
  }

  private atom(): string {
    const next = this.peek();
    if (next === undefined) {
      throw new UnreadFormula("something is missing");
    }
    this.at += 1;
    if ("character" in next) {
      const character = next.character;
      if (character === "{") {
        return this.sequence("}");
      }
      if (/[0-9]/.test(character)) {
        let number = character;
        for (let more = this.peek(); more !== undefined && "character" in more && /[0-9.]/.test(more.character); more = this.peek()) {
          number += more.character;
          this.at += 1;
        }
        return `<mn>${number}</mn>`;
      }
      if (/\p{L}/u.test(character)) {
        return `<mi>${escaped(character)}</mi>`;
      }
      if (OPERATOR_CHARACTERS.has(character)) {
        return `<mo>${escaped(character)}</mo>`;
      }
      if (character === "~") {
        return `<mspace width="0.25em"/>`;
      }
      throw new UnreadFormula(`${character} is not read`);
    }
    const name = next.command;
    if (name === "frac" || name === "dfrac" || name === "tfrac") {
      return `<mfrac>${this.argument()}${this.argument()}</mfrac>`;
    }
    if (name === "sqrt") {
      this.skipSpace();
      const index = this.peek();
      if (index !== undefined && "character" in index && index.character === "[") {
        this.at += 1;
        const degree = this.sequence("]");
        return `<mroot>${this.argument()}${degree}</mroot>`;
      }
      return `<msqrt>${this.argument()}</msqrt>`;
    }
    if (name === "left") {
      const opening = this.delimiter();
      const inside = this.sequence();
      const right = this.peek();
      if (right === undefined || !("command" in right) || right.command !== "right") {
        throw new UnreadFormula("\\left has no \\right");
      }
      this.at += 1;
      return `<mrow>${opening}${inside}${this.delimiter()}</mrow>`;
    }
    if (name === "begin") {
      const environment = this.rawGroup();
      if (!TABLES.has(environment)) {
        throw new UnreadFormula(`${environment} is not read`);
      }
      if (WITH_COLUMNS.has(environment)) {
        this.rawGroup();
      }
      return this.table();
    }
    if (name === "tag") {
      return `<mspace width="2em"/><mtext>(${escaped(this.rawGroup())})</mtext>`;
    }
    if (OVER.has(name)) {
      const above = this.argument();
      return `<mover>${this.argument()}${above}</mover>`;
    }
    if (name === "underset") {
      const below = this.argument();
      return `<munder>${this.argument()}${below}</munder>`;
    }
    if (TEXTS.has(name)) {
      return `<mtext>${escaped(this.rawGroup())}</mtext>`;
    }
    const variant = VARIANTS[name];
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
        return `<mstyle mathvariant="${variant}">${this.sequence("}")}</mstyle>`;
      }
      return `<mstyle mathvariant="${variant}">${this.atom()}</mstyle>`;
    }
    const accent = ACCENTS[name];
    if (accent !== undefined) {
      return `<mover accent="true">${this.argument()}<mo>${accent}</mo></mover>`;
    }
    if (name === "underline") {
      return `<munder>${this.argument()}<mo>_</mo></munder>`;
    }
    const greek = GREEK[name];
    if (greek !== undefined) {
      return `<mi>${greek}</mi>`;
    }
    const symbol = OPERATORS[name];
    if (symbol !== undefined) {
      return `<mo>${escaped(symbol)}</mo>`;
    }
    if (FUNCTIONS.has(name)) {
      return `<mi>${name}</mi>`;
    }
    const space = SPACES[name];
    if (space !== undefined) {
      return `<mspace width="${space}"/>`;
    }
    if (SIZES.has(name)) {
      return this.atom();
    }
    throw new UnreadFormula(`\\${name} is not read`);
  }
}

// The MathML of a formula, or nothing where its LaTeX uses what is not read here.
export function mathOf(latex: string, display: boolean): string | undefined {
  try {
    const reading = new Reading(tokens(latex));
    const body = reading.sequence();
    if (!reading.finished()) {
      return undefined;
    }
    return `<math display="${display ? "block" : "inline"}">${body}</math>`;
  } catch (error) {
    if (error instanceof UnreadFormula) {
      return undefined;
    }
    throw error;
  }
}

// What tells a formula's drawing apart from text: the code that resolves anchors passes over
// what lies in an element carrying it, and reads the formula's LaTeX beside it instead.
export const DRAWN_ATTRIBUTE = "data-drawn";

// A formula: its LaTeX, kept in the page as text but out of sight, so that a block's element
// still holds exactly its text, and beside it the formula as it is set. One that cannot be
// set is its LaTeX, in sight.
export function Formula({ latex, display }: { latex: string; display: boolean }) {
  const math = useMemo(() => mathOf(latex, display), [latex, display]);
  if (math === undefined) {
    return <code className="formula">{latex}</code>;
  }
  return (
    <span className={display ? "formula display" : "formula"} title={latex}>
      <span className="formula-source" aria-hidden="true">
        {latex}
      </span>
      <span {...{ [DRAWN_ATTRIBUTE]: "" }} dangerouslySetInnerHTML={{ __html: math }} />
    </span>
  );
}
