import {
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
} from "react";
import type { Folder, Subscription } from "./client";
import { endDrag, startDrag } from "./dragging";
import { FolderMenu, type Held } from "./foldermenu";
import type { Placer } from "./folders";
import { isMenuKey, openingAt, openingOfContextMenu, type Opening } from "./menu";
import { NewCount } from "./newmarks";
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

// A folder as the tree shows it: the subscriptions in it, in the order chosen.
type Branch = { folder: Folder; subscriptions: Subscription[] };
type Shown = { branches: Branch[]; unfiled: Subscription[] };

// What the tree shows: the folders, each with its subscriptions, and the subscriptions in no
// folder, in the order chosen. Under a media, a folder or a subscription that holds no item of
// it is left out, unless it is selected, or holds the subscription selected, or is a folder that
// holds nothing yet, which a reader has made to put things in.
function shownOf(
  folders: Folder[],
  subscriptions: Subscription[],
  tally: Tally,
  media: MediaChoice,
  order: Order,
  selection: Selection,
  occupied: ReadonlySet<string>,
): Shown {
  const known = new Set(folders.map((folder) => folder.id));
  const selectedSubscription =
    selection.kind === "subscription" ? selection.subscriptionId : undefined;
  const isShownSubscription = (subscription: Subscription) =>
    media === "any" ||
    tally.holdingSubscriptions.has(subscription.id) ||
    subscription.id === selectedSubscription;
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
  const holdingSelected = subscriptions.find((each) => each.id === selectedSubscription)?.folder;
  const isShownFolder = (folder: Folder) =>
    media === "any" ||
    tally.holdingFolders.has(folder.id) ||
    !occupied.has(folder.id) ||
    (selection.kind === "folder" && selection.folderId === folder.id) ||
    holdingSelected === folder.id;
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
  const subscriptionRow = (subscription: Subscription, parent?: string) => ({
    key: keyOf({ kind: "subscription", subscriptionId: subscription.id }),
    parent,
  });
  return [
    ...FIXED.map((fixed) => ({ key: keyOf(fixed) })),
    ...shown.branches.flatMap(({ folder, subscriptions }) => {
      const key = keyOf({ kind: "folder", folderId: folder.id });
      return [
        { key },
        ...(folded.has(folder.id)
          ? []
          : subscriptions.map((subscription) => subscriptionRow(subscription, key))),
      ];
    }),
    ...shown.unfiled.map((subscription) => subscriptionRow(subscription)),
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
  | { kind: "folder"; folder: Folder; opening: Opening }
  | { kind: "subscription"; subscription: Subscription; opening: Opening };

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
  label,
  active,
  open,
  onOpen,
}: {
  label: string;
  active: boolean;
  open: boolean;
  onOpen: (opener: HTMLElement) => void;
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
        onOpen(event.currentTarget);
      }}
    >
      <span aria-hidden="true">⋯</span>
    </button>
  );
}

// A row of the tree: an item that takes the keyboard when it is the one to, selected by a press
// or by Enter and Space, with its own menu on the pointer's other button; and, for a folder, the
// subscriptions under it.
function TreeRow({
  rowKey,
  level,
  heard,
  selected,
  tabbable,
  expanded,
  className,
  dragging,
  under,
  onPress,
  onFocused,
  onContextMenu,
  children,
}: {
  rowKey: string;
  level: number;
  heard: string;
  selected: boolean;
  tabbable: boolean;
  expanded?: boolean;
  className: string;
  dragging?: (event: DragEvent<HTMLLIElement>) => void;
  under?: ReactNode;
  onPress: () => void;
  onFocused: () => void;
  onContextMenu?: (event: MouseEvent<HTMLLIElement>) => void;
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
      onFocus={(event) => event.target === event.currentTarget && onFocused()}
      onContextMenu={onContextMenu}
    >
      <div
        className="row"
        onClick={(event) => {
          event.stopPropagation();
          onPress();
        }}
      >
        {children}
      </div>
      {under}
    </li>
  );
}

