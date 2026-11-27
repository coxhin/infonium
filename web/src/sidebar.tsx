import {
  useDeferredValue,
  useEffect,
  useId,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import type { Browsing } from "./browsing";
import { requestReason, type Folder, type Subscription } from "./client";
import { draggedOver, endDrag, type Dragged } from "./dragging";
import { FOLDER_NAME_LENGTH } from "./folders";
import { Interruption } from "./interruption";
import type { Watched } from "./loading";
import { MediaIcon } from "./mediaicon";
import { usePress } from "./pressing";
import { SHORTCUT_NAME, type SidebarLayout } from "./sidebarlayout";
import {
  MEDIA_CHOICES,
  MEDIA_NAMES,
  keyOf,
  selectionOfKey,
  type MediaChoice,
  type Order,
  type Selection,
} from "./sidebarselection";
import { SidebarTree } from "./sidebartree";
import "./heard.css";
import "./sidebar.css";

const ORDER_NAMES: Record<Order, string> = { name: "按名称排列", new: "按新来源数排列" };

// A folder's name asked for in place of the press that makes one: Enter makes it, Escape or
// leaving the field empty gives it up. A refusal is said under the field, which keeps what was
// typed.
function NewFolderField({
  onMake,
  onDone,
}: {
  onMake: (name: string) => Promise<Folder>;
  onDone: (made: Folder | undefined) => void;
}) {
  const [name, setName] = useState("");
  const { press, ask } = usePress();

  function make(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (trimmed === "") {
      onDone(undefined);
      return;
    }
    ask(() => onMake(trimmed), onDone);
  }

  return (
    <form className="new-folder" onSubmit={make}>
      <div className="new-folder-row">
        <input
          autoFocus
          aria-label="新分类的名称"
          placeholder="新分类的名称"
          maxLength={FOLDER_NAME_LENGTH}
          readOnly={press.state === "asking"}
          value={name}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              event.stopPropagation();
              onDone(undefined);
            }
          }}
          onBlur={() => name.trim() === "" && press.state === "idle" && onDone(undefined)}
        />
        <button aria-disabled={press.state === "asking"}>
          {press.state === "asking" ? "新建中…" : "新建"}
        </button>
      </div>
      {press.state === "refused" && (
        <p className="status failed" role="alert">
          没有新建分类：{press.reason}
        </p>
      )}
    </form>
  );
}

// The sidebar's own failures: a list it could not read, with why and a way to try again, and a
// service that does not answer.
function ListFailures({
  folders,
  subscriptions,
}: {
  folders: Watched<Folder[]>;
  subscriptions: Watched<Subscription[]>;
}) {
  return (
    <>
      {folders.loaded.state === "failed" && (
        <p className="status failed">
          分类没有读到：{folders.loaded.reason}
          <button onClick={folders.readNow}>重试</button>
        </p>
      )}
      {subscriptions.loaded.state === "failed" && (
        <p className="status failed">
          订阅没有读到：{subscriptions.loaded.reason}
          <button onClick={subscriptions.readNow}>重试</button>
        </p>
      )}
      {(folders.interrupted || subscriptions.interrupted) && (
        <Interruption refusal={subscriptions.refusal ?? folders.refusal} />
      )}
    </>
  );
}

