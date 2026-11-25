import { pieceStarts, translatedText, type AreaText } from "./areas";
import type { Mark, TranslatedBlock } from "./client";
import type { PageText } from "./pagelines";
import { linkOfTarget, type PageLink } from "./pagelinks";

// The links of a PDF's lines carried over to the translation set over them. A translation is
// made of a block's text without its links, so a link is found again in it by the words it
// lies over: a citation, a year, a number or an address, which a translation keeps as they are,
// or a label such as "Figure 3", which it gives in its own words. Where every time the block
// says those words they are a link to the same place, the translation's every saying of them
// is that link; otherwise a link is carried only where the translation says them as often as
// the source does, and the one it lies over is told by its place among them.

type LinkMark = Extract<Mark, { kind: "link" }>;

// A link of a block's source: the stretch of the block's text, in code points, that it lies
// over, and the address of a link mark that leads where it does.
export type SourceLink = { start: number; end: number; target: string };

// A line belongs to a link's box where this part of its height lies inside the box, and a word
// of the line belongs to the link where this part of its width does.
const OVERLAP = 0.5;
const SPACE = /\s/u;

// The links of a page, by the block whose words each lies over: the words of a line that lie
// inside the link's box for the most part. Lines written downwards, and lines taken whole,
// carry none.
export function sourceLinksOn(
  text: PageText,
  page: number,
  links: PageLink[],
): Map<number, SourceLink[]> {
  const byBlock = new Map<number, SourceLink[]>();
  for (const { box, target } of links) {
    for (const line of text.onPages.get(page) ?? []) {
      const { place } = line;
      const across =
        Math.min(place.top + place.height, box.top + box.height) - Math.max(place.top, box.top);
      if (line.downward || line.whole || across < OVERLAP * place.height) {
        continue;
      }
      const characters = text.textOf(line.blockNumber).slice(line.start, line.end);
      const { edges } = text.cutOf(line);
      const xOf = (at: number) => place.left + (edges[at] ?? (at === 0 ? 0 : 1)) * place.width;
      let first: number | undefined;
      let last: number | undefined;
      for (let at = 0; at < characters.length; ) {
        if (SPACE.test(characters[at] ?? "")) {
          at += 1;
          continue;
        }
        let end = at;
        while (end < characters.length && !SPACE.test(characters[end] ?? "")) {
          end += 1;
        }
        const left = xOf(at);
        const right = xOf(end);
        const inside = Math.min(right, box.left + box.width) - Math.max(left, box.left);
        if (right > left && inside >= OVERLAP * (right - left)) {
          first ??= at;
          last = end;
        }
        at = end;
      }
      if (first !== undefined && last !== undefined) {
        const found = byBlock.get(line.blockNumber) ?? [];
        found.push({
          start: line.start + first,
          end: line.start + last,
          target: linkOfTarget(target),
        });
        byBlock.set(line.blockNumber, found);
      }
    }
  }
  return byBlock;
}

