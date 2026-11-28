import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import type { Folder, Subscription } from "./client";
import { FolderMenu } from "./foldermenu";
import type { Held, Placer } from "./folders";
import {
  isOverruled,
  keyboardIsNowhere,
  landingOn,
  useKeyboardHolder,
  useOnLanding,
  type Landing,
} from "./keyboarding";
import { isMenuKey, openingAt, type Opening } from "./menu";
import { PlaceMenu } from "./placemenu";
import {
  ALL,
  keyOf,
  ordered,
  selectionOfKey,
  type MediaChoice,
  type Order,
  type Selection,
  type Tally,
} from "./sidebarselection";
import {
  FIXED,
  FixedRow,
  FolderRow,
  ID_SEPARATOR,
  SubscriptionRow,
  folderKey,
  subscriptionKey,
  type RowActions,
} from "./treerows";

// A folder as the tree shows it: the subscriptions in it, in the order chosen.
type Branch = { folder: Folder; subscriptions: Subscription[] };
type Shown = { branches: Branch[]; unfiled: Subscription[] };

// What the tree shows: the folders, each with its subscriptions, and the subscriptions in no
// folder, in the order chosen. Under a media, a folder or a subscription that holds no item of
// it is left out, unless it is selected (kept), or holds the subscription selected, or is a
// folder that holds nothing yet, which a reader has made to put things in.
function shownOf(
  folders: Folder[],
  subscriptions: Subscription[],
  tally: Tally,
  media: MediaChoice,
  order: Order,
  kept: Selection | undefined,
  occupied: ReadonlySet<string>,
): Shown {
  const known = new Set(folders.map((folder) => folder.id));
  const keptSubscription = kept?.kind === "subscription" ? kept.subscriptionId : undefined;
  const isShownSubscription = (subscription: Subscription) =>
    media === "any" ||
    tally.holdingSubscriptions.has(subscription.id) ||
    subscription.id === keptSubscription;
  const inFolder = new Map<string | null, Subscription[]>();
  for (const subscription of subscriptions) {
    const folder =
      subscription.folder !== null && known.has(subscription.folder) ? subscription.folder : null;
    inFolder.set(folder, [...(inFolder.get(folder) ?? []), subscription]);
  }
  const orderedSubscriptions = (listed: Subscription[]) =>
    ordered(
      listed.filter(isShownSubscription),
      (subscription) => subscription.title,
      (subscription) => tally.freshOfSubscription.get(subscription.id) ?? 0,
      order,
    );
  const holdingKept = subscriptions.find((each) => each.id === keptSubscription)?.folder;
  const isShownFolder = (folder: Folder) =>
    media === "any" ||
    tally.holdingFolders.has(folder.id) ||
    !occupied.has(folder.id) ||
    (kept?.kind === "folder" && kept.folderId === folder.id) ||
    holdingKept === folder.id;
  return {
    branches: ordered(
      folders.filter(isShownFolder),
      (folder) => folder.name,
      (folder) => tally.freshInFolder.get(folder.id) ?? 0,
      order,
    ).map((folder) => ({
      folder,
      subscriptions: orderedSubscriptions(inFolder.get(folder.id) ?? []),
    })),
    unfiled: orderedSubscriptions(inFolder.get(null) ?? []),
  };
}

// The rows the keyboard moves among, in the order they stand, each with the folder it stands
// under, if any.
function visibleRows(
  shown: Shown,
  folded: ReadonlySet<string>,
): { key: string; parent?: string }[] {
  return [
    ...FIXED.map((fixed) => ({ key: keyOf(fixed) })),
    ...shown.branches.flatMap(({ folder, subscriptions }) => {
      const key = folderKey(folder.id);
      return [
        { key },
        ...(folded.has(folder.id)
          ? []
          : subscriptions.map((subscription) => ({
              key: subscriptionKey(subscription.id),
              parent: key,
            }))),
      ];
    }),
    ...shown.unfiled.map((subscription) => ({ key: subscriptionKey(subscription.id) })),
  ];
}

type MenuOpen =
  | { kind: "folder"; key: string; folder: Folder; opening: Opening }
  | { kind: "subscription"; key: string; subscription: Subscription; opening: Opening };

