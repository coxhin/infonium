import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import type { Held } from "./foldermenu";
import {
  entriesPlaced,
  outOfFolder,
  subscriptionsPlaced,
  useFolders,
  withFolder,
  type Placer,
} from "./folders";
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
  holds,
  sameSelection,
  tallied,
  type MediaChoice,
  type Order,
  type Selection,
  type Tally,
} from "./selection";
import { useSubscriptions, withSubscription, type SubscriptionPlace } from "./subscriptions";

export type Navigation = {
  subscriptions: Watched<Subscription[]>;
  folders: Watched<Folder[]>;
  selection: Selection;
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
};

function listedOf<Value>(watched: Watched<Value[]>): Value[] {
  return watched.loaded.state === "ready" ? watched.loaded.value : [];
}

export function useNavigation(
  entries: Watched<ItemEntry[]>,
  seen: ReadonlySet<string>,
  openedAtStart: string | undefined,
): Navigation {
  const subscriptions = useSubscriptions(entries.loaded, seen, entries.readNow);
  const folders = useFolders();
  const [selection, keepSelection] = useRememberedSelection();
  const [media, chooseMedia] = useRememberedMedia();
  const [order, chooseOrder] = useRememberedOrder();
  const [folded, fold] = useFoldedFolders();
  const [subscribing, setSubscribing] = useState(false);
  const [pane, setPane] = useState<SubscriptionPlace>();

  const listedEntries = listedOf(entries);
  const listedSubscriptions = listedOf(subscriptions);
  const listedFolders = listedOf(folders);
  const present = useMemo(
    () => new Set(listedSubscriptions.map((subscription) => subscription.id)),
    [listedSubscriptions],
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
      setSubscribing(false);
      setPane(
        chosen.kind === "subscription"
          ? { subscriptionId: chosen.subscriptionId, sought: undefined, again: false }
          : undefined,
      );
    },
    [keepSelection],
  );

  // After a reload, the selection remembered is kept where it holds the item open in the
  // reader; otherwise the item's subscription is selected, or its folder, or every item. A
  // subscription selected so looks for the item's posting. An item opened later, by a link
  // within the app, leaves the selection as it is.
  const opening = useRef(openedAtStart);
  useEffect(() => {
    const itemId = opening.current;
    if (
      itemId === undefined ||
      entries.loaded.state === "loading" ||
      subscriptions.loaded.state === "loading" ||
      folders.loaded.state === "loading"
    ) {
      return;
    }
    opening.current = undefined;
    const entry = listedEntries.find((each) => each.id === itemId);
    if (entry === undefined) {
      return;
    }
    const subscription = followed(entry, present);
    const chosen = holds(selection, entry, present, seen)
      ? selection
      : subscription !== undefined
        ? ({ kind: "subscription", subscriptionId: subscription } as const)
        : entry.folder !== null
          ? ({ kind: "folder", folderId: entry.folder } as const)
          : ALL;
    keepSelection(chosen);
    if (chosen.kind === "subscription") {
      setPane({ subscriptionId: chosen.subscriptionId, sought: itemId, again: false });
    }
  }, [entries.loaded, subscriptions.loaded, folders.loaded]);

  // A folder or a subscription selected that is there no more, as one removed in another window,
  // gives way to every item.
  useEffect(() => {
    const missing =
      (selection.kind === "folder" &&
        folders.loaded.state === "ready" &&
        !listedFolders.some((folder) => folder.id === selection.folderId)) ||
      (selection.kind === "subscription" &&
        subscriptions.loaded.state === "ready" &&
        !present.has(selection.subscriptionId));
    if (missing) {
      select(ALL);
    }
  }, [selection, folders.loaded, subscriptions.loaded]);

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

  const dissolve = useCallback(
    async (folderId: string) => {
      await readOnRefusal(dissolveFolder(folderId));
      folders.answer((before) => before.filter((folder) => folder.id !== folderId));
      entries.answer((before) => outOfFolder(before, folderId));
      subscriptions.answer((before) => outOfFolder(before, folderId));
      if (sameSelection(selection, { kind: "folder", folderId })) {
        select(ALL);
      }
    },
    [readOnRefusal, folders.answer, entries.answer, subscriptions.answer, selection, select],
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
      keepSelection({ kind: "subscription", subscriptionId: subscription.id });
      setSubscribing(false);
      setPane({ subscriptionId: subscription.id, sought: undefined, again });
    },
    [present, selection, readOnRefusal, subscriptions.answer, entries.readNow, keepSelection],
  );

  const gone = useCallback(
    (subscriptionId: string) => {
      const folder = listedSubscriptions.find((each) => each.id === subscriptionId)?.folder;
      subscriptions.answer((before) =>
        before.filter((subscription) => subscription.id !== subscriptionId),
      );
      entries.readNow();
      if (sameSelection(selection, { kind: "subscription", subscriptionId })) {
        select(
          folder === null || folder === undefined
            ? { kind: "unfiled" }
            : { kind: "folder", folderId: folder },
        );
      }
    },
    [listedSubscriptions, subscriptions.answer, entries.readNow, selection, select],
  );

  return {
    subscriptions,
    folders,
    selection,
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
  };
}
