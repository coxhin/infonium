import { useLayoutEffect, useMemo, useRef, useState } from "react";
import { requestReason, type ItemEntry, type Subscription } from "./client";
import { Interruption } from "./interruption";
import { ItemList, type ImportInto } from "./itemlist";
import type { Loaded } from "./loading";
import type { Navigation } from "./navigation";
import {
  MEDIA_NAMES,
  keyOf,
  listed,
  selectionName,
  type MediaChoice,
  type Selection,
} from "./selection";
import type { SidebarLayout } from "./sidebarlayout";
import { SubscribeForm, SubscribingHint, withSubscription } from "./subscriptions";
import { SubscriptionPane } from "./subscriptionpane";
import "./listcolumn.css";

// What the list says where a selection lists nothing, of every media or of one. A media named in
// Latin letters stands a space apart from the Chinese around it, as the interface writes PDF
// elsewhere, and from no punctuation.
function emptyText(selection: Selection, media: MediaChoice): string {
  const name = media === "any" ? "文章" : MEDIA_NAMES[media];
  const of = /^[A-Za-z]/.test(name) ? ` ${name} ` : name;
  switch (selection.kind) {
    case "all":
      return media === "any"
        ? "粘贴一个网址或上传一个文件，它会出现在这里。"
        : `文库里没有${of.trimEnd()}。`;
    case "new":
      return `没有新来源：订阅带来、还没打开看过的${of}会列在这里。`;
    case "unfiled":
      return `不在任何分类里、手动添加的${of}会列在这里。`;
    case "folder":
      return `这个分类里还没有${of.trimEnd()}。可以把订阅或文章拖到侧栏的这个分类上，或用它们的菜单“移到分类…”。`;
    case "subscription":
      return "";
  }
}

// The list column: under a head that names the selection and the media, the items the sidebar
// selected, with adding by an address or a file where the selection takes it; or the
// subscription selected, as its panel shows it, whatever the media; or the form that subscribes.
// In one column, a press at its head opens the sidebar over the page.
export function ListColumn({
  entries,
  interrupted,
  refusal,
  selected,
  seen,
  navigation,
  layout,
  onImported,
  onChanged,
}: {
  entries: Loaded<ItemEntry[]>;
  interrupted: boolean;
  // Why reading the list of items again failed, where the service said so.
  refusal: string | undefined;
  selected: string | undefined;
  // The items told as seen from this page.
  seen: ReadonlySet<string>;
  navigation: Navigation;
  layout: SidebarLayout;
  onImported: (entry: ItemEntry) => void;
  // What the library holds has changed by a press in the column.
  onChanged: () => void;
}) {
  const { selection, media, present, subscriptions, folders, placer } = navigation;
  const [unplaced, setUnplaced] = useState<string>();
  const folderMap = useMemo(
    () =>
      new Map(
        (folders.loaded.state === "ready" ? folders.loaded.value : []).map((folder) => [
          folder.id,
          folder,
        ]),
      ),
    [folders.loaded],
  );
  const subscriptionList: Subscription[] =
    subscriptions.loaded.state === "ready" ? subscriptions.loaded.value : [];
  const subscriptionTitles = useMemo(
    () => new Map(subscriptionList.map((subscription) => [subscription.id, subscription.title])),
    [subscriptionList],
  );
  const subscriptionFolders = useMemo(
    () =>
      new Map(
        subscriptionList.flatMap((subscription) => {
          const folder =
            subscription.folder === null ? undefined : folderMap.get(subscription.folder);
          return folder === undefined ? [] : [[subscription.id, folder.name] as const];
        }),
      ),
    [subscriptionList, folderMap],
  );
  const listedEntries = useMemo(
    () =>
      entries.state === "ready" ? listed(entries.value, selection, media, present, seen) : [],
    [entries, selection, media, present, seen],
  );

  const subscribing = navigation.subscribing;
  const name = subscribing ? "订阅" : selectionName(selection, folderMap);
  const listKey = `${keyOf(selection)}|${media}`;
  const view = useRef<HTMLDivElement>(null);
  // Another selection or media is listed from its top, not where the one before was scrolled.
  useLayoutEffect(() => {
    view.current?.scrollTo({ top: 0 });
  }, [listKey]);
  const importInto: ImportInto =
    selection.kind === "folder"
      ? { folderId: selection.folderId, name: folderMap.get(selection.folderId)?.name ?? "" }
      : undefined;
  // The lists the selection is drawn from are read first; until then, they are being opened.
  const waiting =
    selection.kind !== "all" &&
    (subscriptions.loaded.state === "loading" || folders.loaded.state === "loading");

  function onSubscribed(subscription: Subscription) {
    setUnplaced(undefined);
    navigation.subscribed(subscription).then(
      () => onChanged(),
      (error: unknown) =>
        setUnplaced(`已订阅，但没有放进分类“${importInto?.name ?? ""}”：${requestReason(error)}`),
    );
  }

  return (
    <nav className="library" aria-label="列表">
      <div className="list-brand">
        {layout.width === "narrow" && (
          <button
            className="sidebar-opener"
            aria-label="打开侧栏"
            aria-expanded={layout.overlaid}
            title="打开侧栏"
            onClick={(event) => layout.open(event.currentTarget)}
          >
            <svg viewBox="0 0 16 16" aria-hidden="true">
              <path d="M2.5 4h11M2.5 8h11M2.5 12h11" />
            </svg>
          </button>
        )}
        <h1>infonium</h1>
      </div>
      <div className="list-head">
        <h2 dir="auto" title={name}>
          {name}
        </h2>
        {!subscribing && selection.kind !== "subscription" && media !== "any" && (
          <span className="list-media">{MEDIA_NAMES[media]}</span>
        )}
        {subscribing && (
          <button className="quiet" onClick={navigation.stopSubscribing}>
            取消
          </button>
        )}
      </div>
      {subscribing ? (
        <div className="list-view">
          <SubscribeForm onSubscribed={onSubscribed} />
          {unplaced !== undefined && (
            <p className="status failed" role="alert">
              {unplaced}
            </p>
          )}
          {subscriptions.interrupted && <Interruption refusal={subscriptions.refusal} />}
          {subscriptions.loaded.state === "ready" && subscriptionList.length === 0 && (
            <SubscribingHint />
          )}
        </div>
      ) : selection.kind === "subscription" && navigation.pane !== undefined ? (
        <SubscriptionPane
          key={navigation.pane.subscriptionId}
          subscriptionId={navigation.pane.subscriptionId}
          sought={navigation.pane.sought}
          again={navigation.pane.again}
          selected={selected}
          seen={seen}
          shown
          onView={(subscription) =>
            subscriptions.replace((before) => withSubscription(before, subscription))
          }
          onGone={navigation.gone}
          onChanged={onChanged}
        />
      ) : (
        <div className="list-view" ref={view}>
          {waiting ? (
            <p className="status">正在打开…</p>
          ) : (
            <ItemList
              entries={entries}
              listed={listedEntries}
              listKey={listKey}
              importable={selection.kind !== "new"}
              importInto={importInto}
              emptyText={emptyText(selection, media)}
              interrupted={interrupted}
              refusal={refusal}
              selected={selected}
              seen={seen}
              present={present}
              subscriptionTitles={subscriptionTitles}
              subscriptionFolders={subscriptionFolders}
              placer={placer}
              onImported={onImported}
            />
          )}
        </div>
      )}
    </nav>
  );
}
