import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
} from "react";
import {
  importLink,
  importUpload,
  listSubscriptions,
  type ItemEntry,
  type Subscription,
} from "./client";
import { useFollowed } from "./following";
import { itemLink } from "./links";
import { reasonOf, type Loaded } from "./loading";
import { isBeingRead, isNew, itemTitle, originLabel } from "./entry";
import { NewCount, NewDot, NewHeard } from "./newmarks";
import { SubscriptionsPanel, type Opened } from "./subscriptions";
import "./listcolumn.css";

type Importing = { state: "idle" } | { state: "running" } | { state: "failed"; reason: string };

const ATTEMPT_NOTES: Record<ItemEntry["reading"]["attempt"]["kind"], string> = {
  underway: " · 读取中…",
  settled: "",
  failed: " · 读取失败",
};
// Kinds of file the upload dialog offers; what a file really is, the server tells from its bytes.
const UPLOADABLE = "application/pdf,audio/*,video/*,text/html,.html,.htm";

type ColumnTab = "items" | "subscriptions";
const TAB_NAMES: Record<ColumnTab, string> = { items: "文库", subscriptions: "订阅" };

// Which items the list of items shows: all, those a reader added by a link or an upload, or
// those that subscriptions brought.
type Narrowing = "all" | "added" | "subscribed";
const NARROWING_NAMES: Record<Narrowing, string> = {
  all: "全部",
  added: "手动添加",
  subscribed: "订阅",
};
const NARROWED_AWAY: Record<Narrowing, string> = {
  all: "",
  added: "粘贴网址或上传文件添加的文章会列在这里。",
  subscribed: "订阅读进来的文章会列在这里。",
};

function narrowed(entries: ItemEntry[], narrowing: Narrowing): ItemEntry[] {
  switch (narrowing) {
    case "all":
      return entries;
    case "added":
      return entries.filter((entry) => entry.origin.kind !== "subscribed");
    case "subscribed":
      return entries.filter((entry) => entry.origin.kind === "subscribed");
  }
}

// Where the list column stands: the tab on show, and under 订阅 the subscription open, if any.
type Place = { tab: ColumnTab; opened: Opened | undefined };
const AT_ITEMS: Place = { tab: "items", opened: undefined };

// Where the column stands when the app opens with an item: at the subscription that brought it,
// with it marked, or at the list of items. Nothing while the lists that tell it are read, and a
// tab pressed meanwhile decides it.
function placeAtOpening(
  selected: string,
  entries: Loaded<ItemEntry[]>,
  subscriptions: Loaded<Subscription[]>,
): Place | undefined {
  if (entries.state === "loading" || subscriptions.state === "loading") {
    return undefined;
  }
  const origin =
    entries.state === "ready"
      ? entries.value.find((entry) => entry.id === selected)?.origin
      : undefined;
  const subscribed =
    origin?.kind === "subscribed" &&
    subscriptions.state === "ready" &&
    subscriptions.value.some((subscription) => subscription.id === origin.subscription);
  return subscribed
    ? { tab: "subscriptions", opened: { subscriptionId: origin.subscription, sought: selected } }
    : AT_ITEMS;
}

// What scrolls the column to a place, kept for each view of it: a tab, or an open subscription.
function viewName(place: Place): string {
  return place.opened === undefined ? place.tab : `subscription:${place.opened.subscriptionId}`;
}

function tabId(tab: ColumnTab): string {
  return `list-tab-${tab}`;
}

function panelId(tab: ColumnTab): string {
  return `list-panel-${tab}`;
}

