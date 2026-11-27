import {
  isUnanswered,
  listFolders,
  type Folder,
  type ItemEntry,
  type Placing,
  type Subscription,
} from "./client";
import { LIST_INTERVAL_MS, useWatched, type Watched } from "./loading";
import { followed } from "./sidebarselection";

// The most characters a folder's name holds, as the service counts them once the white space
// around the name is trimmed.
export const FOLDER_NAME_LENGTH = 50;

// What a folder holds, as the question before dissolving it says it.
export type Held = { subscriptions: number; items: number };

// What moves subscriptions and items between folders, for the menus and the drags of both
// columns: the folders by name, a placing sent to the service, and a folder made. Each answers
// once the service has done it and what is on show follows, and fails with the service's reason.
export type Placer = {
  folders: Folder[];
  place: (placing: Placing) => Promise<void>;
  make: (name: string) => Promise<Folder>;
};

// The owner's folders, read again every minute while the page is visible, for those made,
// renamed or dissolved in another window, and at once after a press here.
export function useFolders(): Watched<Folder[]> {
  return useWatched(listFolders, "folders", () => false, {
    interval: () => LIST_INTERVAL_MS,
    shown: true,
    unanswered: isUnanswered,
  });
}

// The items as the service holds them once it has answered a placing: the items named, and
// those that follow a subscription named, are in the folder.
export function entriesPlaced(
  entries: ItemEntry[],
  placing: Placing,
  present: ReadonlySet<string>,
): ItemEntry[] {
  const items = new Set(placing.items);
  const subscriptions = new Set(placing.subscriptions);
  return entries.map((entry) => {
    const subscription = followed(entry, present);
    const moved =
      items.has(entry.id) || (subscription !== undefined && subscriptions.has(subscription));
    return moved ? { ...entry, folder: placing.folder } : entry;
  });
}

export function subscriptionsPlaced(
  listed: Subscription[],
  placing: Placing,
): Subscription[] {
  const subscriptions = new Set(placing.subscriptions);
  return listed.map((subscription) =>
    subscriptions.has(subscription.id) ? { ...subscription, folder: placing.folder } : subscription,
  );
}

// What was in a folder dissolved is in none.
export function outOfFolder<Placed extends { folder: string | null }>(
  placed: Placed[],
  folderId: string,
): Placed[] {
  return placed.map((each) => (each.folder === folderId ? { ...each, folder: null } : each));
}

export function withFolder(folders: Folder[], folder: Folder): Folder[] {
  return folders.some((held) => held.id === folder.id)
    ? folders.map((held) => (held.id === folder.id ? folder : held))
    : [...folders, folder];
}
