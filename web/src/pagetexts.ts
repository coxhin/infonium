import { layoutOf, type AreaText, type Layout } from "./areas";

// What a page is told of its translation: the areas set over it, and what the document tells of
// every page, from which the page's columns, its body print and its leadings are worked out.
export type PageTexts = { areas: AreaText[]; layout: Layout; unspaced: boolean };

export const NO_PAGE_TEXTS: PageTexts = { areas: [], layout: layoutOf([]), unspaced: false };

// What each page of a document was last given, by the document's layout: a page whose areas
// are the very ones it was given before is given the same again, so that it is not set again
// when a translation under way is read again and its blocks are the same.
const pagesGiven = new WeakMap<Layout, Map<number, PageTexts>>();

export function pageTextsOf(
  onPages: Map<number, AreaText[]>,
  layout: Layout,
  unspaced: boolean,
): Map<number, PageTexts> {
  const before = pagesGiven.get(layout);
  const given = new Map<number, PageTexts>();
  onPages.forEach((areas, page) => {
    const kept = before?.get(page);
    const same =
      kept !== undefined &&
      kept.unspaced === unspaced &&
      kept.areas.length === areas.length &&
      kept.areas.every((area, index) => area === areas[index]);
    given.set(page, same ? kept : { areas, layout, unspaced });
  });
  pagesGiven.set(layout, given);
  return given;
}
