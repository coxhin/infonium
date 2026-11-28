import {
  isUnanswered,
  listFolders,
  place,
  Refused,
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
const NOT_FOUND = 404;

// What a folder holds, as the question before dissolving it says it.
export type Held = { subscriptions: number; items: number };

// A placing refused because its folder is there no more, as one dissolved in another window. The
// service names the folder by its id, which the reader never saw; this says it by the name the
// page showed it by.
export class FolderGone extends Error {
  constructor(folderName: string) {
    super(`分类“${folderName}”已经不在了`);
  }
}

// Whether the service holds the folder still; where it cannot be asked, the refusal it gave is
// said as it gave it.
function isFolderThere(folderId: string): Promise<boolean> {
  return listFolders().then(
    (folders) => folders.some((folder) => folder.id === folderId),
    () => true,
  );
}

// Places as the service does. A placing it refuses as not in the library, into a folder it no
// longer holds, is refused as FolderGone, by the folder's name among those the page knew; any
// other refusal is the service's.
export async function placeAmong(placing: Placing, known: Folder[]): Promise<void> {
  try {
    await place(placing);
  } catch (error) {
    const folderId = placing.folder;
    if (
      folderId !== null &&
      error instanceof Refused &&
      error.status === NOT_FOUND &&
      !(await isFolderThere(folderId))
    ) {
      throw new FolderGone(known.find((folder) => folder.id === folderId)?.name ?? "");
    }
    throw error;
  }
}

// What moves subscriptions and items between folders, for the menus and the drags of both
// columns: the folders by name, a placing sent to the service, and a folder made. Each answers
// once the service has done it and what is on show follows, and fails with the service's reason,
// or with FolderGone.
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
