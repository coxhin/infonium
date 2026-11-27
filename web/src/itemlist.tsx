import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
  type MouseEvent,
} from "react";
import { importLink, importUpload, requestReason, type ItemEntry } from "./client";
import { endDrag, startDrag } from "./dragging";
import { isNew, itemTitle, originLabel } from "./entry";
import type { Placer } from "./folders";
import { Interruption } from "./interruption";
import { keyboardIsNowhere, useKeyboardHolder } from "./keyboarding";
import { itemLink } from "./links";
import { reasonOf, type Loaded } from "./loading";
import { MediaIcon, NO_MEDIA_NAME } from "./mediaicon";
import { isMenuKey, openingAt, openingOfContextMenu, type Opening } from "./menu";
import { NewDot, NewHeard } from "./newmarks";
import { PlaceMenu } from "./placemenu";
import { MEDIA_NAMES, followed } from "./sidebarselection";
import "./heard.css";

type Importing =
  | { state: "idle" }
  | { state: "running" }
  | { state: "failed"; reason: string }
  | { state: "unplaced"; reason: string };

const ATTEMPT_NOTES: Record<ItemEntry["reading"]["attempt"]["kind"], string> = {
  underway: " · 读取中…",
  settled: "",
  failed: " · 读取失败",
};
// Kinds of file the upload dialog offers; what a file really is, the server tells from its bytes.
const UPLOADABLE = "application/pdf,audio/*,video/*,text/html,.html,.htm";
// How many rows are drawn at first, a window and more of them, and how many more each time the
// list is scrolled near the last drawn: a list of thousands is drawn as it is reached, so that
// it shows at once.
const ROWS_AT_A_TIME = 60;

// Where an import goes: into the folder selected, or where the service puts it.
export type ImportInto = { folderId: string; name: string } | undefined;

// The rows drawn of a list, more as the last of them comes near; as many as at first again
// whenever the list shown is another.
function useRowsDrawn(total: number, listKey: string) {
  const [drawn, setDrawn] = useState({ listKey, count: ROWS_AT_A_TIME });
  const count = drawn.listKey === listKey ? drawn.count : ROWS_AT_A_TIME;
  const end = useRef<HTMLLIElement>(null);
  useEffect(() => {
    const element = end.current;
    if (element === null || count >= total) {
      return undefined;
    }
    const watcher = new IntersectionObserver(
      (seen) => {
        if (seen.some((each) => each.isIntersecting)) {
          setDrawn({ listKey, count: count + ROWS_AT_A_TIME });
        }
      },
      { rootMargin: "400px 0px" },
    );
    watcher.observe(element);
    return () => watcher.disconnect();
  }, [count, total, listKey]);
  return { count, end };
}

// The menu open of an item, and where it opened.
type MenuOpen = { entry: ItemEntry; opening: Opening };

// The row that holds the keyboard: its item, and whether on its link or on its ⋯.
type HeldRow = { itemId: string; control: "link" | "menu" };

function heldRowOf(element: Element): HeldRow | undefined {
  const itemId = element.closest<HTMLElement>("li[data-item]")?.dataset.item;
  return itemId === undefined
    ? undefined
    : { itemId, control: element.closest(".row-menu") === null ? "link" : "menu" };
}

