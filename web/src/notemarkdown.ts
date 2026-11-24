import { PASSAGE_LINK } from "./client";

// The Markdown that infonium writes into a note by itself, and what is read out of a note's
// Markdown without the editor. The editor is fetched only when a note is first opened, so what
// the rest of the app needs of a note's Markdown lives here, apart from it.

// The characters that mean something inside a line, escaped as the editor escapes them, so that
// a note written here and later edited does not change where nothing was edited.
const INLINE_MARKS = /[\\`*_[\]~]/g;
// What starts a block when it starts a line of a paragraph: a heading, an item of a list, the
// underline of a heading or a rule.
const HEADING_START = /^#{1,6}(?=[ \t]|$)/;
const ITEM_START = /^[-+*](?=[ \t]|$)/;
const NUMBERED_START = /^(\d{1,9})([.)])(?=[ \t]|$)/;
const UNDERLINE = /^(?:=+|-[-\s]*)$/;
// A line that a table's row or its delimiter row could start with, which would make the lines
// around it a table.
const TABLE_ROW_START = /^\|/;
const TABLE_DELIMITER = /^(?=.*\|)\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;
const WEB_ADDRESS = /https?:\/\/[^\s<>　-〿＀-￯]+/g;
const ADDRESS_END = /[.,;:!?'"]+$/;
const LEADING_SPACE = /^[ \t]+/;
const SPACE_REFERENCES: Record<string, string> = { " ": "&#32;", "\t": "&#9;" };
const FIRST_WORDS = 24;
const LINK = /!?\[([^\]]*)\]\([^)]*\)/g;
const BLOCK_MARKERS = /^\s*(?:#{1,6}\s+|>\s*|[-+*]\s+|\d{1,9}[.)]\s+)*/;
const MARKS = /\\(.)|[`*_~]/g;
const ENTITIES: Record<string, string> = { "&amp;": "&", "&lt;": "<", "&gt;": ">" };
const ENTITY = /&(?:amp|lt|gt);/g;

export function inlineText(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(INLINE_MARKS, "\\$&");
}

// A line of a paragraph as Markdown keeps it a line of that paragraph: what would start a
// block at its head is escaped, and the spaces Markdown drops at the head of a line are kept as
// references to them.
export function paragraphLine(line: string): string {
  const spaced = line.replace(LEADING_SPACE, (spaces) =>
    [...spaces].map((space) => SPACE_REFERENCES[space] ?? space).join(""),
  );
  if (
    HEADING_START.test(spaced) ||
    ITEM_START.test(spaced) ||
    UNDERLINE.test(spaced) ||
    TABLE_ROW_START.test(spaced) ||
    TABLE_DELIMITER.test(spaced)
  ) {
    return `\\${spaced}`;
  }
  return spaced.replace(NUMBERED_START, "$1\\$2");
}

// An address in a statement, which Markdown would take as a link by itself, is written as a
// link to it, so that nothing in it is escaped and it comes back the same. The marks that end
// a sentence are not part of it.
function linkedAddresses(text: string): string {
  let written = "";
  let from = 0;
  for (const found of text.matchAll(WEB_ADDRESS)) {
    const address = found[0].replace(ADDRESS_END, "");
    const at = found.index;
    written += `${inlineText(text.slice(from, at))}[${inlineText(address)}](${address
      .replace(/\(/g, "%28")
      .replace(/\)/g, "%29")})`;
    from = at + address.length;
  }
  return written + inlineText(text.slice(from));
}

export function paragraph(text: string): string {
  return text.split("\n").map(paragraphLine).join("\n");
}

export function passageLink(words: string, passageId: string): string {
  return `[${words}](${PASSAGE_LINK}${passageId})`;
}

// A statement, then a link to each passage it cites, numbered from 1.
export function statementMarkdown(text: string, passageIds: string[]): string {
  const links = passageIds.map((passageId, index) => passageLink(String(index + 1), passageId));
  return [paragraph(linkedAddresses(text)), ...links].join(" ");
}

// The words of a passage, a paragraph for each block it runs through, as a quotation; then a
// link to it, called after its source. A line break within a block is kept as one.
export function quoteMarkdown(quotes: string[], source: string, passageId: string): string {
  const quoted = quotes
    .map((quote) =>
      quote
        .split("\n")
        .map((line) => `> ${paragraphLine(inlineText(line))}`)
        .join("  \n"),
    )
    .join("\n>\n");
  return `${quoted}\n\n${passageLink(inlineText(source), passageId)}`;
}

// A note's Markdown with more put at its end, as a block of its own.
export function appended(markdown: string, addition: string): string {
  const held = markdown.trimEnd();
  return held === "" ? addition : `${held}\n\n${addition}`;
}

// What a note is called in a list when it has no title: the start of its first line that says
// something, without the marks of Markdown; nothing when it says nothing.
export function firstWords(markdown: string): string {
  for (const line of markdown.split("\n")) {
    const words = line
      .replace(LINK, "$1")
      .replace(BLOCK_MARKERS, "")
      .replace(MARKS, (_, escaped: string | undefined) => escaped ?? "")
      .replace(ENTITY, (entity) => ENTITIES[entity] ?? entity)
      .trim();
    if (words !== "" && !/^(?:```|---|&nbsp;)/.test(line.trim())) {
      return [...words].length > FIRST_WORDS
        ? `${[...words].slice(0, FIRST_WORDS).join("")}…`
        : words;
    }
  }
  return "";
}
