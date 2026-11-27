import { useId, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { subscribe, type Subscription } from "./client";
import type { Followed } from "./following";
import { NewCount } from "./newmarks";
import { usePress } from "./pressing";
import { OpenedSubscription } from "./subscriptionpane";
import "./subscriptions.css";

// How many of the newest postings a new subscription may have read at once, and how many unless
// the reader chooses otherwise.
const ADMITTED_CHOICES = [0, 1, 3, 5, 10];
const ADMITTED_AT_FIRST = 3;

// A subscription open in the list column, and the item it was opened at, whose posting it
// looks for, if any.
export type Opened = { subscriptionId: string; sought: string | undefined };

// A word on how a subscription stands, where a reader must know it.
function stateWord(subscription: Subscription): string | undefined {
  if (subscription.poll.kind === "underway") {
    return "检查中…";
  }
  if (subscription.follows === "paused") {
    return "已暂停";
  }
  return subscription.poll.kind === "failed" ? "检查失败" : undefined;
}

function SubscribeForm({ onSubscribed }: { onSubscribed: (subscription: Subscription) => void }) {
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
          readOnly={asking}
          placeholder="粘贴要订阅的地址"
          aria-describedby={hintId}
          value={address}
          onChange={(event) => setAddress(event.target.value)}
        />
        <button disabled={asking}>{asking ? "订阅中…" : "订阅"}</button>
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
      {asking && (
        <p className="status" role="status">
          正在查找并读取订阅源，可能要半分钟…
        </p>
      )}
      {press.state === "refused" && (
        <p className="status failed" role="alert">
          {press.reason}
        </p>
      )}
    </form>
  );
}

function SubscriptionList({
  subscriptions,
  left,
  onOpen,
}: {
  subscriptions: Subscription[];
  left: string | undefined;
  onOpen: (subscriptionId: string) => void;
}) {
  const list = useRef<HTMLUListElement>(null);
  // Back from a subscription, the keyboard is given to its row.
  useLayoutEffect(() => {
    if (left !== undefined && document.activeElement === document.body) {
      list.current
        ?.querySelector<HTMLButtonElement>(`[data-subscription="${CSS.escape(left)}"]`)
        ?.focus({ preventScroll: true });
    }
  }, []);

  if (subscriptions.length === 0) {
    return (
      <p className="status">
        订阅一个博客、新闻网站、播客或 YouTube 频道后，infonium
        会定时检查它，把新的文章和节目读进文库。没有订阅源的网站，可以通过 RSSHub 的路由订阅。
      </p>
    );
  }
  return (
    <ul className="subscription-list" aria-label="订阅" ref={list}>
      {subscriptions.map((subscription) => {
        const word = stateWord(subscription);
        return (
          <li key={subscription.id}>
            <button
              data-subscription={subscription.id}
              title={subscription.title}
              onClick={() => onOpen(subscription.id)}
            >
              <span className="subscription-title">{subscription.title}</span>
              {word !== undefined && (
                <small className={subscription.poll.kind === "failed" ? "failed" : undefined}>
                  {word}
                </small>
              )}
              {subscription.unseen > 0 && <NewCount count={subscription.unseen} />}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

// The tab of subscriptions: subscribing to an address, the subscriptions, the one added last
// first, and one of them open in place of the list.
export function SubscriptionsPanel({
  subscriptions,
  opened,
  left,
  selected,
  seen,
  shown,
  onOpen,
  onBack,
  onSubscribed,
  onView,
  onGone,
  onChanged,
}: {
  subscriptions: Followed<Subscription[]>;
  opened: Opened | undefined;
  // The subscription the reader went back to the list from.
  left: string | undefined;
  selected: string | undefined;
  seen: ReadonlySet<string>;
  shown: boolean;
  onOpen: (subscriptionId: string) => void;
  onBack: () => void;
  onSubscribed: (subscription: Subscription) => void;
  onView: (subscription: Subscription) => void;
  onGone: (subscriptionId: string) => void;
  onChanged: () => void;
}) {
  if (opened !== undefined) {
    return (
      <OpenedSubscription
        key={opened.subscriptionId}
        subscriptionId={opened.subscriptionId}
        sought={opened.sought}
        selected={selected}
        seen={seen}
        shown={shown}
        onBack={onBack}
        onView={onView}
        onGone={onGone}
        onChanged={onChanged}
      />
    );
  }
  const { loaded } = subscriptions;
  return (
    <>
      <SubscribeForm onSubscribed={onSubscribed} />
      {loaded.state === "failed" && (
        <p className="status failed">
          订阅没有读到：{loaded.reason}
          <button onClick={subscriptions.readNow}>重试</button>
        </p>
      )}
      {subscriptions.interrupted && <p className="status failed">连接不上服务，正在重试…</p>}
      {loaded.state === "ready" && (
        <SubscriptionList subscriptions={loaded.value} left={left} onOpen={onOpen} />
      )}
    </>
  );
}
