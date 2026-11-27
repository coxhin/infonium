import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { listSubscriptions, type ItemEntry, type Subscription } from "./client";
import { isBeingRead } from "./entry";
import { useFollowed } from "./following";
import { ItemList } from "./itemlist";
import type { Loaded } from "./loading";
import { NewCount } from "./newmarks";
import { SubscriptionsPanel, type Opened } from "./subscriptions";
import "./listcolumn.css";

type ColumnTab = "items" | "subscriptions";
const TAB_NAMES: Record<ColumnTab, string> = { items: "文库", subscriptions: "订阅" };

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

function withSubscription(listed: Subscription[], read: Subscription): Subscription[] {
  return listed.map((held) => (held.id === read.id ? read : held));
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
        <ItemList
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
              // A feed subscribed to already is answered as its subscription, which keeps its
              // place in the list.
              subscriptions.replace((before) =>
                before.some((held) => held.id === subscription.id)
                  ? withSubscription(before, subscription)
                  : [subscription, ...before],
              );
              openSubscription(subscription.id);
              onChanged();
            }}
            onView={(subscription) =>
              subscriptions.replace((before) => withSubscription(before, subscription))
            }
            onGone={gone}
            onChanged={onChanged}
          />
        )}
      </div>
    </nav>
  );
}
