import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  dissolveFolder,
  listFolders,
  makeFolder,
  renameFolder,
  type Folder,
  type ItemEntry,
  type Placing,
  type Subscription,
} from "./client";
import {
  entriesPlaced,
  outOfFolder,
  placeAmong,
  subscriptionsPlaced,
  useFolders,
  withFolder,
  type Held,
  type Placer,
} from "./folders";
import { landingOn, type Landing } from "./keyboarding";
import type { Watched } from "./loading";
import { byName } from "./nameorder";
import {
  useFoldedFolders,
  useRememberedMedia,
  useRememberedOrder,
  useRememberedSelection,
} from "./remembered";
import {
  ALL,
  followed,
  keyOf,
  openingSelection,
  sameSelection,
  tallied,
  type MediaChoice,
  type OpeningLists,
  type Order,
  type Selection,
  type Tally,
} from "./sidebarselection";
import { useSubscriptions, withSubscription } from "./subscriptionlist";

// How a subscription's panel opens: on which subscription; at the item it was opened at, whose
// posting it looks for, if any; and whether as the answer to subscribing to a feed the list held
// already.
type PaneOpening = {
  subscriptionId: string;
  sought: string | undefined;
  again: boolean;
};

// The state of browsing the library, which the sidebar and the list column share: the lists the
// sidebar is drawn from, what it has selected and how, its counts, and what moves subscriptions
// and items between folders.
export type Browsing = {
  subscriptions: Watched<Subscription[]>;
  folders: Watched<Folder[]>;
  selection: Selection;
  // Whether the selection the page opens with is decided. Until it is, nothing but a press of the
  // reader's selects, and what shows the selection waits for it.
  opened: boolean;
  // How the panel of the subscription the list column shows opened.
  pane: PaneOpening | undefined;
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
  // A subscription that left, removed here or in another window: its items are where it was,
  // and, selected, the selection goes there.
  left: (subscriptionId: string) => void;
  // The row of the sidebar the keyboard is to land on, where it has nowhere else to be: a folder
  // or a subscription just made, or what is selected once the subscription open is removed.
  landing: Landing | undefined;
  land: (key: string) => void;
};

function listedOf<Value>(watched: Watched<Value[]>): Value[] {
  return watched.loaded.state === "ready" ? watched.loaded.value : [];
}

