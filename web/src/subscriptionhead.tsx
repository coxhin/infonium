import { useId, useRef, useState } from "react";
import {
  chooseFollows,
  pollSubscription,
  requestReason,
  seeSubscription,
  unsubscribe,
  type Follows,
  type Subscription,
} from "./client";
import { usePress, type Press } from "./pressing";
import { Moment, toldAhead, toldPast } from "./when";
import "./heard.css";

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
          aria-disabled={press.state === "asking"}
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

type FollowsChoice = {
  // The choice the reader made last, while the service does not hold it yet.
  wanted: Follows | undefined;
  // Why the service refused the last choice it was sent.
  refusal: string | undefined;
  choose: (follows: Follows) => void;
};

// How the subscription follows its feed, as the reader chooses it. One choice is sent at a time,
// and a choice made meanwhile, the last of them, is sent once the one before is answered, so that
// however fast the choices come the service ends up holding the last one, which is on show until
// it does. Each answer is put on show as it comes.
function useFollowsChoice(
  subscriptionId: string,
  onAnswer: (subscription: Subscription) => void,
): FollowsChoice {
  const [wanted, setWanted] = useState<Follows>();
  const [refusal, setRefusal] = useState<string>();
  const sending = useRef(false);
  const latest = useRef<Follows>(undefined);

  function send(follows: Follows) {
    sending.current = true;
    chooseFollows(subscriptionId, follows).then(
      (answer) => {
        onAnswer(answer);
        answered(follows, undefined);
      },
      (error: unknown) => answered(follows, requestReason(error)),
    );
  }

  function answered(sent: Follows, reason: string | undefined) {
    sending.current = false;
    setRefusal(reason);
    const later = latest.current;
    if (later !== undefined && later !== sent) {
      send(later);
      return;
    }
    latest.current = undefined;
    setWanted(undefined);
  }

  function choose(follows: Follows) {
    latest.current = follows;
    setWanted(follows);
    setRefusal(undefined);
    if (!sending.current) {
      send(follows);
    }
  }

  return { wanted, refusal, choose };
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
  const seeing = usePress();
  const choice = useFollowsChoice(subscription.id, onAnswer);
  const followsId = useId();
  const check = useRef<HTMLButtonElement>(null);
  const underway = subscription.poll.kind === "underway" || polling.press.state === "asking";

  // The press is gone once nothing is new, so the keyboard is given to the press beside it.
  function seeAll() {
    seeing.ask(
      () => seeSubscription(subscription.id),
      (answer) => {
        onSeenAll(answer);
        check.current?.focus({ preventScroll: true });
      },
    );
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
          ref={check}
          aria-disabled={underway}
          onClick={() =>
            !underway && polling.ask(() => pollSubscription(subscription.id), onAnswer)
          }
        >
          {underway ? "正在检查…" : "立即检查"}
        </button>
        {subscription.unseen > 0 && (
          <button aria-disabled={seeing.press.state === "asking"} onClick={seeAll}>
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
          value={choice.wanted ?? subscription.follows}
          onChange={(event) => choice.choose(event.target.value as Follows)}
        >
          {(Object.keys(FOLLOWS_NAMES) as Follows[]).map((follows) => (
            <option key={follows} value={follows}>
              {FOLLOWS_NAMES[follows]}
            </option>
          ))}
        </select>
      </div>
      {choice.refusal !== undefined && (
        <p className="status failed" role="alert">
          没有改变更新方式：{choice.refusal}
        </p>
      )}
      <Unsubscribe subscription={subscription} onRemoved={onRemoved} />
    </header>
  );
}
