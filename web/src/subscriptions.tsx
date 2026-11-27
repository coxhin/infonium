import { useId, useState, type FormEvent } from "react";
import { subscribe, type Subscription } from "./client";
import { usePress } from "./pressing";
import "./heard.css";
import "./subscriptions.css";

// How many of the newest postings a new subscription may have read at once, and how many unless
// the reader chooses otherwise.
const ADMITTED_CHOICES = [0, 1, 3, 5, 10];
const ADMITTED_AT_FIRST = 3;

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
