import { useEffect, useLayoutEffect, useRef } from "react";
import type { Posting, Subscription } from "./client";
import { useOpenSubscription, type OpenSubscription } from "./postinglist";
import { PostingList } from "./postings";
import { SubscriptionHead } from "./subscriptionhead";
import { useNow } from "./when";

function withPosting(open: OpenSubscription, posting: Posting): OpenSubscription {
  return open.kind === "open"
    ? {
        ...open,
        postings: open.postings.map((held) => (held.number === posting.number ? posting : held)),
      }
    : open;
}

// One subscription, open in the list column in place of the list of subscriptions: its head and
// its postings, followed while they are on show. The subscription as read here is told to the
// column, so that the list it goes back to, and the count of new items, say the same. The
// postings are read again when an item is told as seen; one opened at an item looks for that
// item's posting and brings it into view.
export function OpenedSubscription({
  subscriptionId,
  sought,
  selected,
  seen,
  shown,
  onBack,
  onView,
  onGone,
  onChanged,
}: {
  subscriptionId: string;
  sought: string | undefined;
  selected: string | undefined;
  seen: ReadonlySet<string>;
  shown: boolean;
  onBack: () => void;
  onView: (subscription: Subscription) => void;
  onGone: (subscriptionId: string) => void;
  // What the library holds has changed by a press here.
  onChanged: () => void;
}) {
  const open = useOpenSubscription(subscriptionId, sought, shown);
  const now = useNow();
  const back = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const { loaded } = open;
  const value = loaded.state === "ready" ? loaded.value : undefined;
  const subscription = value?.kind === "open" ? value.subscription : undefined;

  useEffect(() => {
    if (subscription !== undefined) {
      onView(subscription);
    }
  }, [subscription]);

  useEffect(() => {
    if (value?.kind === "gone") {
      onGone(subscriptionId);
    }
  }, [value?.kind]);

  // The keyboard, left on a control that is gone, is given to the way back.
  useLayoutEffect(() => {
    if (document.activeElement === document.body) {
      back.current?.focus({ preventScroll: true });
    }
  }, []);

  const toldSeen = useRef(seen.size);
  useEffect(() => {
    if (seen.size !== toldSeen.current) {
      toldSeen.current = seen.size;
      open.readNow();
    }
  }, [seen.size]);

  // The posting of the item sought is brought into view once it is among those read.
  const broughtIntoView = useRef(sought === undefined);
  useLayoutEffect(() => {
    if (broughtIntoView.current || value?.kind !== "open") {
      return;
    }
    broughtIntoView.current = true;
    list.current
      ?.querySelector(`[data-item="${CSS.escape(sought ?? "")}"]`)
      ?.scrollIntoView({ block: "center" });
  }, [value]);

  function answered(answer: Subscription) {
    if (value?.kind === "open") {
      open.watchFrom({ ...value, subscription: answer });
    }
  }

  return (
    <div className="opened-subscription" ref={list}>
      <button ref={back} className="back quiet" onClick={onBack}>
        ← 全部订阅
      </button>
      {loaded.state === "loading" && <p className="status">正在打开…</p>}
      {loaded.state === "failed" && (
        <p className="status failed">
          打不开这个订阅：{loaded.reason}
          <button onClick={open.readNow}>重试</button>
        </p>
      )}
      {open.interrupted && <p className="status failed">连接不上服务，正在重试…</p>}
      {value?.kind === "open" && (
        <>
          <SubscriptionHead
            subscription={value.subscription}
            now={now}
            onAnswer={answered}
            onSeenAll={(answer) => {
              answered(answer);
              onChanged();
            }}
            onRemoved={() => onGone(subscriptionId)}
          />
          <PostingList
            subscription={value.subscription}
            postings={value.postings}
            selected={selected}
            seen={seen}
            older={open.older}
            onOlder={open.readOlder}
            onPosting={(posting) => {
              open.watchFrom(withPosting(value, posting));
              onChanged();
            }}
          />
        </>
      )}
    </div>
  );
}
