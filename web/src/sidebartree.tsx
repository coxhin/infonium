import {
  memo,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type { Folder, Subscription } from "./client";
import { endDrag, startDrag } from "./dragging";
import { FolderMenu, type Held } from "./foldermenu";
import type { Placer } from "./folders";
import { isMenuKey, openingAt, openingOfContextMenu, type Opening } from "./menu";
import { FOLDER_NAME_LENGTH, PlaceMenu } from "./placemenu";
import { usePress } from "./pressing";
import {
  ALL,
  FIXED_NAMES,
  keyOf,
  ordered,
  selectionOfKey,
  type MediaChoice,
  type Order,
  type Selection,
  type Tally,
} from "./selection";
import { stateWord } from "./subscriptions";

const FIXED = [{ kind: "all" }, { kind: "new" }, { kind: "unfiled" }] as const;
// The ids of the subscriptions a folder holds, as one value a row compares cheaply.
const ID_SEPARATOR = " ";

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

function subscriptionKey(subscriptionId: string): string {
  return keyOf({ kind: "subscription", subscriptionId });
}

function folderKey(folderId: string): string {
  return keyOf({ kind: "folder", folderId });
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

// What a screen reader hears of a row: its name, how it stands, and its count of new items. A
// folder is named by its own row alone, not by the subscriptions under it.
function heardName(name: string, fresh: number, word: string | undefined): string {
  return [name, word, fresh > 0 ? `${fresh} 篇新来源` : undefined]
    .filter((part) => part !== undefined)
    .join("，");
}

type MenuOpen =
  | { kind: "folder"; key: string; folder: Folder; opening: Opening }
  | { kind: "subscription"; key: string; subscription: Subscription; opening: Opening };

// What a row asks of the tree. The tree hands every row the same functions for as long as it
// lives, so that a row whose own values are unchanged is not drawn again when another changes,
// as when the selection moves among 250 rows.
type RowActions = {
  select: (key: string) => void;
  focus: (key: string) => void;
  openMenu: (key: string, opening: Opening) => void;
  fold: (folderId: string, folding: boolean) => void;
  rename: (folderId: string, name: string) => Promise<void>;
  renamed: (key: string) => void;
};

// A folder's name, changed in its place: Enter or leaving the field sends a changed name,
// Escape keeps the name it had. A refusal is said under the field, which keeps what was typed.
function RenameField({
  folder,
  onRename,
  onDone,
}: {
  folder: Folder;
  onRename: (name: string) => Promise<void>;
  onDone: () => void;
}) {
  const [name, setName] = useState(folder.name);
  const { press, ask } = usePress();
  const field = useRef<HTMLInputElement>(null);
  useEffect(() => {
    field.current?.select();
  }, []);

  function send(event?: FormEvent) {
    event?.preventDefault();
    const trimmed = name.trim();
    if (trimmed === "" || trimmed === folder.name) {
      onDone();
      return;
    }
    ask(() => onRename(trimmed), onDone);
  }

  return (
    <form className="rename" onSubmit={send} onClick={(event) => event.stopPropagation()}>
      <input
        ref={field}
        aria-label={`分类“${folder.name}”的新名称`}
        maxLength={FOLDER_NAME_LENGTH}
        readOnly={press.state === "asking"}
        value={name}
        onChange={(event) => setName(event.target.value)}
        onKeyDown={(event) => {
          event.stopPropagation();
          if (event.key === "Escape") {
            event.preventDefault();
            onDone();
          }
        }}
        onBlur={() => press.state === "idle" && send()}
      />
      {press.state === "refused" && (
        <p className="status failed" role="alert">
          没有改名：{press.reason}
        </p>
      )}
    </form>
  );
}

// A row's ⋯, which the keyboard reaches by Tab from the row it stands on.
function RowMenuButton({
  rowKey,
  label,
  active,
  open,
  actions,
}: {
  rowKey: string;
  label: string;
  active: boolean;
  open: boolean;
  actions: RowActions;
}) {
  return (
    <button
      type="button"
      className="row-menu"
      tabIndex={active ? 0 : -1}
      aria-label={label}
      aria-haspopup="menu"
      aria-expanded={open}
      title={label}
      onClick={(event) => {
        event.stopPropagation();
        actions.openMenu(rowKey, openingAt(event.currentTarget));
      }}
    >
      ⋯
    </button>
  );
}

// A row's count of new items, which a screen reader hears in the row's name.
function RowCount({ count }: { count: number }) {
  return count > 0 ? (
    <span className="new-count" aria-hidden="true">
      {count}
    </span>
  ) : null;
}

// A row of the tree: an item that takes the keyboard when it is the one to, selected by a press
// or by Enter and Space, with its menu, where it has one, on the pointer's other button; and,
// for a folder, the subscriptions under it.
function TreeRow({
  rowKey,
  level,
  heard,
  selected,
  tabbable,
  expanded,
  className,
  menu,
  dragging,
  under,
  actions,
  children,
}: {
  rowKey: string;
  level: number;
  heard: string;
  selected: boolean;
  tabbable: boolean;
  expanded?: boolean;
  className: string;
  menu: boolean;
  dragging?: (event: DragEvent<HTMLLIElement>) => void;
  under?: ReactNode;
  actions: RowActions;
  children: ReactNode;
}) {
  return (
    <li
      role="treeitem"
      data-key={rowKey}
      aria-level={level}
      aria-selected={selected}
      aria-expanded={expanded}
      aria-label={heard}
      tabIndex={tabbable ? 0 : -1}
      className={className}
      draggable={dragging !== undefined}
      onDragStart={dragging}
      onDragEnd={dragging === undefined ? undefined : endDrag}
      onFocus={(event) => event.target === event.currentTarget && actions.focus(rowKey)}
      onContextMenu={
        menu
          ? (event) => {
              event.preventDefault();
              event.stopPropagation();
              actions.openMenu(
                rowKey,
                openingOfContextMenu(event.currentTarget, event.clientX, event.clientY),
              );
            }
          : undefined
      }
    >
      <div
        className="row"
        onClick={(event) => {
          event.stopPropagation();
          actions.select(rowKey);
        }}
      >
        {children}
      </div>
      {under}
    </li>
  );
}

const FixedRow = memo(function FixedRow({
  kind,
  fresh,
  selected,
  tabbable,
  actions,
}: {
  kind: (typeof FIXED)[number]["kind"];
  fresh: number;
  selected: boolean;
  tabbable: boolean;
  actions: RowActions;
}) {
  return (
    <TreeRow
      rowKey={kind}
      level={1}
      heard={heardName(FIXED_NAMES[kind], fresh, undefined)}
      selected={selected}
      tabbable={tabbable}
      className={kind === "unfiled" ? "fixed last-fixed" : "fixed"}
      menu={false}
      actions={actions}
    >
      <span className="name">{FIXED_NAMES[kind]}</span>
      <RowCount count={fresh} />
      <span className="row-menu-room" />
    </TreeRow>
  );
});

const SubscriptionRow = memo(function SubscriptionRow({
  subscription,
  level,
  fresh,
  selected,
  tabbable,
  menuOpen,
  actions,
}: {
  subscription: Subscription;
  level: number;
  fresh: number;
  selected: boolean;
  tabbable: boolean;
  menuOpen: boolean;
  actions: RowActions;
}) {
  const key = subscriptionKey(subscription.id);
  const word = stateWord(subscription);
  return (
    <TreeRow
      rowKey={key}
      level={level}
      heard={heardName(subscription.title, fresh, word)}
      selected={selected}
      tabbable={tabbable}
      className="subscription"
      menu
      dragging={(event) => {
        event.stopPropagation();
        startDrag(event, {
          kind: "subscription",
          id: subscription.id,
          folder: subscription.folder,
        });
      }}
      actions={actions}
    >
      <span className="name" dir="auto" title={subscription.title}>
        {subscription.title}
      </span>
      {word !== undefined && (
        <small className={subscription.poll.kind === "failed" ? "failed" : undefined}>{word}</small>
      )}
      <RowCount count={fresh} />
      <RowMenuButton
        rowKey={key}
        label={`移动订阅“${subscription.title}”…`}
        active={tabbable}
        open={menuOpen}
        actions={actions}
      />
    </TreeRow>
  );
});

// A folder's row and, unfolded, the rows of the subscriptions under it. Of those, it is told
// only the one selected, the one that takes Tab and the one whose menu is open, where they are
// among its own, so that a selection elsewhere leaves it as it is.
const FolderRow = memo(function FolderRow({
  folder,
  folded,
  fresh,
  selected,
  tabbable,
  dropping,
  renaming,
  menuOpen,
  inside,
  subscriptionsById,
  freshOfSubscription,
  selectedInside,
  tabbableInside,
  menuInside,
  actions,
}: {
  folder: Folder;
  folded: boolean;
  fresh: number;
  selected: boolean;
  tabbable: boolean;
  dropping: boolean;
  renaming: boolean;
  menuOpen: boolean;
  // The ids of the subscriptions under it, in order.
  inside: string;
  subscriptionsById: ReadonlyMap<string, Subscription>;
  freshOfSubscription: ReadonlyMap<string, number>;
  selectedInside: string | undefined;
  tabbableInside: string | undefined;
  menuInside: string | undefined;
  actions: RowActions;
}) {
  const key = folderKey(folder.id);
  const ids = inside === "" ? [] : inside.split(ID_SEPARATOR);
  return (
    <TreeRow
      rowKey={key}
      level={1}
      heard={heardName(folder.name, fresh, undefined)}
      selected={selected}
      tabbable={tabbable}
      expanded={!folded}
      className={dropping ? "folder dropping" : "folder"}
      menu
      actions={actions}
      under={
        !folded &&
        ids.length > 0 && (
          <ul role="group">
            {ids.map((id) => {
              const subscription = subscriptionsById.get(id);
              const rowKey = subscriptionKey(id);
              return (
                subscription !== undefined && (
                  <SubscriptionRow
                    key={rowKey}
                    subscription={subscription}
                    level={2}
                    fresh={freshOfSubscription.get(id) ?? 0}
                    selected={rowKey === selectedInside}
                    tabbable={rowKey === tabbableInside}
                    menuOpen={rowKey === menuInside}
                    actions={actions}
                  />
                )
              );
            })}
          </ul>
        )
      }
    >
      <span
        className="twisty"
        aria-hidden="true"
        onClick={(event) => {
          event.stopPropagation();
          actions.fold(folder.id, !folded);
        }}
      >
        {folded ? "▸" : "▾"}
      </span>
      {renaming ? (
        <RenameField
          folder={folder}
          onRename={(name) => actions.rename(folder.id, name)}
          onDone={() => actions.renamed(key)}
        />
      ) : (
        <span className="name" dir="auto" title={folder.name}>
          {folder.name}
        </span>
      )}
      <RowCount count={fresh} />
      <RowMenuButton
        rowKey={key}
        label={`分类“${folder.name}”的操作`}
        active={tabbable}
        open={menuOpen}
        actions={actions}
      />
    </TreeRow>
  );
});

// The one of the keys under a folder that is the key looked for, if it is among them.
function among(keys: string[], key: string | undefined): string | undefined {
  return key !== undefined && keys.includes(key) ? key : undefined;
}

// The sidebar's tree: the fixed entries, the folders, each folded or unfolded over its
// subscriptions, and the subscriptions in no folder. ↑ and ↓ move among the rows on show, →
// unfolds a folder or goes to its first subscription, ← folds it or goes up to it, Enter and
// Space select. One row at a time takes Tab: the one the keyboard moved to last, which is the
// selection's row until the keyboard moves. A folder something is dragged onto is marked.
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
  focusKey,
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
  // A row the keyboard is sent to from outside the tree, as a folder just made, each time the
  // value changes.
  focusKey: { key: string } | undefined;
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

  useEffect(() => {
    if (focusKey !== undefined) {
      setActive(focusKey.key);
      requestAnimationFrame(() => rowElement(focusKey.key)?.focus());
    }
  }, [focusKey]);

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
    renamed: (key) => {
      setRenaming(undefined);
      requestAnimationFrame(() => rowElement(key)?.focus());
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
      renamed: (key) => latest.current.renamed(key),
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

  function leaveMenu(refocus: boolean) {
    const opener = menu?.opening.opener;
    setMenu(undefined);
    if (refocus) {
      (opener?.isConnected === true ? opener : rowElement(tabKey))?.focus({
        preventScroll: true,
      });
    }
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
            // Only 新来源 is counted: 全部 would show the same number, and the hand-added items
            // under 未分类 are new only where their subscription was removed.
            fresh={kind === "new" ? tally.fresh : 0}
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
          onPlace={(folderId) =>
            placer.place({ folder: folderId, subscriptions: [menu.subscription.id], items: [] })
          }
          onMake={placer.make}
          onLeave={leaveMenu}
        />
      )}
    </>
  );
}
