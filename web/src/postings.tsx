import { useRef, type ReactNode } from "react";
import { readPosting, type Posting, type Subscription } from "./client";
import { isNew } from "./entry";
import { itemLink } from "./links";
import { NewDot, NewHeard } from "./newmarks";
import { POSTINGS_AT_A_TIME, type OlderReading } from "./postinglist";
import { usePress } from "./pressing";
import { Moment, toldDay, useNow } from "./when";

// A posting the list shows, and what has come of it: its item, read, as a link to the reader;
// its item being read, or failed with the reason and a press to try again; waiting its turn; or
// only listed, with a press that has it read now.
function PostingRow({
  subscriptionId,
  posting,
  selected,
  seen,
  now,
  onPosting,
}: {
  subscriptionId: string;
  posting: Posting;
  selected: string | undefined;
  seen: ReadonlySet<string>;
  now: Date;
  onPosting: (posting: Posting) => void;
}) {
  const { press, ask } = usePress();
  const row = useRef<HTMLLIElement>(null);
  const { admission } = posting;
  const published = posting.published !== null && (
    <Moment at={posting.published} told={toldDay(posting.published, now)} />
  );

  if (admission.kind === "admitted" && admission.item.reading.kind === "read") {
    const item = admission.item;
    const fresh = isNew(item, seen);
    return (
      <li data-item={item.id} ref={row} tabIndex={-1}>
        <a
          href={itemLink(item.id)}
          className={fresh ? "posting new" : "posting"}
          aria-current={item.id === selected}
          title={posting.title}
        >
          <span className="posting-title">
            {fresh && <NewDot />}
            {posting.title}
            {fresh && <NewHeard />}
          </span>
          {published !== false && <small>{published}</small>}
        </a>
      </li>
    );
  }

  // The press is gone once the posting is admitted, so the keyboard is kept on its row.
  function readNow() {
    ask(
      () => readPosting(subscriptionId, posting.number),
      (answer) => {
        onPosting(answer);
        row.current?.focus({ preventScroll: true });
      },
    );
  }

  let state: ReactNode;
  let action: ReactNode;
  switch (admission.kind) {
    case "unadmitted":
      action = (
        <button aria-disabled={press.state === "asking"} onClick={readNow}>
          读取
        </button>
      );
      break;
    case "awaited":
      state = "等待读取";
      break;
    case "admitted":
      if (admission.item.reading.attempt.kind === "failed") {
        const { reason } = admission.item.reading.attempt;
        state = (
          <span className="posting-failed" title={reason}>
            读取失败<span className="heard">：{reason}</span>
          </span>
        );
        action = (
          <button aria-disabled={press.state === "asking"} onClick={readNow}>
            重试
          </button>
        );
      } else {
        state = "读取中…";
      }
      break;
  }
  return (
    <li
      data-item={admission.kind === "admitted" ? admission.item.id : undefined}
      ref={row}
      tabIndex={-1}
    >
      <div className="posting">
        <span className="posting-title" title={posting.title}>
          {posting.title}
        </span>
        <small>
          {published}
          {published !== false && state !== undefined && " · "}
          {state}
        </small>
      </div>
      {action}
      {press.state === "refused" && (
        <p className="status failed" role="alert">
          没有读取：{press.reason}
        </p>
      )}
    </li>
  );
}

// The postings read of a subscription so far, the newest first, with the press that brings
// older ones while there are older ones.
export function PostingList({
  subscription,
  postings,
  selected,
  seen,
  older,
  onOlder,
  onPosting,
}: {
  subscription: Subscription;
  postings: Posting[];
  selected: string | undefined;
  seen: ReadonlySet<string>;
  older: OlderReading;
  onOlder: () => void;
  onPosting: (posting: Posting) => void;
}) {
  const now = useNow();
  if (postings.length === 0) {
    return <p className="status">这个订阅源现在没有条目。</p>;
  }
  const left = subscription.postings - postings.length;
  return (
    <>
      <ul className="postings" aria-label="条目">
        {postings.map((posting) => (
          <PostingRow
            key={posting.number}
            subscriptionId={subscription.id}
            posting={posting}
            selected={selected}
            seen={seen}
            now={now}
            onPosting={onPosting}
          />
        ))}
      </ul>
      {left > 0 && (
        <button
          className="older"
          aria-disabled={older.state === "reading"}
          onClick={() => older.state !== "reading" && onOlder()}
        >
          {older.state === "reading"
            ? "正在载入…"
            : `载入更早的 ${Math.min(left, POSTINGS_AT_A_TIME)} 条（还有 ${left} 条）`}
        </button>
      )}
      {older.state === "failed" && (
        <p className="status failed" role="alert">
          没有载入更早的条目：{older.reason}
        </p>
      )}
    </>
  );
}
