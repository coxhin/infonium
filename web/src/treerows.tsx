import {
  memo,
  useEffect,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
  type ReactNode,
} from "react";
import type { Folder, Subscription } from "./client";
import { endDrag, startDrag } from "./dragging";
import { openingAt, openingOfContextMenu, type Opening } from "./menu";
import { FOLDER_NAME_LENGTH } from "./placemenu";
import { usePress } from "./pressing";
import { FIXED_NAMES, keyOf } from "./sidebarselection";
import { stateWord } from "./subscriptions";

// The rows of the sidebar's tree, each drawn again only when its own values change: the fixed
// entries, in the order they stand; a folder, with the subscriptions under it; a subscription.
export const FIXED = [{ kind: "all" }, { kind: "new" }, { kind: "unfiled" }] as const;
// The ids of the subscriptions a folder holds, as one value a row compares cheaply.
export const ID_SEPARATOR = " ";

export function subscriptionKey(subscriptionId: string): string {
  return keyOf({ kind: "subscription", subscriptionId });
}

export function folderKey(folderId: string): string {
  return keyOf({ kind: "folder", folderId });
}

// What a screen reader hears of a row: its name, how it stands, and its count of new items. A
// folder is named by its own row alone, not by the subscriptions under it.
function heardName(name: string, fresh: number, word: string | undefined): string {
  return [name, word, fresh > 0 ? `${fresh} 篇新来源` : undefined]
    .filter((part) => part !== undefined)
    .join("，");
}

// What a row asks of the tree. The tree hands every row the same functions for as long as it
// lives, so that a row whose own values are unchanged is not drawn again when another changes,
// as when the selection moves among 250 rows.
export type RowActions = {
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

export const FixedRow = memo(function FixedRow({
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

export const SubscriptionRow = memo(function SubscriptionRow({
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
export const FolderRow = memo(function FolderRow({
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