// The list of items as it has always been: adding by an address or a file, and the items, each
// with where it came from, a subscription by its title; narrowed by where they came from.
function ItemsPanel({
  entries,
  interrupted,
  selected,
  seen,
  subscriptionTitles,
  onImported,
}: {
  entries: Loaded<ItemEntry[]>;
  interrupted: boolean;
  selected: string | undefined;
  seen: ReadonlySet<string>;
  subscriptionTitles: ReadonlyMap<string, string>;
  onImported: (entry: ItemEntry) => void;
}) {
  const [url, setUrl] = useState("");
  const [importing, setImporting] = useState<Importing>({ state: "idle" });
  const [narrowing, setNarrowing] = useState<Narrowing>("all");
  const narrowingName = useId();

  async function imported(importingItem: Promise<ItemEntry>) {
    setImporting({ state: "running" });
    try {
      const entry = await importingItem;
      setImporting({ state: "idle" });
      onImported(entry);
    } catch (error) {
      setImporting({ state: "failed", reason: reasonOf(error) });
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

  const shown = entries.state === "ready" ? narrowed(entries.value, narrowing) : [];
  return (
    <>
      <form onSubmit={importUrl}>
        <input
          type="url"
          required
          placeholder="粘贴网址：文章、PDF、视频、播客"
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
      {importing.state === "failed" && <p className="status failed">{importing.reason}</p>}
      {entries.state === "failed" && <p className="status failed">{entries.reason}</p>}
      {interrupted && <p className="status failed">连接不上服务，正在重试…</p>}
      {entries.state === "ready" && entries.value.length > 0 && (
        <fieldset className="narrowing">
          <legend className="heard">显示哪些文章</legend>
          {(Object.keys(NARROWING_NAMES) as Narrowing[]).map((name) => (
            <label key={name}>
              <input
                type="radio"
                name={narrowingName}
                value={name}
                checked={narrowing === name}
                onChange={() => setNarrowing(name)}
              />
              {NARROWING_NAMES[name]}
            </label>
          ))}
        </fieldset>
      )}
      {entries.state === "ready" && entries.value.length > 0 && shown.length === 0 && (
        <p className="status">{NARROWED_AWAY[narrowing]}</p>
      )}
      {shown.length > 0 && (
        <ul>
          {shown.map((entry) => {
            const fresh = isNew(entry, seen);
            const source = sourceName(entry);
            return (
              <li key={entry.id}>
                <a
                  href={itemLink(entry.id)}
                  className={fresh ? "new" : undefined}
                  aria-current={entry.id === selected}
                >
                  {fresh && <NewDot />}
                  {itemTitle(entry)}
                  {fresh && <NewHeard />}
                  <small>
                    <span className="source" title={source}>
                      {source}
                    </span>
                    <span className="attempt">{ATTEMPT_NOTES[entry.reading.attempt.kind]}</span>
                  </small>
                </a>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

// The list column: the library's items under 文库, and the subscriptions under 订阅, whose tab
// counts their new items. The subscriptions are followed whichever tab is on show: their count
// stands on the tab, and their titles in the list of items. Each view of the column keeps the
// place it was scrolled to while another is on show.
export function Library({
  entries,
  interrupted,
  selected,
  seen,
  onImported,
  onChanged,
}: {
  entries: Loaded<ItemEntry[]>;
  interrupted: boolean;
  selected: string | undefined;
  // The items told as seen from this page.
  seen: ReadonlySet<string>;
  onImported: (entry: ItemEntry) => void;
  // What the library holds has changed by a press in the column.
  onChanged: () => void;
}) {
  // An item that a subscription brought being read changes the count of its subscription's new
  // items when it is read, so the subscriptions are followed closely meanwhile.
  const readingSubscribed =
    entries.state === "ready"
      ? entries.value
          .filter((entry) => entry.origin.kind === "subscribed" && isBeingRead(entry))
          .map((entry) => entry.id)
          .join(" ")
      : "";
  const subscriptions = useFollowed(
    listSubscriptions,
    "subscriptions",
    (listed) =>
      readingSubscribed !== "" ||
      listed.some((subscription) => subscription.poll.kind === "underway"),
    true,
  );
  const [opening] = useState(selected);
  const [place, setPlace] = useState<Place | undefined>(
    opening === undefined ? AT_ITEMS : undefined,
  );
  const [left, setLeft] = useState<string>();
  const column = useRef<HTMLElement>(null);
  const scrolled = useRef(new Map<string, number>());

  useEffect(() => {
    if (place === undefined && opening !== undefined) {
      setPlace(placeAtOpening(opening, entries, subscriptions.loaded));
    }
  }, [place, entries, subscriptions.loaded]);

  // An item read or seen changes the counts of new items: the subscriptions are read again.
  const told = useRef({ readingSubscribed, seen: seen.size });
  useEffect(() => {
    if (told.current.readingSubscribed !== readingSubscribed || told.current.seen !== seen.size) {
      told.current = { readingSubscribed, seen: seen.size };
      subscriptions.readNow();
    }
  }, [readingSubscribed, seen.size]);

  const shownView = place === undefined ? undefined : viewName(place);
  useLayoutEffect(() => {
    if (shownView !== undefined && column.current !== null) {
      column.current.scrollTop = scrolled.current.get(shownView) ?? 0;
    }
  }, [shownView]);

  function go(next: Place) {
    if (place !== undefined && column.current !== null) {
      scrolled.current.set(viewName(place), column.current.scrollTop);
    }
    setPlace(next);
  }

  function openSubscription(subscriptionId: string) {
    setLeft(undefined);
    scrolled.current.delete(`subscription:${subscriptionId}`);
    go({ tab: "subscriptions", opened: { subscriptionId, sought: undefined } });
  }

  function backToList() {
    setLeft(place?.opened?.subscriptionId);
    go({ tab: "subscriptions", opened: undefined });
  }

  function gone(subscriptionId: string) {
    subscriptions.replace((listed) =>
      listed.filter((subscription) => subscription.id !== subscriptionId),
    );
    if (place?.opened?.subscriptionId === subscriptionId) {
      setLeft(undefined);
      go({ tab: "subscriptions", opened: undefined });
    }
  }

  const listed = subscriptions.loaded.state === "ready" ? subscriptions.loaded.value : [];
  const subscriptionTitles = new Map(
    listed.map((subscription) => [subscription.id, subscription.title]),
  );
  const unseen = listed.reduce((sum, subscription) => sum + subscription.unseen, 0);

  return (
    <nav className="library" ref={column}>
      <h1>infonium</h1>
      <div className="tabs" role="tablist">
        {(Object.keys(TAB_NAMES) as ColumnTab[]).map((name) => (
          <button
            key={name}
            id={tabId(name)}
            role="tab"
            aria-selected={place?.tab === name}
            aria-controls={panelId(name)}
            onClick={() => go({ tab: name, opened: place?.opened })}
          >
            {TAB_NAMES[name]}
            {name === "subscriptions" && unseen > 0 && <NewCount count={unseen} />}
          </button>
        ))}
      </div>
      {place === undefined && (interrupted || subscriptions.interrupted) && (
        <p className="status failed">连接不上服务，正在重试…</p>
      )}
      <div
        id={panelId("items")}
        role="tabpanel"
        aria-labelledby={tabId("items")}
        hidden={place?.tab !== "items"}
      >
        <ItemsPanel
          entries={entries}
          interrupted={interrupted}
          selected={selected}
          seen={seen}
          subscriptionTitles={subscriptionTitles}
          onImported={onImported}
        />
      </div>
      <div
        id={panelId("subscriptions")}
        role="tabpanel"
        aria-labelledby={tabId("subscriptions")}
        hidden={place?.tab !== "subscriptions"}
      >
        {place !== undefined && (
          <SubscriptionsPanel
            subscriptions={subscriptions}
            opened={place.opened}
            left={left}
            selected={selected}
            seen={seen}
            shown={place.tab === "subscriptions"}
            onOpen={openSubscription}
            onBack={backToList}
            onSubscribed={(subscription) => {
              subscriptions.replace((before) => [
                subscription,
                ...before.filter((held) => held.id !== subscription.id),
              ]);
              openSubscription(subscription.id);
              onChanged();
            }}
            onView={(subscription) =>
              subscriptions.replace((before) =>
                before.map((held) => (held.id === subscription.id ? subscription : held)),
              )
            }
            onGone={gone}
            onChanged={onChanged}
          />
        )}
      </div>
    </nav>
  );
}