// The whole sidebar: the media, the tree, and at its foot the presses that make a folder and
// subscribe, the order, and the press that folds it. Shown as a column, or over the page, where a
// selection, Escape, a press outside it or the keyboard leaving it closes it. Dragged onto it
// outside the folders, a subscription or an item leaves its folder.
function SidebarBody({
  overlaid,
  browsing,
  layout,
}: {
  overlaid: boolean;
  browsing: Browsing;
  layout: SidebarLayout;
}) {
  const own = useRef<HTMLElement>(null);
  const makeButton = useRef<HTMLButtonElement>(null);
  const mediaName = useId();
  const [naming, setNaming] = useState(false);
  const [focusKey, setFocusKey] = useState<{ key: string }>();
  // The folder what is dragged would go into, none where it would leave its folder, and nothing
  // where no drag is over the sidebar.
  const [dropOn, setDropOn] = useState<string | null>();
  // Why the service refused the last drop, said at the sidebar's head, which stays in view
  // whether the folder dropped on does or not.
  const [dropRefusal, setDropRefusal] = useState<string>();
  const { tally, media, order, selection, folders, subscriptions } = browsing;
  // A media chosen narrows the list column at once, and the tree, which may draw again
  // hundreds of rows, after it, in pieces that leave the page free to answer meanwhile.
  const treeMedia = useDeferredValue(media);
  const treeTally = useDeferredValue(tally);
  const folderList = folders.loaded.state === "ready" ? folders.loaded.value : [];
  const subscriptionList = subscriptions.loaded.state === "ready" ? subscriptions.loaded.value : [];

  // Opened over the page, the sidebar takes the keyboard to its tree.
  useEffect(() => {
    if (overlaid) {
      own.current?.querySelector<HTMLElement>('.sidebar-tree [tabindex="0"]')?.focus();
    }
  }, [overlaid]);

  useEffect(() => {
    if (!overlaid) {
      return undefined;
    }
    const pressed = (event: PointerEvent) => {
      const target = event.target;
      if (
        target instanceof Element &&
        !own.current?.contains(target) &&
        target.closest(".popup, .rail") === null
      ) {
        layout.close();
      }
    };
    addEventListener("pointerdown", pressed, true);
    return () => removeEventListener("pointerdown", pressed, true);
  }, [overlaid, layout.close]);

  function select(chosen: Selection) {
    browsing.select(chosen);
    if (overlaid) {
      layout.close();
    }
  }

  function subscribe() {
    browsing.startSubscribing();
    if (overlaid) {
      layout.close();
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (overlaid && event.key === "Escape") {
      event.preventDefault();
      layout.close();
    }
  }

  // Where what is dragged over the sidebar goes: into the folder it is over, a subscription
  // under a folder counting as that folder, and anywhere else out of its folder; nowhere where
  // it is already.
  function dropOf(
    event: DragEvent<HTMLElement>,
  ): { dragged: Dragged; folderId: string | null } | undefined {
    const dragged = draggedOver(event);
    if (dragged === undefined || !(event.target instanceof Element)) {
      return undefined;
    }
    const over = selectionOfKey(event.target.closest<HTMLElement>("li.folder")?.dataset.key ?? "");
    const folderId = over?.kind === "folder" ? over.folderId : null;
    return dragged.folder === folderId ? undefined : { dragged, folderId };
  }

  function onDragOver(event: DragEvent<HTMLElement>) {
    const drop = dropOf(event);
    setDropOn(drop?.folderId);
    if (drop !== undefined) {
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
    }
  }

  function onDrop(event: DragEvent<HTMLElement>) {
    const drop = dropOf(event);
    setDropOn(undefined);
    endDrag();
    if (drop === undefined) {
      return;
    }
    event.preventDefault();
    const { dragged, folderId } = drop;
    const doing =
      folderId === null
        ? "没有移出分类"
        : `没有移到“${folderList.find((folder) => folder.id === folderId)?.name ?? ""}”`;
    setDropRefusal(undefined);
    browsing.placer
      .place({
        folder: folderId,
        subscriptions: dragged.kind === "subscription" ? [dragged.id] : [],
        items: dragged.kind === "item" ? [dragged.id] : [],
      })
      .catch((error: unknown) => setDropRefusal(`${doing}：${requestReason(error)}`));
  }

  // A drag that ends anywhere leaves nothing marked.
  useEffect(() => {
    const ended = () => setDropOn(undefined);
    addEventListener("dragend", ended);
    addEventListener("drop", ended);
    return () => {
      removeEventListener("dragend", ended);
      removeEventListener("drop", ended);
    };
  }, []);

  // Over the list column, the press keeps the sidebar open as a column; in one column, and as a
  // column, it folds it.
  const folding =
    overlaid && layout.width !== "narrow"
      ? { label: "展开侧栏", glyph: "»", press: layout.unfold }
      : { label: "收起侧栏", glyph: "«", press: overlaid ? layout.close : layout.fold };

  return (
    <nav
      ref={own}
      className="sidebar"
      data-overlaid={overlaid}
      data-dropping={dropOn === null}
      aria-label="侧栏"
      onKeyDown={onKeyDown}
      onBlur={(event) => {
        const next = event.relatedTarget;
        if (
          overlaid &&
          next instanceof Element &&
          !own.current?.contains(next) &&
          next.closest(".popup") === null
        ) {
          layout.close();
        }
      }}
      onDragOver={onDragOver}
      onDragLeave={(event) => {
        if (!(event.relatedTarget instanceof Node && own.current?.contains(event.relatedTarget))) {
          setDropOn(undefined);
        }
      }}
      onDrop={onDrop}
    >
      <fieldset className="media-choice">
        <legend className="heard">类型</legend>
        {MEDIA_CHOICES.map((choice) => (
          <label key={choice} title={MEDIA_NAMES[choice]}>
            <input
              type="radio"
              name={mediaName}
              value={choice}
              checked={media === choice}
              onChange={() => browsing.chooseMedia(choice)}
            />
            <MediaIcon media={choice} />
            <span>{MEDIA_NAMES[choice]}</span>
          </label>
        ))}
      </fieldset>
      {dropOn === null && <p className="sidebar-said">放在这里，移出分类</p>}
      {dropRefusal !== undefined && (
        <p className="sidebar-said status failed" role="alert">
          {dropRefusal}
        </p>
      )}
      <div className="sidebar-scroll">
        <ListFailures folders={folders} subscriptions={subscriptions} />
        <SidebarTree
          selection={selection}
          tally={treeTally}
          media={treeMedia}
          order={order}
          folders={folderList}
          subscriptions={subscriptionList}
          occupied={browsing.occupied}
          heldIn={browsing.heldIn}
          folded={browsing.folded}
          focusKey={focusKey}
          dropOn={dropOn ?? undefined}
          placer={browsing.placer}
          onSelect={select}
          onFolded={browsing.fold}
          onRename={browsing.rename}
          onDissolve={browsing.dissolve}
        />
      </div>
      <div className="sidebar-foot">
        {naming ? (
          <NewFolderField
            onMake={browsing.placer.make}
            onDone={(made) => {
              setNaming(false);
              if (made === undefined) {
                requestAnimationFrame(() => makeButton.current?.focus());
              } else {
                setFocusKey({ key: keyOf({ kind: "folder", folderId: made.id }) });
              }
            }}
          />
        ) : (
          <div className="foot-row">
            <button ref={makeButton} onClick={() => setNaming(true)}>
              ＋ 分类
            </button>
            <button onClick={subscribe}>＋ 订阅</button>
          </div>
        )}
        <div className="foot-row">
          <select
            aria-label="分类和订阅的排列"
            value={order}
            onChange={(event) => browsing.chooseOrder(event.target.value as Order)}
          >
            {(Object.keys(ORDER_NAMES) as Order[]).map((each) => (
              <option key={each} value={each}>
                {ORDER_NAMES[each]}
              </option>
            ))}
          </select>
          <button
            className="sidebar-fold"
            aria-label={folding.label}
            title={`${folding.label}（${SHORTCUT_NAME}）`}
            onClick={folding.press}
          >
            <span aria-hidden="true">{folding.glyph}</span>
          </button>
        </div>
      </div>
    </nav>
  );
}

// The sidebar folded: the media's icons, the one chosen marked, and the count of new items; a
// press opens the whole sidebar over the list column, and a drag that comes over it opens it to
// be dropped on. Under them, the press that unfolds it.
function Rail({
  media,
  fresh,
  layout,
}: {
  media: MediaChoice;
  fresh: number;
  layout: SidebarLayout;
}) {
  const openedByDrag = useRef(false);
  const opener = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const ended = () => {
      if (openedByDrag.current) {
        openedByDrag.current = false;
        layout.close();
      }
    };
    addEventListener("dragend", ended);
    addEventListener("drop", ended);
    return () => {
      removeEventListener("dragend", ended);
      removeEventListener("drop", ended);
    };
  }, [layout.close]);

  const told = `打开侧栏：${MEDIA_NAMES[media]}${fresh > 0 ? `，${fresh} 篇新来源` : ""}`;
  return (
    <div className="rail">
      <button
        ref={opener}
        className="rail-open"
        aria-label={told}
        aria-expanded={layout.overlaid}
        title={told}
        onClick={(event) => (layout.overlaid ? layout.close() : layout.open(event.currentTarget))}
        onDragEnter={(event) => {
          if (draggedOver(event) !== undefined && !layout.overlaid && opener.current !== null) {
            openedByDrag.current = true;
            layout.open(opener.current);
          }
        }}
      >
        <svg className="rail-mark" viewBox="0 0 16 16" aria-hidden="true">
          <path d="M2.5 4h11M2.5 8h11M2.5 12h11" />
        </svg>
        {MEDIA_CHOICES.map((choice) => (
          <span key={choice} className="rail-media" data-chosen={choice === media}>
            <MediaIcon media={choice} />
          </span>
        ))}
        {fresh > 0 && (
          <span className="new-count" aria-hidden="true">
            {fresh}
          </span>
        )}
      </button>
      <button
        className="rail-unfold"
        aria-label="展开侧栏"
        title={`展开侧栏（${SHORTCUT_NAME}）`}
        onClick={layout.unfold}
      >
        <span aria-hidden="true">»</span>
      </button>
    </div>
  );
}

// The sidebar where the layout puts it: a column; or a rail, and over the list column when it
// is opened; or, in one column, nothing until a press opens it over the page.
export function Sidebar({ browsing, layout }: { browsing: Browsing; layout: SidebarLayout }) {
  if (layout.pinned) {
    return <SidebarBody overlaid={false} browsing={browsing} layout={layout} />;
  }
  return (
    <div className="sidebar-place">
      {layout.width !== "narrow" && (
        <Rail media={browsing.media} fresh={browsing.tally.fresh} layout={layout} />
      )}
      {layout.overlaid && (
        <>
          {layout.width === "narrow" && <div className="scrim" aria-hidden="true" />}
          <SidebarBody overlaid browsing={browsing} layout={layout} />
        </>
      )}
    </div>
  );
}
