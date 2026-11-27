import { useEffect, useId, useRef, useState } from "react";
import {
  chooseFollows,
  pollSubscription,
  seeSubscription,
  unsubscribe,
  type Follows,
  type Subscription,
} from "./client";
import { usePress, type Press } from "./pressing";
import { Moment, toldAhead, toldPast } from "./when";

const FOLLOWS_NAMES: Record<Follows, string> = {
  reads: "自动读取新条目",
  lists: "只列出新条目",
  paused: "暂停",
};
const WHAT_GOES = "之后不再检查它，它的条目列表也会删去；已经读取的文章仍留在文库里。";

function Refusal({ press, doing }: { press: Press; doing: string }) {
  return press.state === "refused" ? (
    <p className="status failed" role="alert">
      {doing}：{press.reason}
    </p>
  ) : null;
}

// One line on the latest poll: that one is under way; when the last one was, what it found or
// why it failed, and when the next is due; or that the subscription is paused, and so polled
// only when the reader asks.
function PollLine({ subscription, now }: { subscription: Subscription; now: Date }) {
  const { poll, follows } = subscription;
  if (poll.kind === "underway") {
    return <p className="poll-line">正在检查新条目…</p>;
  }
  const at = <Moment at={poll.at} told={toldPast(poll.at, now)} />;
  const due = <Moment at={poll.due} told={toldAhead(poll.due, now)} />;
  const last =
    poll.kind === "settled" ? (
      <>
        上次检查：{at}，{poll.found === 0 ? "没有新条目" : `发现 ${poll.found} 条新条目`}。
      </>
    ) : (
      <>
        上次检查失败（{at}）：{poll.reason}
        {poll.failures > 1 && `。已连续失败 ${poll.failures} 次`}。
      </>
    );
  if (follows === "paused") {
    return <p className="poll-line">已暂停，不会自动检查。{last}</p>;
  }
  return poll.kind === "settled" ? (
    <p className="poll-line">
      {last}下次检查：{due}。
    </p>
  ) : (
    <p className="poll-line failed">
      {last}下次重试：{due}。
    </p>
  );
}

// Removing a subscription is asked once more, saying what goes and what stays.
function Unsubscribe({
  subscription,
  onRemoved,
}: {
  subscription: Subscription;
  onRemoved: () => void;
}) {
  const [asking, setAsking] = useState(false);
  const { press, ask } = usePress();
  const offer = useRef<HTMLButtonElement>(null);
  const askId = useId();

  function cancel() {
    setAsking(false);
    // The offer is drawn again after the question is gone, so it is focused once it is there.
    requestAnimationFrame(() => offer.current?.focus());
  }

  if (!asking) {
    return (
      <button ref={offer} className="quiet" onClick={() => setAsking(true)}>
        取消订阅
      </button>
    );
  }
  return (
    <div
      className="confirm"
      role="group"
      aria-labelledby={askId}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          cancel();
        }
      }}
    >
      <p id={askId}>{`取消订阅“${subscription.title}”？` + WHAT_GOES}</p>
      <span className="confirm-choices">
        <button
          className="danger"
          disabled={press.state === "asking"}
          onClick={() => ask(() => unsubscribe(subscription.id), onRemoved)}
        >
          {press.state === "asking" ? "正在取消…" : "取消订阅"}
        </button>
        <button autoFocus onClick={cancel}>
          保留
        </button>
      </span>
      <Refusal press={press} doing="没有取消订阅" />
    </div>
  );
}

// The head of an open subscription: its title, linking to its site; how its latest poll stands;
// a poll now; how it follows its feed; taking its new items for seen; and removing it.
export function SubscriptionHead({
  subscription,
  now,
  onAnswer,
  onSeenAll,
  onRemoved,
}: {
  subscription: Subscription;
  now: Date;
  // The subscription as the service answered a press on it.
  onAnswer: (subscription: Subscription) => void;
  onSeenAll: (subscription: Subscription) => void;
  onRemoved: () => void;
}) {
  const polling = usePress();
  const following = usePress();
  const seeing = usePress();
  // The choice sent and not yet answered is shown as made.
  const [asked, setAsked] = useState<Follows>();
  const followsId = useId();
  const underway = subscription.poll.kind === "underway" || polling.press.state === "asking";

  useEffect(() => {
    if (following.press.state !== "asking") {
      setAsked(undefined);
    }
  }, [following.press.state]);

  function choose(follows: Follows) {
    setAsked(follows);
    following.ask(() => chooseFollows(subscription.id, follows), onAnswer);
  }

  return (
    <header className="subscription-head">
      <h2 title={subscription.title}>
        {subscription.site === null ? (
          subscription.title
        ) : (
          <a href={subscription.site} target="_blank" rel="noreferrer noopener">
            {subscription.title}
            <span aria-hidden="true"> ↗</span>
            <span className="heard">（在新标签页打开网站）</span>
          </a>
        )}
      </h2>
      <PollLine subscription={subscription} now={now} />
      <div className="subscription-controls">
        <button
          disabled={underway}
          onClick={() => polling.ask(() => pollSubscription(subscription.id), onAnswer)}
        >
          {underway ? "正在检查…" : "立即检查"}
        </button>
        {subscription.unseen > 0 && (
          <button
            disabled={seeing.press.state === "asking"}
            onClick={() => seeing.ask(() => seeSubscription(subscription.id), onSeenAll)}
          >
            全部标为已读
          </button>
        )}
      </div>
      <Refusal press={polling.press} doing="没有开始检查" />
      <Refusal press={seeing.press} doing="没有标为已读" />
      <div className="subscription-controls">
        <label className="follows" htmlFor={followsId}>
          更新方式
        </label>
        <select
          id={followsId}
          value={asked ?? subscription.follows}
          disabled={following.press.state === "asking"}
          onChange={(event) => choose(event.target.value as Follows)}
        >
          {(Object.keys(FOLLOWS_NAMES) as Follows[]).map((follows) => (
            <option key={follows} value={follows}>
              {FOLLOWS_NAMES[follows]}
            </option>
          ))}
        </select>
      </div>
      <Refusal press={following.press} doing="没有改变更新方式" />
      <Unsubscribe subscription={subscription} onRemoved={onRemoved} />
    </header>
  );
}
