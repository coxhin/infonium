import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { Subscription } from "./client";
import { Interruption } from "./interruption";
import { PostingList } from "./postinglist";
import { SubscriptionHead } from "./subscriptionhead";
import { useSubscriptionState } from "./subscriptionstate";
import { useNow } from "./when";

const ALREADY_SUBSCRIBED = "已经订阅过这个订阅源，这里就是它；这次没有再读取条目。";

// One subscription, open in the list column as the sidebar selected it: what is said of it, which
// stays in place, and under it its head and its postings, which scroll, read again while they are
// on show. The subscription as read here is told to the column, so that the sidebar's row of it
// and its count of new items say the same. The postings are read again when an item is told as
// seen; one opened at an item looks for that item's posting and brings it into view. One opened
// as the answer to subscribing to a feed subscribed to already says so, until the reader does
// something else.
export function SubscriptionPane({
  subscriptionId,
  sought,
  again,
  selected,
  seen,
  onView,
  onGone,
  onChanged,
}: {
  subscriptionId: string;
  sought: string | undefined;
  again: boolean;
  selected: string | undefined;
  seen: ReadonlySet<string>;
  onView: (subscription: Subscription) => void;
  onGone: (subscriptionId: string) => void;
  // What the library holds has changed by a press here.
  onChanged: () => void;
}) {
  const state = useSubscriptionState(subscriptionId, sought);
  const now = useNow();
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

  const [toldAgain, setToldAgain] = useState(again);
  useEffect(() => {
    if (!toldAgain) {
      return undefined;
    }
    const doneWith = () => setToldAgain(false);
    document.addEventListener("pointerdown", doneWith);
    document.addEventListener("keydown", doneWith);
    return () => {
      document.removeEventListener("pointerdown", doneWith);
      document.removeEventListener("keydown", doneWith);
    };
  }, [toldAgain]);

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
        {state.interrupted && <Interruption refusal={state.refusal} />}
        {/* Stays in the page while it says nothing, so that what it comes to say is read out. */}
        <p className="status told-again" role="status">
          {toldAgain && ALREADY_SUBSCRIBED}
        </p>
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
