import { useEffect, useRef, useState } from "react";
import { isUnanswered, listSubscriptions, type ItemEntry, type Subscription } from "./client";
import { isBeingRead } from "./entry";
import { Interruption } from "./interruption";
import { ItemList } from "./itemlist";
import { useWatched, type Loaded } from "./loading";
import { NewCount } from "./newmarks";
import { isPollDue, untilNextPoll } from "./nextpoll";
import { SubscriptionsPanel, type SubscriptionPlace } from "./subscriptions";
import "./listcolumn.css";

type ColumnTab = "items" | "subscriptions";
const TAB_NAMES: Record<ColumnTab, string> = { items: "文库", subscriptions: "订阅" };

// Where the list column stands: the tab on show, and under 订阅 the subscription open, if any.
type Place = { tab: ColumnTab; opened: SubscriptionPlace | undefined };
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
    ? {
        tab: "subscriptions",
        opened: { subscriptionId: origin.subscription, sought: selected, again: false },
      }
    : AT_ITEMS;
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
// counts their new items. The tabs stay in place, and each view under them scrolls by itself,
// so that it keeps the place it was scrolled to while another is on show. The subscriptions are
// read again whichever tab is on show: their count stands on the tab, and their titles in the
// list of items.
export function ListColumn({
  entries,
  interrupted,
  refusal,
  selected,
  seen,
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
  onImported: (entry: ItemEntry) => void;
  // What the library holds has changed by a press in the column.
  onChanged: () => void;
}) {
  // An item that a subscription brought being read changes the count of its subscription's new
  // items when it is read, so the subscriptions are read again closely meanwhile, as they are
  // while a poll is under way or its time has come.
  const readingSubscribed =
    entries.state === "ready"
      ? entries.value
          .filter((entry) => entry.origin.kind === "subscribed" && isBeingRead(entry))
          .map((entry) => entry.id)
          .join(" ")
      : "";
  const subscriptions = useWatched(
    listSubscriptions,
    "subscriptions",
    (listed) =>
      readingSubscribed !== "" ||
      listed.some(
        (subscription) =>
          subscription.poll.kind === "underway" || isPollDue(subscription, Date.now()),
      ),
    {
      interval: (listed) => untilNextPoll(listed, Date.now()),
      shown: true,
      unanswered: isUnanswered,
    },
  );
  const [opening] = useState(selected);
  const [place, setPlace] = useState<Place | undefined>(
    opening === undefined ? AT_ITEMS : undefined,
  );
  const [left, setLeft] = useState<string>();

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

  function openSubscription(opened: SubscriptionPlace) {
    setLeft(undefined);
    setPlace({ tab: "subscriptions", opened });
  }

  function backToList() {
    setLeft(place?.opened?.subscriptionId);
    setPlace({ tab: "subscriptions", opened: undefined });
  }

  // A subscription removed here or elsewhere leaves the list, and the column, where it is open.
  function gone(subscriptionId: string) {
    subscriptions.answer((listed) =>
      listed.filter((subscription) => subscription.id !== subscriptionId),
    );
    if (place?.opened?.subscriptionId === subscriptionId) {
      setLeft(undefined);
      setPlace({ tab: "subscriptions", opened: undefined });
    }
  }

  const listed = subscriptions.loaded.state === "ready" ? subscriptions.loaded.value : [];
  const subscriptionTitles = new Map(
    listed.map((subscription) => [subscription.id, subscription.title]),
  );
  const unseen = listed.reduce((sum, subscription) => sum + subscription.unseen, 0);

  return (
    <nav className="library">
      <h1>infonium</h1>
      <div className="tabs" role="tablist">
        {(Object.keys(TAB_NAMES) as ColumnTab[]).map((name) => (
          <button
            key={name}
            id={tabId(name)}
            role="tab"
            aria-selected={place?.tab === name}
            aria-controls={panelId(name)}
            onClick={() => setPlace({ tab: name, opened: place?.opened })}
          >
            {TAB_NAMES[name]}
            {name === "subscriptions" && unseen > 0 && <NewCount count={unseen} />}
          </button>
        ))}
      </div>
      {place === undefined && (interrupted || subscriptions.interrupted) && (
        <Interruption refusal={subscriptions.refusal ?? refusal} />
      )}
      <div
        id={panelId("items")}
        role="tabpanel"
        aria-labelledby={tabId("items")}
        className="list-view"
        hidden={place?.tab !== "items"}
      >
        <ItemList
          entries={entries}
          interrupted={interrupted}
          refusal={refusal}
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
        className="list-stack"
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
            onOpen={(subscriptionId) =>
              openSubscription({ subscriptionId, sought: undefined, again: false })
            }
            onBack={backToList}
            onSubscribed={(subscription) => {
              // A feed subscribed to already is answered as its subscription, which keeps its
              // place in the list, and is opened saying so.
              const again = listed.some((held) => held.id === subscription.id);
              subscriptions.answer((before) =>
                before.some((held) => held.id === subscription.id)
                  ? withSubscription(before, subscription)
                  : [subscription, ...before],
              );
              openSubscription({ subscriptionId: subscription.id, sought: undefined, again });
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
