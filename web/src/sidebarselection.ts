import type { Folder, ItemEntry, Media } from "./client";
import { isNew } from "./entry";
import { byName } from "./nameorder";

// What the sidebar has selected, which the list column shows: every item, the new ones, the
// hand-added items in no folder, one folder, or one subscription.
export type Selection =
  | { kind: "all" }
  | { kind: "new" }
  | { kind: "unfiled" }
  | { kind: "folder"; folderId: string }
  | { kind: "subscription"; subscriptionId: string };

export const ALL: Selection = { kind: "all" };

// The media the sidebar narrows its counts and the lists to, or every media.
export type MediaChoice = Media | "any";

export const MEDIA_NAMES: Record<MediaChoice, string> = {
  any: "所有类型",
  web: "网页",
  pdf: "PDF",
  recording: "录音",
};
export const MEDIA_CHOICES = Object.keys(MEDIA_NAMES) as MediaChoice[];

// How the sidebar orders folders and subscriptions: by name, or by count of new items, the
// largest first.
export type Order = "name" | "new";

export const FIXED_NAMES: Record<"all" | "new" | "unfiled", string> = {
  all: "全部",
  new: "新来源",
  unfiled: "未分类",
};

export function sameSelection(one: Selection, other: Selection): boolean {
  return keyOf(one) === keyOf(other);
}

// A selection written as one string: what the browser keeps of it, and the key of its row.
export function keyOf(selection: Selection): string {
  switch (selection.kind) {
    case "folder":
      return `folder:${selection.folderId}`;
    case "subscription":
      return `subscription:${selection.subscriptionId}`;
    default:
      return selection.kind;
  }
}

export function selectionOfKey(key: string): Selection | undefined {
  const [kind, id] = key.split(/:(.*)/s);
  switch (kind) {
    case "all":
    case "new":
    case "unfiled":
      return id === undefined ? { kind } : undefined;
    case "folder":
      return id === undefined || id === "" ? undefined : { kind, folderId: id };
    case "subscription":
      return id === undefined || id === "" ? undefined : { kind, subscriptionId: id };
    default:
      return undefined;
  }
}

// The subscription an item follows: the one that brought it, while it is there. An item that
// follows none is hand-added.
export function followed(entry: ItemEntry, present: ReadonlySet<string>): string | undefined {
  return entry.origin.kind === "subscribed" && present.has(entry.origin.subscription)
    ? entry.origin.subscription
    : undefined;
}

export function isOfMedia(entry: ItemEntry, media: MediaChoice): boolean {
  return media === "any" || entry.media === media;
}

// Whether what the selection names is there: a folder or a subscription may have gone, as one
// removed in another window.
export function isThere(
  selection: Selection,
  folders: ReadonlySet<string>,
  present: ReadonlySet<string>,
): boolean {
  switch (selection.kind) {
    case "folder":
      return folders.has(selection.folderId);
    case "subscription":
      return present.has(selection.subscriptionId);
    default:
      return true;
  }
}

// The selection a page opens with, once the items, the subscriptions and the folders are read:
// the one remembered, where it is there still and holds the item open in the reader, if one is;
// otherwise the item's subscription where it follows one, else its folder, else every item. A
// folder or a subscription that is there no more holds nothing. The item open is seen as soon as
// the reader has it on show, so that 新来源 holds it no more whichever comes first.
export function openingSelection(
  remembered: Selection,
  opened: ItemEntry | undefined,
  folders: ReadonlySet<string>,
  present: ReadonlySet<string>,
  seen: ReadonlySet<string>,
): Selection {
  const there = isThere(remembered, folders, present);
  if (opened === undefined) {
    return there ? remembered : ALL;
  }
  if (there && holds(remembered, opened, present, new Set(seen).add(opened.id))) {
    return remembered;
  }
  const subscription = followed(opened, present);
  if (subscription !== undefined) {
    return { kind: "subscription", subscriptionId: subscription };
  }
  return opened.folder !== null && folders.has(opened.folder)
    ? { kind: "folder", folderId: opened.folder }
    : ALL;
}