// The items the selection lists, in the order the service gives them, each with the icon of its
// media and where it came from, a subscription by its title. Above them, where the selection
// takes them, adding by an address or a file. A hand-added item can be moved between folders by
// its menu or by dragging it onto the sidebar; an item that follows its subscription says so. A
// row that leaves the list with the keyboard in it, as one moved elsewhere or one of 新来源
// seen, hands the keyboard to the row that stands in its place, or to the one before it at the
// end of the list, on the same control where that row has it; to the list's head where none is
// left.
export function ItemList({
  entries,
  listed,
  listKey,
  importable,
  importInto,
  emptyText,
  interrupted,
  refusal,
  selected,
  seen,
  present,
  subscriptionTitles,
  subscriptionFolders,
  placer,
  onImported,
  onEmptied,
}: {
  entries: Loaded<ItemEntry[]>;
  listed: ItemEntry[];
  // Names the list shown, so that the rows drawn start again when it changes.
  listKey: string;
  importable: boolean;
  importInto: ImportInto;
  emptyText: string;
  interrupted: boolean;
  // Why reading the list again failed, where the service said so.
  refusal: string | undefined;
  selected: string | undefined;
  seen: ReadonlySet<string>;
  // The subscriptions there are, which items that they brought follow.
  present: ReadonlySet<string>;
  subscriptionTitles: ReadonlyMap<string, string>;
  // The name of the folder each subscription is in, of those in one.
  subscriptionFolders: ReadonlyMap<string, string>;
  placer: Placer;
  onImported: (entry: ItemEntry) => void;
  // The last row has left the list with the keyboard in it.
  onEmptied: () => void;
}) {
  const [url, setUrl] = useState("");
  const [importing, setImporting] = useState<Importing>({ state: "idle" });
  const [menu, setMenu] = useState<MenuOpen>();
  const rows = useRowsDrawn(listed.length, listKey);
  const describedPrefix = useId();
  const list = useRef<HTMLUListElement>(null);
  const holder = useKeyboardHolder(list, heldRowOf);
  const drawnList = useRef(listed);

  useLayoutEffect(() => {
    const before = drawnList.current;
    drawnList.current = listed;
    const held = holder.current;
    if (held === undefined || !keyboardIsNowhere()) {
      return;
    }
    const at = before.findIndex((entry) => entry.id === held.itemId);
    const standing = listed.some((entry) => entry.id === held.itemId)
      ? held.itemId
      : at < 0
        ? undefined
        : listed[Math.min(at, listed.length - 1)]?.id;
    if (standing === undefined) {
      if (at >= 0) {
        onEmptied();
      }
      return;
    }
    const row = list.current?.querySelector(`li[data-item="${CSS.escape(standing)}"]`);
    const control =
      held.control === "menu" ? (row?.querySelector<HTMLElement>(".row-menu") ?? null) : null;
    (control ?? row?.querySelector<HTMLElement>("a"))?.focus();
  });

  // An import into a folder is placed there once the service has answered it; the item is
  // opened either way, and a placing the service refused is said under the form.
  async function imported(importingItem: Promise<ItemEntry>) {
    const into = importInto;
    setImporting({ state: "running" });
    let entry: ItemEntry;
    try {
      entry = await importingItem;
    } catch (error) {
      setImporting({ state: "failed", reason: reasonOf(error) });
      return;
    }
    if (into === undefined) {
      setImporting({ state: "idle" });
      onImported(entry);
      return;
    }
    try {
      await placer.place({ folder: into.folderId, subscriptions: [], items: [entry.id] });
      setImporting({ state: "idle" });
      onImported(entry);
    } catch (error) {
      setImporting({
        state: "unplaced",
        reason: `已导入，但没有放进分类“${into.name}”：${requestReason(error)}`,
      });
      onImported(entry);
    }
  }

  function importUrl(event: FormEvent) {
    event.preventDefault();
    void imported(importLink(url).then((entry) => (setUrl(""), entry)));
  }

  function importFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    // The same file can be chosen again only once the field has let go of it.
    event.target.value = "";
    if (file !== undefined) {
      void imported(importUpload(file));
    }
  }

  // An item that a subscription brought is told by the subscription's title, and by its site
  // once the subscription is gone.
  function sourceName(entry: ItemEntry): string {
    return entry.origin.kind === "subscribed"
      ? (subscriptionTitles.get(entry.origin.subscription) ?? originLabel(entry.origin))
      : originLabel(entry.origin);
  }

  // Of an item that follows its subscription: where it is, and how it moves.
  function followingText(subscription: string): string {
    const title = subscriptionTitles.get(subscription) ?? "";
    const folder = subscriptionFolders.get(subscription);
    return folder === undefined
      ? `跟着订阅“${title}”，不在分类里；要移动它，请移动订阅。`
      : `跟着订阅“${title}”放在分类“${folder}”里；要移动它，请移动订阅。`;
  }

  function openMenu(entry: ItemEntry, opening: Opening) {
    setMenu({ entry, opening });
  }

  function onRowKey(event: KeyboardEvent<HTMLAnchorElement>, entry: ItemEntry) {
    if (isMenuKey(event)) {
      event.preventDefault();
      openMenu(entry, openingAt(event.currentTarget));
    }
  }

  function onRowContextMenu(event: MouseEvent<HTMLAnchorElement>, entry: ItemEntry) {
    event.preventDefault();
    openMenu(entry, openingOfContextMenu(event.currentTarget, event.clientX, event.clientY));
  }

  const loaded = entries.state === "ready";
  const shown = listed.slice(0, rows.count);
  return (
    <>
      {importable && (
        <>
          <form onSubmit={importUrl}>
            <input
              type="url"
              required
              placeholder="粘贴网址：文章、PDF、视频、播客"
              aria-label={
                importInto === undefined ? "要导入的网址" : `要导入到分类“${importInto.name}”的网址`
              }
              value={url}
              onChange={(event) => setUrl(event.target.value)}
            />
            <button disabled={importing.state === "running"}>
              {importing.state === "running" ? "导入中…" : "导入"}
            </button>
          </form>
          <label className="upload" aria-disabled={importing.state === "running"}>
            <input
              type="file"
              accept={UPLOADABLE}
              disabled={importing.state === "running"}
              onChange={importFile}
            />
            上传 PDF、音频或视频
          </label>
          {importInto !== undefined && (
            <p className="into">导入的文章会放进分类“{importInto.name}”。</p>
          )}
          {(importing.state === "failed" || importing.state === "unplaced") && (
            <p className="status failed" role="alert">
              {importing.reason}
            </p>
          )}
        </>
      )}
      {entries.state === "failed" && <p className="status failed">{entries.reason}</p>}
      {interrupted && <Interruption refusal={refusal} />}
      {loaded && listed.length === 0 && <p className="status">{emptyText}</p>}
      {shown.length > 0 && (
        <ul ref={list} className="items">
          {shown.map((entry) => {
            const fresh = isNew(entry, seen);
            const source = sourceName(entry);
            const title = itemTitle(entry);
            const following = followed(entry, present);
            const describedId = `${describedPrefix}${entry.id}`;
            return (
              <li key={entry.id} data-item={entry.id}>
                <a
                  href={itemLink(entry.id)}
                  className={fresh ? "new" : undefined}
                  aria-current={entry.id === selected}
                  draggable={following === undefined}
                  title={following === undefined ? undefined : followingText(following)}
                  aria-describedby={following === undefined ? undefined : describedId}
                  onDragStart={(event) =>
                    following === undefined &&
                    startDrag(event, { kind: "item", id: entry.id, folder: entry.folder })
                  }
                  onDragEnd={endDrag}
                  onKeyDown={(event) => following === undefined && onRowKey(event, entry)}
                  onContextMenu={(event) =>
                    following === undefined && onRowContextMenu(event, entry)
                  }
                >
                  <MediaIcon
                    media={entry.media ?? "none"}
                    heardAs={entry.media === null ? NO_MEDIA_NAME : MEDIA_NAMES[entry.media]}
                  />
                  <span className="item-text">
                    <span className="item-title" dir="auto">
                      {fresh && <NewDot />}
                      {title}
                      {fresh && <NewHeard />}
                    </span>
                    <small>
                      <span className="source" dir="auto" title={source}>
                        {source}
                      </span>
                      <span className="attempt">{ATTEMPT_NOTES[entry.reading.attempt.kind]}</span>
                    </small>
                  </span>
                </a>
                {following === undefined ? (
                  <button
                    className="row-menu"
                    aria-label={`移动“${title}”…`}
                    aria-haspopup="menu"
                    aria-expanded={menu?.entry.id === entry.id}
                    title="移到分类…"
                    onClick={(event) => openMenu(entry, openingAt(event.currentTarget))}
                  >
                    <span aria-hidden="true">⋯</span>
                  </button>
                ) : (
                  <span id={describedId} className="heard">
                    {followingText(following)}
                  </span>
                )}
              </li>
            );
          })}
          {rows.count < listed.length && (
            <li ref={rows.end} className="status more">
              还有 {listed.length - rows.count} 篇…
            </li>
          )}
        </ul>
      )}
      {menu !== undefined && (
        <PlaceMenu
          opening={menu.opening}
          what={itemTitle(menu.entry)}
          folders={placer.folders}
          current={menu.entry.folder}
          onPlace={(folderId) =>
            placer.place({ folder: folderId, subscriptions: [], items: [menu.entry.id] })
          }
          onMake={placer.make}
          onLeave={(refocus) => {
            setMenu(undefined);
            if (refocus) {
              menu.opening.opener.focus({ preventScroll: true });
            }
          }}
        />
      )}
    </>
  );
}
