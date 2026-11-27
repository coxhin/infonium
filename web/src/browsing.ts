import { useCallback, useEffect, useMemo, useState } from "react";
import {
  dissolveFolder,
  makeFolder,
  place,
  renameFolder,
  type Folder,
  type ItemEntry,
  type Placing,
  type Subscription,
} from "./client";
import {
  entriesPlaced,
  outOfFolder,
  subscriptionsPlaced,
  useFolders,
  withFolder,
  type Held,
  type Placer,
} from "./folders";
import type { Landing } from "./keyboarding";
import type { Watched } from "./loading";
import {
  useFoldedFolders,
  useRememberedMedia,
  useRememberedOrder,
  useRememberedSelection,
} from "./remembered";
import {
  ALL,
  byName,
  followed,
  isThere,
  keyOf,
  openingSelection,
  sameSelection,
  tallied,
  type MediaChoice,
  type Order,
  type Selection,
  type Tally,
} from "./sidebarselection";
import { useSubscriptions, withSubscription, type SubscriptionPlace } from "./subscriptions";

// The state of browsing the library, which the sidebar and the list column share: the lists the
// sidebar is drawn from, what it has selected and how, its counts, and what moves subscriptions
// and items between folders.
export type Browsing = {
  subscriptions: Watched<Subscription[]>;
  folders: Watched<Folder[]>;
  selection: Selection;
  // Whether the selection the page opens with is decided, which waits for the three lists.
  opened: boolean;
  // The subscription the list column shows, with what it was opened for.
  pane: SubscriptionPlace | undefined;
  select: (selection: Selection) => void;
  media: MediaChoice;
  chooseMedia: (media: MediaChoice) => void;
  order: Order;
  chooseOrder: (order: Order) => void;
  folded: ReadonlySet<string>;
  fold: (folded: ReadonlySet<string>) => void;
  // Whether the list column shows the form that subscribes, in place of the selection's list.
  subscribing: boolean;
  startSubscribing: () => void;
  stopSubscribing: () => void;
  // The subscriptions there are, which items they brought follow.
  present: ReadonlySet<string>;
  tally: Tally;
  // The folders that hold anything, of any media.
  occupied: ReadonlySet<string>;
  heldIn: (folderId: string) => Held;
  placer: Placer;
  rename: (folderId: string, name: string) => Promise<void>;
  dissolve: (folderId: string) => Promise<void>;
  // A subscription answered by subscribing: placed in the folder selected where it is new, and
  // selected once it is. A placing the service refused is the reason the promise fails with.
  subscribed: (subscription: Subscription) => Promise<void>;
  // A subscription removed here or elsewhere: its items are where it was.
  gone: (subscriptionId: string) => void;
  // The row of the sidebar the keyboard is to land on, where it has nowhere else to be: a folder
  // or a subscription just made, or what is selected once the subscription open is removed.
  landing: Landing | undefined;
  land: (key: string) => void;
};

function listedOf<Value>(watched: Watched<Value[]>): Value[] {
  return watched.loaded.state === "ready" ? watched.loaded.value : [];
}