// The sidebar's tree: the fixed entries, the folders, each folded or unfolded over its
// subscriptions, and the subscriptions in no folder. ↑ and ↓ move among the rows on show, →
// unfolds a folder or goes to its first subscription, ← folds it or goes up to it, Enter and
// Space select. One row at a time takes Tab: the one the keyboard moved to last, which is the
// selection's row until the keyboard moves. A folder something is dragged onto is marked, and
// what came of a drop on it is said under it.
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
  dropRefusal,
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
  // Why the service refused the last drop, and on which folder, or on none.
  dropRefusal: { folderId: string | null; reason: string } | undefined;
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

  const shown = shownOf(folders, subscriptions, tally, media, order, selection, occupied);
  const rows = visibleRows(shown, folded);

  // The keyboard's row follows the selection, wherever it was made.
  useEffect(() => {
    setActive(selectedKey);
  }, [selectedKey]);

  // A subscription selected inside a folded folder, as one selected when the app opens, is
  // shown by unfolding its folder.
  const selectedParent =
    selection.kind === "subscription"
      ? subscriptions.find((each) => each.id === selection.subscriptionId)?.folder
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

  function openMenuOf(key: string, opening: Opening) {
    const chosen = selectionOfKey(key);
    if (chosen?.kind === "folder") {
      const folder = folders.find((each) => each.id === chosen.folderId);
      if (folder !== undefined) {
        setMenu({ kind: "folder", folder, opening });
      }
    } else if (chosen?.kind === "subscription") {
      const subscription = subscriptions.find((each) => each.id === chosen.subscriptionId);
      if (subscription !== undefined) {
        setMenu({ kind: "subscription", subscription, opening });
      }
    }
  }

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
      ? () => openMenuOf(key, openingAt(item))
      : keyed(key, row, event.key);
    if (row >= 0 && move !== undefined) {
      event.preventDefault();
      move();
    }
  }

  function contextMenuOf(key: string) {
    return (event: MouseEvent<HTMLLIElement>) => {
      event.preventDefault();
      event.stopPropagation();
      openMenuOf(key, openingOfContextMenu(event.currentTarget, event.clientX, event.clientY));
    };
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

  function subscriptionRow(subscription: Subscription, level: number) {
    const key = keyOf({ kind: "subscription", subscriptionId: subscription.id });
    const word = stateWord(subscription);
    const fresh = tally.freshOfSubscription.get(subscription.id) ?? 0;
    return (
      <TreeRow
        key={key}
        rowKey={key}
        level={level}
        heard={heardName(subscription.title, fresh, word)}
        selected={key === selectedKey}
        tabbable={key === tabKey}
        className="subscription"
        dragging={(event) => {
          event.stopPropagation();
          startDrag(event, {
            kind: "subscription",
            id: subscription.id,
            folder: subscription.folder,
          });
        }}
        onPress={() => select(key)}
        onFocused={() => setActive(key)}
        onContextMenu={contextMenuOf(key)}
      >
        <span className="name" dir="auto" title={subscription.title}>
          {subscription.title}
        </span>
        {word !== undefined && (
          <small className={subscription.poll.kind === "failed" ? "failed" : undefined}>
            {word}
          </small>
        )}
        {fresh > 0 && <NewCount count={fresh} />}
        <RowMenuButton
          label={`移动订阅“${subscription.title}”…`}
          active={key === tabKey}
          open={menu?.kind === "subscription" && menu.subscription.id === subscription.id}
          onOpen={(opener) => openMenuOf(key, openingAt(opener))}
        />
      </TreeRow>
    );
  }

  function folderRow({ folder, subscriptions: inside }: Branch) {
    const key = keyOf({ kind: "folder", folderId: folder.id });
    const isFolded = folded.has(folder.id);
    const fresh = tally.freshInFolder.get(folder.id) ?? 0;
    return (
      <TreeRow
        key={key}
        rowKey={key}
        level={1}
        heard={heardName(folder.name, fresh, undefined)}
        selected={key === selectedKey}
        tabbable={key === tabKey}
        expanded={!isFolded}
        className={dropOn === folder.id ? "folder dropping" : "folder"}
        under={
          <>
            {dropRefusal?.folderId === folder.id && (
              <p className="status failed" role="alert">
                没有移到“{folder.name}”：{dropRefusal.reason}
              </p>
            )}
            {!isFolded && inside.length > 0 && (
              <ul role="group">{inside.map((subscription) => subscriptionRow(subscription, 2))}</ul>
            )}
          </>
        }
        onPress={() => select(key)}
        onFocused={() => setActive(key)}
        onContextMenu={contextMenuOf(key)}
      >
        <span
          className="twisty"
          aria-hidden="true"
          onClick={(event) => {
            event.stopPropagation();
            fold(folder.id, !isFolded);
          }}
        >
          {isFolded ? "▸" : "▾"}
        </span>
        {renaming === folder.id ? (
          <RenameField
            folder={folder}
            onRename={(name) => onRename(folder.id, name)}
            onDone={() => {
              setRenaming(undefined);
              requestAnimationFrame(() => rowElement(key)?.focus());
            }}
          />
        ) : (
          <span className="name" dir="auto" title={folder.name}>
            {folder.name}
          </span>
        )}
        {fresh > 0 && <NewCount count={fresh} />}
        <RowMenuButton
          label={`分类“${folder.name}”的操作`}
          active={key === tabKey}
          open={menu?.kind === "folder" && menu.folder.id === folder.id}
          onOpen={(opener) => openMenuOf(key, openingAt(opener))}
        />
      </TreeRow>
    );
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
        {FIXED.map((fixed) => {
          const key = keyOf(fixed);
          // Only 新来源 is counted: 全部 would show the same number, and the hand-added items
          // under 未分类 are new only where their subscription was removed.
          const fresh = fixed.kind === "new" ? tally.fresh : 0;
          return (
            <TreeRow
              key={key}
              rowKey={key}
              level={1}
              heard={heardName(FIXED_NAMES[fixed.kind], fresh, undefined)}
              selected={key === selectedKey}
              tabbable={key === tabKey}
              className={fixed.kind === "unfiled" ? "fixed last-fixed" : "fixed"}
              onPress={() => select(key)}
              onFocused={() => setActive(key)}
            >
              <span className="name">{FIXED_NAMES[fixed.kind]}</span>
              {fresh > 0 && <NewCount count={fresh} />}
              <span className="row-menu-room" />
            </TreeRow>
          );
        })}
        {shown.branches.map(folderRow)}
        {shown.unfiled.map((subscription) => subscriptionRow(subscription, 1))}
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