const NOTHING_READ: OpeningLists = { entries: undefined, folders: undefined, present: undefined };

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
  const [pane, setPane] = useState<PaneOpening>();
  const [landing, setLanding] = useState<Landing>();
  const land = useCallback((key: string) => setLanding(landingOn(key)), []);
  // Whether the selection the page opens with is decided: at once where no list could move it,
  // as every item, or 新来源 with no item open; otherwise below, once the lists settle it.
  const [opened, setOpened] = useState(
    () => openingSelection(selection, openedAtStart, NOTHING_READ) !== undefined,
  );

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
  // The subscriptions tell which items in no folder are hand-added: until they are read, 未分类
  // counts none.
  const subscriptionsRead = subscriptions.loaded.state === "ready";
  const tally = useMemo(() => {
    const counted = tallied(listedEntries, media, present, seen);
    return subscriptionsRead ? counted : { ...counted, freshUnfiled: 0 };
  }, [listedEntries, media, present, seen, subscriptionsRead]);
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

  // The selection a page opens with is decided in this one place, as soon as the lists read so
  // far settle it, whichever arrives first and whichever cannot be read for a while, and as it is
  // with all three read. Until then nothing but a press of the reader's selects, and the list
  // column waits, or says which list it waits for and why. A subscription selected so looks for
  // the posting of the item open. An item opened later, by a link within the app, leaves the
  // selection as it is.
  useEffect(() => {
    if (opened) {
      return;
    }
    const lists: OpeningLists = {
      entries: entries.loaded.state === "ready" ? entries.loaded.value : undefined,
      folders: folders.loaded.state === "ready" ? folderIds : undefined,
      present: subscriptions.loaded.state === "ready" ? present : undefined,
    };
    const chosen = openingSelection(selection, openedAtStart, lists);
    if (chosen === undefined) {
      return;
    }
    setOpened(true);
    keepSelection(chosen);
    const sought = lists.entries?.some((entry) => entry.id === openedAtStart)
      ? openedAtStart
      : undefined;
    if (chosen.kind === "subscription" && sought !== undefined) {
      setPane({ subscriptionId: chosen.subscriptionId, sought, again: false });
    }
  }, [opened, entries.loaded, subscriptions.loaded, folders.loaded]);

  // The folder each subscription was in when the list of subscriptions was last read with it, for
  // one that leaves.
  const folderOf = useRef(new Map<string, string | null>());
  useEffect(() => {
    for (const subscription of listedSubscriptions) {
      folderOf.current.set(subscription.id, subscription.folder);
    }
  }, [listedSubscriptions]);

  // A subscription that left, removed here or in another window, whichever notices it first: the
  // panel, when it reads the subscription, or the list of subscriptions, read without it. Its
  // items stay, hand-added now, where it was. Selected, the selection goes where its items went:
  // the folder it was in, while that folder stands, else 未分类. Another window may have
  // dissolved that folder too, so the service is asked whether it stands, and the folders on
  // show take its answer; the selection moves only while the subscription is selected still. The
  // keyboard follows where it was on what left. The form that subscribes stays where it is open,
  // with what it says.
  const selectionNow = useRef(selection);
  selectionNow.current = selection;
  const leaving = useRef<string>(undefined);
  const left = useCallback(
    (subscriptionId: string) => {
      subscriptions.replace((before) =>
        before.filter((subscription) => subscription.id !== subscriptionId),
      );
      entries.readNow();
      const leftSelected = { kind: "subscription", subscriptionId } as const;
      if (!sameSelection(selection, leftSelected) || leaving.current === subscriptionId) {
        return;
      }
      leaving.current = subscriptionId;
      const folder = folderOf.current.get(subscriptionId) ?? null;
      const standing =
        folder === null
          ? Promise.resolve(false)
          : listFolders().then(
              (listed) => {
                folders.replace(() => listed);
                return listed.some((each) => each.id === folder);
              },
              () => folderIds.has(folder),
            );
      void standing.then((stands) => {
        leaving.current = undefined;
        if (!sameSelection(selectionNow.current, leftSelected)) {
          return;
        }
        const chosen: Selection =
          stands && folder !== null ? { kind: "folder", folderId: folder } : { kind: "unfiled" };
        keepSelection(chosen);
        setPane(undefined);
        land(keyOf(chosen));
      });
    },
    [
      selection,
      folderIds,
      subscriptions.replace,
      folders.replace,
      entries.readNow,
      keepSelection,
      land,
    ],
  );

  // Once the page has opened, a subscription selected that the list of subscriptions no longer
  // holds has left; a folder selected that is there no more, as one dissolved in another window,
  // gives way to every item.
  useEffect(() => {
    if (!opened) {
      return;
    }
    if (
      selection.kind === "subscription" &&
      subscriptions.loaded.state === "ready" &&
      !present.has(selection.subscriptionId)
    ) {
      left(selection.subscriptionId);
    } else if (
      selection.kind === "folder" &&
      folders.loaded.state === "ready" &&
      !folderIds.has(selection.folderId)
    ) {
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
        await readOnRefusal(placeAmong(placing, listedFolders));
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
        await readOnRefusal(placeAmong(placing, listedFolders));
        subscriptions.answer((before) => subscriptionsPlaced(before, placing));
      }
      const chosen = { kind: "subscription", subscriptionId: subscription.id } as const;
      keepSelection(chosen);
      setOpened(true);
      setSubscribing(false);
      setPane({ subscriptionId: subscription.id, sought: undefined, again });
      land(keyOf(chosen));
    },
    [
      present,
      selection,
      listedFolders,
      readOnRefusal,
      subscriptions.answer,
      entries.readNow,
      keepSelection,
      land,
    ],
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
    left,
    landing,
    land,
  };
}