// The count of new items each fixed entry shows.
const FIXED_COUNTS: Record<(typeof FIXED)[number]["kind"], (tally: Tally) => number> = {
  all: () => 0,
  new: (tally) => tally.fresh,
  unfiled: (tally) => tally.freshUnfiled,
};

// The one of the keys under a folder that is the key looked for, if it is among them.
function among(keys: string[], key: string | undefined): string | undefined {
  return key !== undefined && keys.includes(key) ? key : undefined;
}

// The sidebar's tree: the fixed entries, the folders, each folded or unfolded over its
// subscriptions, and the subscriptions in no folder. ↑ and ↓ move among the rows on show, →
// unfolds a folder or goes to its first subscription, ← folds it or goes up to it, Enter and
// Space select. One row at a time takes Tab: the one the keyboard moved to last, which is the
// selection's row until the keyboard moves. A folder something is dragged onto is marked. A row
// that goes with the keyboard in it hands it on: a subscription moved by its menu to its row
// where it stands now, unfolding the folder it went into; any other row to the row that stands
// in its place, or 全部.
export function SidebarTree({
  selection,
  tally,
  media,
  order,
  folders,
  subscriptions,
  occupied,
  heldIn,
  folded,
  landing,
  dropOn,
  placer,
  onSelect,
  onFolded,
  onRename,
  onDissolve,
}: {
  selection: Selection;
  tally: Tally;
  media: MediaChoice;
  order: Order;
  folders: Folder[];
  subscriptions: Subscription[];
  // The folders that hold anything, of any media.
  occupied: ReadonlySet<string>;
  heldIn: (folderId: string) => Held;
  folded: ReadonlySet<string>;
  // A row the keyboard is sent to from outside the tree, where it has nowhere else to be, as a
  // folder or a subscription just made, each time the value changes.
  landing: Landing | undefined;
  dropOn: string | undefined;
  placer: Placer;
  onSelect: (selection: Selection) => void;
  onFolded: (folded: ReadonlySet<string>) => void;
  onRename: (folderId: string, name: string) => Promise<void>;
  onDissolve: (folderId: string) => Promise<void>;
}) {
  const tree = useRef<HTMLUListElement>(null);
  const selectedKey = keyOf(selection);
  const [active, setActive] = useState(selectedKey);
  const [renaming, setRenaming] = useState<string>();
  const [menu, setMenu] = useState<MenuOpen>();

  // The selection matters to what is shown only under a media, where it stays on show.
  const kept = media === "any" ? undefined : selection;
  const keptKey = kept === undefined ? "" : keyOf(kept);
  const shown = useMemo(
    () => shownOf(folders, subscriptions, tally, media, order, kept, occupied),
    [folders, subscriptions, tally, media, order, keptKey, occupied],
  );
  const subscriptionsById = useMemo(
    () => new Map(subscriptions.map((subscription) => [subscription.id, subscription])),
    [subscriptions],
  );
  const rows = visibleRows(shown, folded);

  // The keyboard's row follows the selection, wherever it was made.
  useEffect(() => {
    setActive(selectedKey);
  }, [selectedKey]);

  // A subscription selected inside a folded folder, as one selected when the app opens, is
  // shown by unfolding its folder.
  const selectedParent =
    selection.kind === "subscription"
      ? subscriptionsById.get(selection.subscriptionId)?.folder
      : undefined;
  useEffect(() => {
    if (selectedParent !== undefined && selectedParent !== null && folded.has(selectedParent)) {
      const unfolded = new Set(folded);
      unfolded.delete(selectedParent);
      onFolded(unfolded);
    }
  }, [selectedKey, selectedParent]);

  // The row that holds the keyboard; the rows as they were last drawn; and a row the keyboard is
  // to land on once it is drawn.
  const holder = useKeyboardHolder(tree, (element) =>
    element.closest<HTMLElement>('[role="treeitem"]')?.dataset.key,
  );
  const drawn = useRef(rows);
  const pending = useRef<Landing>(undefined);
  useOnLanding(landing, (asked) => {
    pending.current = asked;
  });
  useLayoutEffect(() => {
    const before = drawn.current;
    drawn.current = rows;
    const asked = pending.current;
    const nowhere = keyboardIsNowhere();
    // A row asked for waits to be drawn while the keyboard has nowhere to be, or is in the menu
    // that asked for it; a press of the reader's, or the keyboard gone elsewhere, ends the wait.
    if (asked !== undefined && !isOverruled(asked)) {
      const element = rowElement(asked.key);
      if (nowhere && element !== null) {
        pending.current = undefined;
        element.focus();
        return;
      }
      if (nowhere || document.activeElement?.closest(".popup") !== null) {
        return;
      }
    }
    pending.current = undefined;
    const held = holder.current;
    if (!nowhere || held === undefined) {
      return;
    }
    const at = before.findIndex((row) => row.key === held);
    const standing = rows.some((row) => row.key === held) ? held : rows[at]?.key;
    rowElement(standing ?? keyOf(ALL))?.focus();
  });

  const tabKey = rows.some((row) => row.key === active)
    ? active
    : rows.some((row) => row.key === selectedKey)
      ? selectedKey
      : keyOf(ALL);

  function rowElement(key: string): HTMLElement | null {
    return tree.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`) ?? null;
  }

  function moveTo(key: string | undefined) {
    if (key !== undefined) {
      setActive(key);
      rowElement(key)?.focus();
    }
  }

  function fold(folderId: string, folding: boolean) {
    const changed = new Set(folded);
    if (folding) {
      changed.add(folderId);
    } else {
      changed.delete(folderId);
    }
    onFolded(changed);
  }

  function select(key: string) {
    const chosen = selectionOfKey(key);
    setActive(key);
    if (chosen !== undefined) {
      onSelect(chosen);
    }
  }

  function openMenu(key: string, opening: Opening) {
    const chosen = selectionOfKey(key);
    if (chosen?.kind === "folder") {
      const folder = folders.find((each) => each.id === chosen.folderId);
      if (folder !== undefined) {
        setMenu({ kind: "folder", key, folder, opening });
      }
    } else if (chosen?.kind === "subscription") {
      const subscription = subscriptionsById.get(chosen.subscriptionId);
      if (subscription !== undefined) {
        setMenu({ kind: "subscription", key, subscription, opening });
      }
    }
  }

  const current: RowActions = {
    select,
    focus: setActive,
    openMenu,
    fold,
    rename: onRename,
    // The field gives the keyboard back to the folder's row where it had it, left by Enter or
    // Escape; where the reader took the keyboard elsewhere, it stays there.
    renamed: (key, done) => {
      setRenaming(undefined);
      if (done.kind === "given up" || (done.kind === "answered" && done.keyboard === "kept")) {
        rowElement(key)?.focus();
      }
    },
  };
  const latest = useRef(current);
  useLayoutEffect(() => {
    latest.current = current;
  });
  const actions = useMemo<RowActions>(
    () => ({
      select: (key) => latest.current.select(key),
      focus: (key) => latest.current.focus(key),
      openMenu: (key, opening) => latest.current.openMenu(key, opening),
      fold: (folderId, folding) => latest.current.fold(folderId, folding),
      rename: (folderId, name) => latest.current.rename(folderId, name),
      renamed: (key, done) => latest.current.renamed(key, done),
    }),
    [],
  );

  // The move each key makes from a row, where it makes one.
  function keyed(key: string, row: number, pressed: string): (() => void) | undefined {
    const chosen = selectionOfKey(key);
    const folderId = chosen?.kind === "folder" ? chosen.folderId : undefined;
    const isFolded = folderId !== undefined && folded.has(folderId);
    const parent = rows[row]?.parent;
    switch (pressed) {
      case "ArrowDown":
        return () => moveTo(rows[row + 1]?.key);
      case "ArrowUp":
        return () => moveTo(rows[row - 1]?.key);
      case "Home":
        return () => moveTo(rows[0]?.key);
      case "End":
        return () => moveTo(rows.at(-1)?.key);
      case "ArrowRight":
        if (folderId === undefined) {
          return () => undefined;
        }
        return isFolded
          ? () => fold(folderId, false)
          : () => rows[row + 1]?.parent === key && moveTo(rows[row + 1]?.key);
      case "ArrowLeft":
        if (folderId !== undefined && !isFolded) {
          return () => fold(folderId, true);
        }
        return () => parent !== undefined && moveTo(parent);
      case "Enter":
      case " ":
        return () => select(key);
      default:
        return undefined;
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLUListElement>) {
    const item = event.target;
    if (!(item instanceof HTMLElement) || item.getAttribute("role") !== "treeitem") {
      return;
    }
    const key = item.dataset.key ?? "";
    const row = rows.findIndex((each) => each.key === key);
    const move = isMenuKey(event)
      ? () => openMenu(key, openingAt(item))
      : keyed(key, row, event.key);
    if (row >= 0 && move !== undefined) {
      event.preventDefault();
      move();
    }
  }

  // A menu left by a key gives the keyboard back to its opener, unless what was chosen in it asks
  // for another row, as a subscription moved, which takes it once it is drawn.
  function leaveMenu(refocus: boolean) {
    const opener = menu?.opening.opener;
    setMenu(undefined);
    if (refocus && pending.current === undefined) {
      (opener?.isConnected === true ? opener : rowElement(tabKey))?.focus({
        preventScroll: true,
      });
    }
  }

  // A subscription moved by its menu takes the keyboard to its row where it stands now, its
  // folder unfolded to show it.
  async function moveSubscription(subscriptionId: string, folderId: string | null) {
    await placer.place({ folder: folderId, subscriptions: [subscriptionId], items: [] });
    if (folderId !== null && folded.has(folderId)) {
      fold(folderId, false);
    }
    pending.current = landingOn(subscriptionKey(subscriptionId));
  }

  return (
    <>
      <ul
        ref={tree}
        role="tree"
        aria-label="分类与订阅"
        className="sidebar-tree"
        onKeyDown={onKeyDown}
      >
        {FIXED.map(({ kind }) => (
          <FixedRow
            key={kind}
            kind={kind}
            // 全部 is not counted: it would show the same number as 新来源.
            fresh={FIXED_COUNTS[kind](tally)}
            selected={kind === selectedKey}
            tabbable={kind === tabKey}
            actions={actions}
          />
        ))}
        {shown.branches.map(({ folder, subscriptions: under }) => {
          const key = folderKey(folder.id);
          const keys = under.map((subscription) => subscriptionKey(subscription.id));
          return (
            <FolderRow
              key={key}
              folder={folder}
              folded={folded.has(folder.id)}
              fresh={tally.freshInFolder.get(folder.id) ?? 0}
              selected={key === selectedKey}
              tabbable={key === tabKey}
              dropping={dropOn === folder.id}
              renaming={renaming === folder.id}
              menuOpen={menu?.key === key}
              inside={under.map((subscription) => subscription.id).join(ID_SEPARATOR)}
              subscriptionsById={subscriptionsById}
              freshOfSubscription={tally.freshOfSubscription}
              selectedInside={among(keys, selectedKey)}
              tabbableInside={among(keys, tabKey)}
              menuInside={among(keys, menu?.key)}
              actions={actions}
            />
          );
        })}
        {shown.unfiled.map((subscription) => {
          const key = subscriptionKey(subscription.id);
          return (
            <SubscriptionRow
              key={key}
              subscription={subscription}
              level={1}
              fresh={tally.freshOfSubscription.get(subscription.id) ?? 0}
              selected={key === selectedKey}
              tabbable={key === tabKey}
              menuOpen={menu?.key === key}
              actions={actions}
            />
          );
        })}
      </ul>
      {menu?.kind === "folder" && (
        <FolderMenu
          opening={menu.opening}
          folder={menu.folder}
          held={heldIn(menu.folder.id)}
          onRename={() => {
            setMenu(undefined);
            setRenaming(menu.folder.id);
          }}
          onDissolve={() => onDissolve(menu.folder.id)}
          onLeave={leaveMenu}
        />
      )}
      {menu?.kind === "subscription" && (
        <PlaceMenu
          opening={menu.opening}
          what={menu.subscription.title}
          folders={placer.folders}
          current={menu.subscription.folder}
          onPlace={(folderId) => moveSubscription(menu.subscription.id, folderId)}
          onMake={placer.make}
          onLeave={leaveMenu}
        />
      )}
    </>
  );
}
