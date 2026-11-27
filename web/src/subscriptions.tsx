import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import {
  isUnanswered,
  listSubscriptions,
  subscribe,
  type ItemEntry,
  type Subscription,
} from "./client";
import { isBeingRead } from "./entry";
import { useWatched, type Loaded, type Watched } from "./loading";
import { isPollDue, untilNextPoll } from "./nextpoll";
import { usePress } from "./pressing";
import "./heard.css";
import "./subscriptions.css";

// How many of the newest postings a new subscription may have read at once, and how many unless
// the reader chooses otherwise.
const ADMITTED_CHOICES = [0, 1, 3, 5, 10];
const ADMITTED_AT_FIRST = 3;

// The subscription the list column shows: which one; the item it was opened at, whose posting
// it looks for, if any; and whether it was opened as the answer to subscribing to a feed the
// list held already.
export type SubscriptionPlace = {
  subscriptionId: string;
  sought: string | undefined;
  again: boolean;
};

// A word on how a subscription stands, where a reader must know it.
export function stateWord(subscription: Subscription): string | undefined {
  if (subscription.poll.kind === "underway") {
    return "检查中…";
  }
  if (subscription.follows === "paused") {
    return "已暂停";
  }
  return subscription.poll.kind === "failed" ? "检查失败" : undefined;
}

export function withSubscription(listed: Subscription[], read: Subscription): Subscription[] {
  return listed.map((held) => (held.id === read.id ? read : held));
}

// The owner's subscriptions, read again closely while a poll is under way or its time has come,
// and while an item that a subscription brought is being read, since its count of new items
// changes when it is read; and read again at once when an item is told as seen. A poll that
// settles may have brought items, so the list of items is read again then.
export function useSubscriptions(
  entries: Loaded<ItemEntry[]>,
  seen: ReadonlySet<string>,
  onPolled: () => void,
): Watched<Subscription[]> {
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

  const told = useRef({ readingSubscribed, seen: seen.size });
  useEffect(() => {
    if (told.current.readingSubscribed !== readingSubscribed || told.current.seen !== seen.size) {
      told.current = { readingSubscribed, seen: seen.size };
      subscriptions.readNow();
    }
  }, [readingSubscribed, seen.size]);

  // The latest poll of each subscription, by when it was; one that settles anew changes it.
  const polls =
    subscriptions.loaded.state === "ready"
      ? subscriptions.loaded.value
          .map(({ id, poll }) => (poll.kind === "underway" ? "" : `${id}@${poll.at}`))
          .join(" ")
      : undefined;
  const polled = useRef(polls);
  useEffect(() => {
    if (polled.current !== undefined && polls !== undefined && polled.current !== polls) {
      onPolled();
    }
    polled.current = polls;
  }, [polls]);

  return subscriptions;
}

// Subscribing to an address: the address, how many of the newest postings are read at once,
// and what came of it. The answer is handed on; a refusal is said under the field.
export function SubscribeForm({
  onSubscribed,
}: {
  onSubscribed: (subscription: Subscription) => void;
}) {
  const [address, setAddress] = useState("");
  const [admitted, setAdmitted] = useState(ADMITTED_AT_FIRST);
  const { press, ask } = usePress();
  const fieldId = useId();
  const hintId = useId();
  const asking = press.state === "asking";

  function submit(event: FormEvent) {
    event.preventDefault();
    ask(
      () => subscribe(address, admitted),
      (subscription) => {
        setAddress("");
        onSubscribed(subscription);
      },
    );
  }

  return (
    <form className="subscribe" onSubmit={submit}>
      <label className="heard" htmlFor={fieldId}>
        要订阅的地址
      </label>
      <div className="subscribe-row">
        <input
          id={fieldId}
          type="text"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          required
          autoFocus
          readOnly={asking}
          placeholder="粘贴要订阅的地址"
          aria-describedby={hintId}
          value={address}
          onChange={(event) => setAddress(event.target.value)}
        />
        <button aria-disabled={asking}>{asking ? "订阅中…" : "订阅"}</button>
      </div>
      <p id={hintId} className="hint">
        订阅源（RSS、Atom、JSON Feed）、播客、YouTube 频道、网站的页面，或 rsshub:// 路由
      </p>
      <label className="admitted">
        订阅后先读取最新的
        <select
          value={admitted}
          disabled={asking}
          onChange={(event) => setAdmitted(Number(event.target.value))}
        >
          {ADMITTED_CHOICES.map((count) => (
            <option key={count} value={count}>
              {count}
            </option>
          ))}
        </select>
        条
      </label>
      {/* Stays in the page while it says nothing, so that what it comes to say is read out. */}
      <p className="status" role="status">
        {asking && "正在查找并读取订阅源，可能要半分钟…"}
      </p>
      {press.state === "refused" && (
        <p className="status failed" role="alert">
          {press.reason}
        </p>
      )}
    </form>
  );
}

// What subscribing is for, said where there are no subscriptions yet.
export function SubscribingHint() {
  return (
    <p className="status">
      订阅一个博客、新闻网站、播客或 YouTube 频道后，infonium
      会定时检查它，把新的文章和节目读进文库。没有订阅源的网站，可以通过 RSSHub 的路由订阅。
    </p>
  );
}
