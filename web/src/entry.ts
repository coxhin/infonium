import type { ItemEntry } from "./client";

export type Origin = ItemEntry["origin"];

// What an item's origin is called where space is short: a link's site, an upload's file name.
export function originLabel(origin: Origin): string {
  return origin.kind === "link" ? new URL(origin.url).hostname : origin.name;
}

// An item is called by the title of the document read from it, and by its origin until then.
export function itemTitle(entry: ItemEntry): string {
  return entry.reading.kind === "read" ? entry.reading.title : originLabel(entry.origin);
}

export function isBeingRead(entry: ItemEntry): boolean {
  return entry.reading.attempt.kind === "underway";
}
