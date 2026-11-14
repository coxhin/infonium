const ITEM_LINK = /^#\/items\/([0-9a-f]+)$/;

export function itemLink(itemId: string): string {
  return `#/items/${itemId}`;
}

export function linkedItem(link: string): string | undefined {
  return ITEM_LINK.exec(link)?.[1];
}
