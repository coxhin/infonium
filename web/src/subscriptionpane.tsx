import { useEffect, useLayoutEffect, useRef } from "react";
import type { Subscription } from "./client";
import { PostingList } from "./postinglist";
import { SubscriptionHead } from "./subscriptionhead";
import { useSubscriptionState } from "./subscriptionstate";
import { useNow } from "./when";

// One subscription, open in the list column in place of the list of subscriptions: the way back
// to the list, which stays in place, and under it its head and its postings, which scroll, read
// again while they are on show. The subscription as read here is told to the column, so that the
// list it goes back to, and the count of new items, say the same. The postings are read again
// when an item is told as seen; one opened at an item looks for that item's posting and brings
// it into view.
export function SubscriptionPane({
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
  const state = useSubscriptionState(subscriptionId, sought, shown);
  const now = useNow();
  const back = useRef<HTMLButtonElement>(null);
  const view = useRef<HTMLDivElement>(null);
  const { loaded } = state;
  const value = loaded.state === "ready" ? loaded.value : undefined;
  const subscription = value?.kind === "present" ? value.subscription : undefined;

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

  // The keyboard, left on the control in the column that opened the subscription, which is
  // hidden now, is given to the way back; one at work elsewhere, or nowhere yet, as when the app
  // opens at the subscription, stays as it is.
  useLayoutEffect(() => {
    if (back.current?.closest("nav")?.contains(document.activeElement) === true) {
      back.current.focus({ preventScroll: true });
    }
  }, []);

  const toldSeen = useRef(seen.size);
  useEffect(() => {
    if (seen.size !== toldSeen.current) {
      toldSeen.current = seen.size;
      state.readNow();
    }
  }, [seen.size]);

  // The posting of the item sought is brought into view once it is among those read.
  const broughtIntoView = useRef(sought === undefined);
  useLayoutEffect(() => {
    if (broughtIntoView.current || value?.kind !== "present") {
      return;
    }
    broughtIntoView.current = true;
    view.current
      ?.querySelector(`[data-item="${CSS.escape(sought ?? "")}"]`)
      ?.scrollIntoView({ block: "center" });
  }, [value]);

  return (
    <div className="subscription-pane">
      <div className="pane-bar">
        <button ref={back} className="back quiet" onClick={onBack}>
          ← 全部订阅
        </button>
        {state.interrupted && <p className="status failed">连接不上服务，正在重试…</p>}
      </div>
      <div className="list-view" ref={view}>
        {loaded.state === "loading" && <p className="status">正在打开…</p>}
        {loaded.state === "failed" && (
          <p className="status failed">
            打不开这个订阅：{loaded.reason}
            <button onClick={state.readNow}>重试</button>
          </p>
        )}
        {value?.kind === "present" && (
          <>
            <SubscriptionHead
              subscription={value.subscription}
              now={now}
              onAnswer={state.answerSubscription}
              onSeenAll={(answer) => {
                state.answerSubscription(answer);
                onChanged();
              }}
              onRemoved={() => onGone(subscriptionId)}
            />
            <PostingList
              subscription={value.subscription}
              postings={value.postings}
              selected={selected}
              seen={seen}
              older={state.older}
              onOlder={state.readOlder}
              onPosting={(posting) => {
                state.answerPosting(posting);
                onChanged();
              }}
            />
          </>
        )}
      </div>
    </div>
  );
}
