import type { ItemEntry } from "./client";

export type Origin = ItemEntry["origin"];

// What an item's origin is called where space is short: an upload's file name, and the site of
// an address, a reader's link or the one a subscription's posting named.
export function originLabel(origin: Origin): string {
  return origin.kind === "upload" ? origin.name : new URL(origin.url).hostname;
}

// An item is called by the title of the document read from it, and by its origin until then.
export function itemTitle(entry: ItemEntry): string {
  return entry.reading.kind === "read" ? entry.reading.title : originLabel(entry.origin);
}

export function isBeingRead(entry: ItemEntry): boolean {
  return entry.reading.attempt.kind === "underway";
}
