import { useSyncExternalStore } from "react";

const ITEM_LINK = /^#\/items\/([0-9a-f]+)(?:\/revisions\/([1-9][0-9]*))?$/;

// What a link of the app opens: an item, as the document it holds now or, where the link
// names one, as an earlier revision of it.
export type Linked = { itemId: string; revision: number | undefined };

export function itemLink(itemId: string): string {
  return `#/items/${itemId}`;
}

export function revisionLink(itemId: string, revision: number): string {
  return `${itemLink(itemId)}/revisions/${revision}`;
}

export function linkedItem(link: string): Linked | undefined {
  const [, itemId, revision] = ITEM_LINK.exec(link) ?? [];
  if (itemId === undefined) {
    return undefined;
  }
  return { itemId, revision: revision === undefined ? undefined : Number(revision) };
}

// The link that opens what a link names, written as the app writes it.
export function linkOf(linked: Linked): string {
  return linked.revision === undefined
    ? itemLink(linked.itemId)
    : revisionLink(linked.itemId, linked.revision);
}

function onLinkChange(notify: () => void): () => void {
  addEventListener("hashchange", notify);
  return () => removeEventListener("hashchange", notify);
}

function currentLink(): string {
  const linked = linkedItem(location.hash);
  return linked === undefined ? "" : linkOf(linked);
}

// The link the window is at, as the app writes it; empty where it opens no item.
export function useLink(): string {
  return useSyncExternalStore(onLinkChange, currentLink);
}

// The link the window is at, read once.
export function linkNow(): string {
  return currentLink();
}