export function useBrowsing(
  entries: Watched<ItemEntry[]>,
  seen: ReadonlySet<string>,
  openedAtStart: string | undefined,
): Browsing {
  const subscriptions = useSubscriptions(entries.loaded, seen, entries.readNow);
  const folders = useFolders();
  const [selection, keepSelection] = useRememberedSelection();
  const [media, chooseMedia] = useRememberedMedia();
  const [order, chooseOrder] = useRememberedOrder();
  const [folded, fold] = useFoldedFolders();
  const [subscribing, setSubscribing] = useState(false);
  const [pane, setPane] = useState<SubscriptionPlace>();
  const [landing, setLanding] = useState<Landing>();
  const land = useCallback((key: string) => setLanding({ key }), []);

  const listedEntries = listedOf(entries);
  const listedSubscriptions = listedOf(subscriptions);
  const listedFolders = listedOf(folders);
  const present = useMemo(
    () => new Set(listedSubscriptions.map((subscription) => subscription.id)),
    [listedSubscriptions],
  );
  const folderIds = useMemo(
    () => new Set(listedFolders.map((folder) => folder.id)),
    [listedFolders],
  );
  const tally = useMemo(
    () => tallied(listedEntries, media, present, seen),
    [listedEntries, media, present, seen],
  );
  const occupied = useMemo(
    () =>
      new Set([
        ...listedSubscriptions.flatMap((subscription) =>
          subscription.folder === null ? [] : [subscription.folder],
        ),
        ...listedEntries.flatMap((entry) => (entry.folder === null ? [] : [entry.folder])),
      ]),
    [listedSubscriptions, listedEntries],
  );

  const select = useCallback(
    (chosen: Selection) => {
      keepSelection(chosen);
      setOpened(true);
      setSubscribing(false);
      setPane(
        chosen.kind === "subscription"
          ? { subscriptionId: chosen.subscriptionId, sought: undefined, again: false }
          : undefined,
      );
    },
    [keepSelection],
  );

  // The selection a page opens with is decided in this one place, once the items, the
  // subscriptions and the folders are all read, whichever arrives first: until then nothing
  // moves it, and the list column waits. A subscription selected so looks for the posting of the
  // item open. A selection the reader makes meanwhile stands. An item opened later, by a link
  // within the app, leaves the selection as it is.
  const [opened, setOpened] = useState(false);
  const allRead =
    entries.loaded.state === "ready" &&
    subscriptions.loaded.state === "ready" &&
    folders.loaded.state === "ready";
  useEffect(() => {
    if (opened || !allRead) {
      return;
    }
    setOpened(true);
    const entry =
      openedAtStart === undefined
        ? undefined
        : listedEntries.find((each) => each.id === openedAtStart);
    const chosen = openingSelection(selection, entry, folderIds, present, seen);
    keepSelection(chosen);
    if (chosen.kind === "subscription" && entry !== undefined) {
      setPane({ subscriptionId: chosen.subscriptionId, sought: entry.id, again: false });
    }
  }, [opened, allRead]);

  // Once the page has opened, a folder or a subscription selected that is there no more, as one
  // removed in another window, gives way to every item. The form that subscribes stays where it
  // is open, with what it says.
  useEffect(() => {
    const read = selection.kind === "folder" ? folders.loaded : subscriptions.loaded;
    if (opened && read.state === "ready" && !isThere(selection, folderIds, present)) {
      keepSelection(ALL);
    }
  }, [opened, selection, folders.loaded, subscriptions.loaded]);

  // What the service refuses may rest on something this page shows that is there no more, as a
  // folder dissolved in another window: the lists are read again, and the refusal is said.
  const readOnRefusal = useCallback(
    <Answer>(asking: Promise<Answer>): Promise<Answer> =>
      asking.catch((error: unknown) => {
        entries.readNow();
        subscriptions.readNow();
        folders.readNow();
        throw error;
      }),
    [entries.readNow, subscriptions.readNow, folders.readNow],
  );

  const placer = useMemo<Placer>(
    () => ({
      folders: [...listedFolders].sort((one, other) => byName(one.name, other.name)),
      place: async (placing: Placing) => {
        await readOnRefusal(place(placing));
        entries.answer((before) => entriesPlaced(before, placing, present));
        subscriptions.answer((before) => subscriptionsPlaced(before, placing));
      },
      make: async (name: string) => {
        const folder = await readOnRefusal(makeFolder(name));
        folders.answer((before) => withFolder(before, folder));
        return folder;
      },
    }),
    [listedFolders, present, readOnRefusal, entries.answer, subscriptions.answer, folders.answer],
  );

  const heldIn = useCallback(
    (folderId: string): Held => ({
      subscriptions: listedSubscriptions.filter((subscription) => subscription.folder === folderId)
        .length,
      items: listedEntries.filter(
        (entry) => entry.folder === folderId && followed(entry, present) === undefined,
      ).length,
    }),
    [listedSubscriptions, listedEntries, present],
  );

  const rename = useCallback(
    async (folderId: string, name: string) => {
      const folder = await readOnRefusal(renameFolder(folderId, name));
      folders.answer((before) => withFolder(before, folder));
    },
    [readOnRefusal, folders.answer],
  );

  // A folder dissolved while selected gives way to every item, as one missing does.
  const dissolve = useCallback(
    async (folderId: string) => {
      await readOnRefusal(dissolveFolder(folderId));
      folders.answer((before) => before.filter((folder) => folder.id !== folderId));
      entries.answer((before) => outOfFolder(before, folderId));
      subscriptions.answer((before) => outOfFolder(before, folderId));
    },
    [readOnRefusal, folders.answer, entries.answer, subscriptions.answer],
  );

  const subscribed = useCallback(
    async (subscription: Subscription) => {
      // A feed subscribed to already is answered as its subscription, which keeps its place,
      // and is opened saying so.
      const again = present.has(subscription.id);
      subscriptions.answer((before) =>
        before.some((held) => held.id === subscription.id)
          ? withSubscription(before, subscription)
          : [subscription, ...before],
      );
      entries.readNow();
      if (!again && selection.kind === "folder") {
        const placing = {
          folder: selection.folderId,
          subscriptions: [subscription.id],
          items: [],
        };
        await readOnRefusal(place(placing));
        subscriptions.answer((before) => subscriptionsPlaced(before, placing));
      }
      const chosen = { kind: "subscription", subscriptionId: subscription.id } as const;
      keepSelection(chosen);
      setOpened(true);
      setSubscribing(false);
      setPane({ subscriptionId: subscription.id, sought: undefined, again });
      land(keyOf(chosen));
    },
    [present, selection, readOnRefusal, subscriptions.answer, entries.readNow, keepSelection, land],
  );

  const gone = useCallback(
    (subscriptionId: string) => {
      const folder = listedSubscriptions.find((each) => each.id === subscriptionId)?.folder;
      subscriptions.answer((before) =>
        before.filter((subscription) => subscription.id !== subscriptionId),
      );
      entries.readNow();
      if (sameSelection(selection, { kind: "subscription", subscriptionId })) {
        const chosen: Selection =
          folder === null || folder === undefined
            ? { kind: "unfiled" }
            : { kind: "folder", folderId: folder };
        select(chosen);
        land(keyOf(chosen));
      }
    },
    [listedSubscriptions, subscriptions.answer, entries.readNow, selection, select, land],
  );

  return {
    subscriptions,
    folders,
    selection,
    opened,
    pane:
      selection.kind === "subscription"
        ? pane?.subscriptionId === selection.subscriptionId
          ? pane
          : { subscriptionId: selection.subscriptionId, sought: undefined, again: false }
        : undefined,
    select,
    media,
    chooseMedia,
    order,
    chooseOrder,
    folded,
    fold,
    subscribing,
    startSubscribing: () => setSubscribing(true),
    stopSubscribing: () => setSubscribing(false),
    present,
    tally,
    occupied,
    heldIn,
    placer,
    rename,
    dissolve,
    subscribed,
    gone,
    landing,
    land,
  };
}
