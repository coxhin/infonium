import type { Anchor } from "./client";

// A quote is a literal piece of its block's text, and the page shows the block's text but for
// white space and for what the browser draws without text: the quotation marks of an inline
// quotation, which the extractor writes into the text as these.
const OPENING_QUOTATION_MARK = "“";
const CLOSING_QUOTATION_MARK = "”";
const INLINE_QUOTATION = "q";
// What the extractor leaves out of a block's text that the page holds: words struck out, and
// code for the browser.
const UNREAD_ELEMENTS = "del, s, strike, script, style, noscript, template";
const SPACE = /\s/u;

// A character of the block's text as the page holds it, and the place in the page just before
// it and just after it.
type Shown = { character: string; start: [Node, number]; end: [Node, number] };

function shownCharacters(element: Element): Shown[] {
  const shown: Shown[] = [];
  function walk(parent: Node) {
    for (const child of parent.childNodes) {
      if (child.nodeType === Node.TEXT_NODE) {
        if (child.parentElement?.checkVisibility() ?? false) {
          let offset = 0;
          for (const character of child.textContent ?? "") {
            const end = offset + character.length;
            shown.push({ character, start: [child, offset], end: [child, end] });
            offset = end;
          }
        }
      } else if (child.nodeType === Node.ELEMENT_NODE) {
        const inner = child as Element;
        if (inner.matches(UNREAD_ELEMENTS)) {
          continue;
        }
        const quotation = inner.localName === INLINE_QUOTATION;
        const inside: [Node, number] = [inner, 0];
        const after: [Node, number] = [inner, inner.childNodes.length];
        if (quotation) {
          shown.push({ character: OPENING_QUOTATION_MARK, start: inside, end: inside });
        }
        walk(inner);
        if (quotation) {
          shown.push({ character: CLOSING_QUOTATION_MARK, start: after, end: after });
        }
      }
    }
  }
  walk(element);
  return shown;
}

// The characters that are not white space, each with its place in the list it is drawn from.
function unspaced(characters: string[]): { text: string; origins: number[] } {
  const kept: string[] = [];
  const origins: number[] = [];
  characters.forEach((character, index) => {
    if (!SPACE.test(character)) {
      for (const unit of character.split("")) {
        kept.push(unit);
        origins.push(index);
      }
    }
  });
  return { text: kept.join(""), origins };
}

// The quoted words in an element that holds its block's text as the page shows it: of the
// places in it that hold the quote, white space aside, the one nearest where the quote stands
// in the block. None where the page's words differ from the quote's.
export function quotedWords(
  page: Document,
  element: Element,
  anchor: Anchor,
  text: string,
): Range | null {
  const quote = unspaced([...anchor.quote]).text;
  if (quote === "") {
    return null;
  }
  const shown = shownCharacters(element);
  const inPage = unspaced(shown.map((character) => character.character));
  const expected = unspaced([...text].slice(0, anchor.start)).text.length;
  let best: number | undefined;
  for (let at = inPage.text.indexOf(quote); at !== -1; at = inPage.text.indexOf(quote, at + 1)) {
    if (best === undefined || Math.abs(at - expected) < Math.abs(best - expected)) {
      best = at;
    }
  }
  if (best === undefined) {
    return null;
  }
  const first = shown[inPage.origins[best] ?? -1];
  const last = shown[inPage.origins[best + quote.length - 1] ?? -1];
  if (first === undefined || last === undefined) {
    return null;
  }
  const range = page.createRange();
  range.setStart(...first.start);
  range.setEnd(...last.end);
  return range;
}