// Marks that bound the words of a link without being part of them.
const OPENING = /^[\s([{（【《“"']+/u;
const CLOSING = /[\s)\]}）】》”"',;:，；：、。]+$/u;
const WORDLIKE = /[\p{L}\p{N}]/u;
const ADDRESS = /https?:\/\/\S+/u;
const PLACE_PREFIX = "#";

function escaped(words: string): string {
  return words.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// A pattern for words as words of their own, not inside longer ones: a number is not found
// inside a longer number, nor a word inside a longer word.
function wordsPattern(words: string): string {
  const before = WORDLIKE.test(words.at(0) ?? "") ? "(?<![\\p{L}\\p{N}.])" : "";
  const after = WORDLIKE.test(words.at(-1) ?? "") ? "(?![\\p{L}\\p{N}]|\\.\\p{N})" : "";
  return `${before}${escaped(words)}${after}`;
}

// Where a pattern stands in a text, as stretches of code points.
function stretchesOf(text: string, pattern: string): { start: number; end: number }[] {
  return [...text.matchAll(new RegExp(pattern, "gu"))]
    .filter((match) => match[0] !== "")
    .map((match) => {
      const start = [...text.slice(0, match.index)].length;
      return { start, end: start + [...match[0]].length };
    });
}

// Labels a paper names its parts by, and how a translation into Chinese says each with its
// number, as patterns around the number.
const LABELS: { source: RegExp; translated: (number: string) => string }[] = [
  { source: /^(?:Figures?|Figs?\.)$/i, translated: (n) => `(?:图示|图)\\s?${n}` },
  { source: /^(?:Tables?|Tabs?\.)$/i, translated: (n) => `(?:表格|表)\\s?${n}` },
  {
    source: /^(?:Sections?|Secs?\.|§|Appendix|Appendices|App\.)$/i,
    translated: (n) => `(?:第\\s?${n}\\s?[节章]|附录\\s?${n}|${n}\\s?节)`,
  },
  {
    source: /^(?:Equations?|Eqs?\.)$/i,
    translated: (n) => `(?:公式|方程|等式|式)\\s?[（(]?\\s?${n}\\s?[）)]?`,
  },
  { source: /^(?:Algorithms?|Algs?\.)$/i, translated: (n) => `算法\\s?${n}` },
  { source: /^(?:Theorems?|Thms?\.)$/i, translated: (n) => `定理\\s?${n}` },
  { source: /^Lemmas?$/i, translated: (n) => `引理\\s?${n}` },
  { source: /^(?:Definitions?|Defs?\.)$/i, translated: (n) => `定义\\s?${n}` },
  { source: /^(?:Chapters?|Chs?\.)$/i, translated: (n) => `第\\s?${n}\\s?章` },
];
// A label's number: digits with their parts, a letter of an appendix, or either in brackets.
const LABEL_NUMBER = /^\(?([A-Z]?\d+(?:\.\d+)*[a-z]?|[A-Z](?:\.\d+)*)\)?$/u;
const LABEL_BEFORE = /([A-Za-z§]+\.?)\s*$/u;
const LABEL_REACH = 16;

// One way of finding a link's words: as a pattern in the source, whose sayings are counted,
// and as patterns in the translation, the first of which that is found is taken.
type Search = { source: string; translated: string[] };

// The ways a link's words are looked for, the surest first.
function searchesFor(source: string, link: SourceLink): Search[] {
  const characters = [...source];
  const raw = characters.slice(link.start, link.end).join("");
  const words = raw.replace(OPENING, "").replace(CLOSING, "");
  const searches: Search[] = [];
  // An address is looked for as itself, with or without the end of a sentence after it.
  const address = link.target.startsWith(PLACE_PREFIX) ? undefined : link.target;
  const shown = ADDRESS.exec(words)?.[0]?.replace(/[.,;:)]+$/u, "");
  for (const each of new Set([shown, address, address?.replace(/\/$/u, "")])) {
    if (each !== undefined && each !== "") {
      searches.push({ source: wordsPattern(each), translated: [wordsPattern(each)] });
    }
  }
  if (words === "" || !WORDLIKE.test(words)) {
    return searches;
  }
  // A label's number alone may be the link, the label standing before it: "Figure 3" with
  // only its 3 linked.
  const bare = words.replace(/\.$/u, "");
  const before = LABEL_BEFORE.exec(
    characters.slice(Math.max(0, link.start - LABEL_REACH), link.start).join(""),
  )?.[1];
  const own = /^(\S+)\s+(\S+)$/u.exec(bare);
  const labelled =
    LABEL_NUMBER.test(bare) && before !== undefined
      ? { label: before, number: LABEL_NUMBER.exec(bare)?.[1], whole: `${before} ${bare}` }
      : own?.[1] !== undefined && own[2] !== undefined
        ? { label: own[1], number: LABEL_NUMBER.exec(own[2])?.[1], whole: bare }
        : undefined;
  const label = LABELS.find((each) => labelled !== undefined && each.source.test(labelled.label));
  if (label !== undefined && labelled?.number !== undefined) {
    searches.push({
      source: wordsPattern(labelled.whole).replace(/ /g, "\\s+"),
      translated: [
        `${label.translated(escaped(labelled.number))}(?![\\p{N}]|\\.\\p{N})`,
        wordsPattern(labelled.whole),
      ],
    });
  }
  // An author's name with "et al." may be given with the translation's own word for the rest.
  const author = /^(\S+(?:\s\S+)?)\s+et\s+al\.?$/u.exec(words)?.[1];
  if (author !== undefined) {
    searches.push({
      source: `${wordsPattern(author)}\\s+et\\s+al`,
      translated: [`${wordsPattern(author)}\\s?(?:et\\s+al\\.?|等人?)`],
    });
  }
  searches.push({ source: wordsPattern(words), translated: [wordsPattern(words)] });
  if (bare !== words) {
    searches.push({ source: wordsPattern(bare), translated: [wordsPattern(bare)] });
  }
  return searches;
}

function overlapping(
  one: { start: number; end: number },
  other: { start: number; end: number },
): boolean {
  return one.start < other.end && other.start < one.end;
}

// Where the translation says the words a link lies over: every saying of them where every
// saying in the source is a link to the same place, or else the one at the link's place among
// them, where the translation says them as often as the source does. An address is found in
// the translation wherever it stands, as it leads where it says.
function carried(
  source: string,
  translation: string,
  link: SourceLink,
  links: SourceLink[],
): { start: number; end: number }[] {
  const toAddress = !link.target.startsWith(PLACE_PREFIX);
  for (const search of searchesFor(source, link)) {
    const inSource = stretchesOf(source, search.source);
    const which = inSource.findIndex((stretch) => overlapping(stretch, link));
    const everyLinked =
      inSource.length > 0 &&
      inSource.every((stretch) =>
        links.some((other) => other.target === link.target && overlapping(other, stretch)),
      );
    if (which < 0 && !toAddress) {
      continue;
    }
    for (const pattern of search.translated) {
      const inTranslation = stretchesOf(translation, pattern);
      if (inTranslation.length === 0) {
        continue;
      }
      if (everyLinked || which < 0) {
        return inTranslation;
      }
      const at = inTranslation[which];
      if (inTranslation.length === inSource.length && at !== undefined) {
        return [at];
      }
    }
  }
  return [];
}

// The link marks a block's translation carries for the links of its source: none over another
// mark's formula or link, and one at most over any word.
export function translatedLinkMarks(
  source: string,
  translation: string,
  links: SourceLink[],
  marks: Mark[],
): LinkMark[] {
  const taken: { start: number; end: number }[] = marks.filter((mark) => mark.kind !== "style");
  const found: LinkMark[] = [];
  for (const link of links) {
    for (const stretch of carried(source, translation, link, links)) {
      if (![...taken, ...found].some((mark) => overlapping(mark, stretch))) {
        found.push({ kind: "link", start: stretch.start, end: stretch.end, target: link.target });
      }
    }
  }
  return found;
}

// An area given the links of its part of the translation, kept for as long as those links stay
// as they are, so that an area whose links did not change is not set again.
const linkedKept = new WeakMap<AreaText, { key: string; linked: AreaText }>();

// The areas of a block's translation, each with the links its part carries of those of the
// source's lines. A link that runs over the end of one area into the next is carried by both.
export function linkedAreas(
  areas: AreaText[],
  source: string,
  translated: TranslatedBlock,
  links: SourceLink[] | undefined,
  gap: string,
): AreaText[] {
  if (links === undefined || links.length === 0) {
    return areas;
  }
  const whole = translatedText(translated, gap);
  const marks = translatedLinkMarks(source, whole.text, links, whole.marks);
  if (marks.length === 0) {
    return areas;
  }
  const starts = pieceStarts(whole.text, areas);
  return areas.map((area, index) => {
    const start = starts[index];
    if (start === undefined) {
      return area;
    }
    const end = start + [...area.piece.text].length;
    const own = marks
      .filter((mark) => mark.start < end && start < mark.end)
      .map((mark) => ({
        ...mark,
        start: Math.max(mark.start, start) - start,
        end: Math.min(mark.end, end) - start,
      }));
    if (own.length === 0) {
      return area;
    }
    const key = own.map((mark) => `${mark.start}-${mark.end} ${mark.target}`).join("|");
    const kept = linkedKept.get(area);
    if (kept?.key === key) {
      return kept.linked;
    }
    const linked = { ...area, piece: { ...area.piece, marks: [...area.piece.marks, ...own] } };
    linkedKept.set(area, { key, linked });
    return linked;
  });
}