// Whether the selection holds the item, whatever its media.
export function holds(
  selection: Selection,
  entry: ItemEntry,
  present: ReadonlySet<string>,
  seen: ReadonlySet<string>,
): boolean {
  switch (selection.kind) {
    case "all":
      return true;
    case "new":
      return isNew(entry, seen);
    case "unfiled":
      return entry.folder === null && followed(entry, present) === undefined;
    case "folder":
      return entry.folder === selection.folderId;
    case "subscription":
      return followed(entry, present) === selection.subscriptionId;
  }
}

// The items the list column lists for a selection and a media, in the order the service gives.
export function listed(
  entries: ItemEntry[],
  selection: Selection,
  media: MediaChoice,
  present: ReadonlySet<string>,
  seen: ReadonlySet<string>,
): ItemEntry[] {
  return entries.filter(
    (entry) => isOfMedia(entry, media) && holds(selection, entry, present, seen),
  );
}

// What the sidebar counts and keeps, of the items of a media: how many are new, in all, of
// each subscription, in each folder and of the hand-added items in no folder; and which
// subscriptions and folders hold any.
export type Tally = {
  fresh: number;
  freshOfSubscription: ReadonlyMap<string, number>;
  freshInFolder: ReadonlyMap<string, number>;
  freshUnfiled: number;
  holdingSubscriptions: ReadonlySet<string>;
  holdingFolders: ReadonlySet<string>;
};

function countOne(counts: Map<string, number>, key: string): void {
  counts.set(key, (counts.get(key) ?? 0) + 1);
}

export function tallied(
  entries: ItemEntry[],
  media: MediaChoice,
  present: ReadonlySet<string>,
  seen: ReadonlySet<string>,
): Tally {
  let fresh = 0;
  let freshUnfiled = 0;
  const freshOfSubscription = new Map<string, number>();
  const freshInFolder = new Map<string, number>();
  const holdingSubscriptions = new Set<string>();
  const holdingFolders = new Set<string>();
  for (const entry of entries) {
    if (!isOfMedia(entry, media)) {
      continue;
    }
    const subscription = followed(entry, present);
    const isFresh = isNew(entry, seen);
    if (subscription !== undefined) {
      holdingSubscriptions.add(subscription);
    }
    if (entry.folder !== null) {
      holdingFolders.add(entry.folder);
    }
    if (isFresh) {
      fresh += 1;
      if (subscription !== undefined) {
        countOne(freshOfSubscription, subscription);
      }
      // A folder counts every new item in it: those of its subscriptions, and those whose
      // subscription was removed, which stay new until they are seen. So does 未分类, of the
      // items a removed subscription in no folder brought.
      if (entry.folder !== null) {
        countOne(freshInFolder, entry.folder);
      } else if (subscription === undefined) {
        freshUnfiled += 1;
      }
    }
  }
  return {
    fresh,
    freshOfSubscription,
    freshInFolder,
    freshUnfiled,
    holdingSubscriptions,
    holdingFolders,
  };
}

// Folders or subscriptions in the order chosen: by name, or by count of new items, the largest
// first and those of one count by name.
export function ordered<Named>(
  named: Named[],
  name: (each: Named) => string,
  count: (each: Named) => number,
  order: Order,
): Named[] {
  return [...named].sort(
    (one, other) =>
      (order === "new" ? count(other) - count(one) : 0) || byName(name(one), name(other)),
  );
}

// What the list column's head calls the selection: a subscription's panel names it below.
export function selectionName(selection: Selection, folders: ReadonlyMap<string, Folder>): string {
  switch (selection.kind) {
    case "folder":
      return folders.get(selection.folderId)?.name ?? "分类";
    case "subscription":
      return "订阅";
    default:
      return FIXED_NAMES[selection.kind];
  }
}
