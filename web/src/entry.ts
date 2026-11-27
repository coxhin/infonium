import type { ItemEntry } from "./client";

export type Origin = ItemEntry["origin"];

// What an item's origin is called where space is short: an upload's file name, and the site of
// an address, a reader's link or the one a subscription's posting named.
export function originLabel(origin: Origin): string {
  return origin.kind === "upload" ? origin.name : new URL(origin.url).hostname;
}

// An item is called by the title of the document read from it; until then, an item that a
// subscription brought by what its posting is called, and any other by its origin.
export function itemTitle(entry: ItemEntry): string {
  if (entry.reading.kind === "read") {
    return entry.reading.title;
  }
  return entry.origin.kind === "subscribed" ? entry.origin.called : originLabel(entry.origin);
}

export function isBeingRead(entry: ItemEntry): boolean {
  return entry.reading.attempt.kind === "underway";
}

// An item stands out as new from when a subscription brought it and it was read, until its
// reader has had it on show: the service says when it was seen, and the items told as seen
// from this page since it was read are seen as well.
export function isNew(entry: ItemEntry, seen: ReadonlySet<string>): boolean {
  return (
    entry.origin.kind === "subscribed" &&
    entry.reading.kind === "read" &&
    entry.seen === null &&
    !seen.has(entry.id)
  );
}
