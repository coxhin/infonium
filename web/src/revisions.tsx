import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import type { ItemRevisions, Revision } from "./client";
import { itemLink, revisionLink } from "./links";
import type { Loaded } from "./loading";
import { revisionName, revisionTold, whenRead } from "./revisionwords";
import "./revisions.css";

// Where a reading of the item that the reader started, or found under way, stands as the head
// tells it: being asked for, refused, under way from the document the item held and the
// documents its revisions held (not yet known of a reading found under way before its
// revisions are read), or settled into a document, which is told once and then let go.
export type Rereading =
  | { state: "idle" }
  | { state: "asking" }
  | { state: "refused"; reason: string }
  | { state: "reading"; before: string; known: string[] | undefined }
  | { state: "settled"; before: string; known: string[] | undefined; after: string };

export const NOT_REREADING: Rereading = { state: "idle" };

const WHAT_HAPPENS =
  "重新读取会再次抓取这一篇的网页，或从保存的文件再读一遍；PDF 和录音可能需要几分钟。" +
  "读取期间这里仍显示现在的内容。读完后若内容有变，会成为新的版本，已做的高亮会在新的内容里重新查找。";

// What a settled reading gave, once the revisions read since it settled are in: a new
// revision, the one the item held, or an earlier one again.
function settledText(
  rereading: Rereading & { state: "settled" },
  revisions: ItemRevisions,
): string | undefined {
  const holding = revisions.revisions.find((revision) => revision.number === revisions.current);
  if (holding?.document !== rereading.after) {
    return undefined;
  }
  if (rereading.after === rereading.before) {
    return `重新读取完了：内容没有变，仍是${revisionName(holding)}的版本。`;
  }
  if (rereading.known?.includes(rereading.after) === true) {
    return `重新读取完了：内容和${revisionName(holding)}的版本相同，现在显示的是它。`;
  }
  return `重新读取完了：得到了新的版本，${revisionTold(holding)}。`;
}

// The revisions of the item, as a control in the reader's head that says which one is on
// show and lists them all, each a link that opens it. The one the item holds now opens as the
// item's plain link does.
function RevisionPicker({
  itemId,
  revisions,
  shown,
}: {
  itemId: string;
  revisions: ItemRevisions;
  shown: Revision;
}) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const toggle = useRef<HTMLButtonElement>(null);
  const holder = useRef<HTMLDivElement>(null);
  const onShow = useRef<HTMLAnchorElement>(null);

  useEffect(() => {
    if (open) {
      onShow.current?.focus();
    }
  }, [open]);

  function close(refocus: boolean) {
    setOpen(false);
    if (refocus) {
      toggle.current?.focus();
    }
  }

  function onKey(event: KeyboardEvent) {
    if (event.key === "Escape" && open) {
      event.preventDefault();
      close(true);
    }
  }

  const label = `${revisionName(shown)} · ${whenRead(shown)}`;
  const which = shown.number === revisions.current ? "现在的版本" : "较早的版本";
  const spoken = `版本：${label}，${which}。共 ${revisions.revisions.length} 个版本`;
  return (
    <div
      className="revision-picker"
      ref={holder}
      onKeyDown={onKey}
      onBlur={(event) => {
        if (!holder.current?.contains(event.relatedTarget)) {
          setOpen(false);
        }
      }}
    >
      <button
        ref={toggle}
        className="quiet"
        aria-expanded={open}
        aria-controls={listId}
        aria-label={spoken}
        onClick={() => setOpen(!open)}
      >
        {label}
        {shown.number !== revisions.current && <span className="earlier-mark">较早</span>}
        <span aria-hidden="true"> ▾</span>
      </button>
      <ul id={listId} className="revision-list" hidden={!open} aria-label="这一篇的各次读取">
        {[...revisions.revisions].reverse().map((revision) => (
          <li key={revision.number}>
            <a
              ref={revision.number === shown.number ? onShow : undefined}
              href={
                revision.number === revisions.current
                  ? itemLink(itemId)
                  : revisionLink(itemId, revision.number)
              }
              aria-current={revision.number === shown.number ? "page" : undefined}
              onClick={() => close(false)}
            >
              <span>{revisionName(revision)}</span>
              <time dateTime={revision.read}>{whenRead(revision)}</time>
              {revision.number === revisions.current && (
                <span className="holding-mark">现在的版本</span>
              )}
            </a>
          </li>
        ))}
      </ul>
    </div>
  );
}

// The part of the reader's head that is about revisions: which one is on show and the others,
// the offer to read the source again, which says first what will happen and starts only once
// the reader confirms, and what came of the last reading. It says why the revisions could not
// be read, and, as the library does, that the watch of a reading has lost the service and is
// trying again.
export function RevisionHead({
  itemId,
  revisions: loaded,
  shownDocument,
  underway,
  interrupted,
  rereading,
  onReadAgain,
  onTold,
}: {
  itemId: string;
  revisions: Loaded<ItemRevisions>;
  shownDocument: string;
  underway: boolean;
  interrupted: boolean;
  rereading: Rereading;
  onReadAgain: () => void;
  onTold: () => void;
}) {
  const [asking, setAsking] = useState(false);
  const askId = useId();
  const offer = useRef<HTMLButtonElement>(null);
  const confirm = useRef<HTMLButtonElement>(null);
  const busy = underway || rereading.state === "asking" || rereading.state === "reading";

  useEffect(() => {
    if (asking) {
      confirm.current?.focus();
    }
  }, [asking]);

  function cancel() {
    setAsking(false);
    offer.current?.focus();
  }

  const revisions = loaded.state === "ready" ? loaded.value : undefined;
  const shown = revisions?.revisions.find((revision) => revision.document === shownDocument);
  // Told once the document the reading gave is on show, not while the one before it stays.
  const told =
    rereading.state === "settled" && revisions !== undefined && shownDocument === rereading.after
      ? settledText(rereading, revisions)
      : undefined;
  return (
    <>
      <div className="revision-controls">
        {revisions !== undefined && revisions.revisions.length > 1 && shown !== undefined && (
          <RevisionPicker itemId={itemId} revisions={revisions} shown={shown} />
        )}
        <button
          ref={offer}
          className="quiet"
          disabled={busy}
          aria-expanded={asking && !busy}
          aria-controls={askId}
          onClick={() => setAsking(!asking)}
        >
          {busy ? "正在重新读取…" : "重新读取"}
        </button>
      </div>
      <div
        id={askId}
        className="reading-line read-again-ask"
        role="group"
        aria-label="确认重新读取"
        hidden={!asking || busy}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            cancel();
          }
        }}
      >
        <span>{WHAT_HAPPENS}</span>
        <span className="read-again-choices">
          <button
            ref={confirm}
            className="confirm"
            onClick={() => {
              setAsking(false);
              onReadAgain();
            }}
          >
            开始重新读取
          </button>
          <button onClick={cancel}>取消</button>
        </span>
      </div>
      {rereading.state === "refused" && (
        <p className="reading-line failed" role="alert">
          没能开始重新读取：{rereading.reason}
        </p>
      )}
      {loaded.state === "failed" && (
        <p className="reading-line failed" role="alert">
          读不到这一篇的各次读取，不知道还有没有别的版本：{loaded.reason}
        </p>
      )}
      {interrupted && (
        <p className="reading-line failed" role="alert">
          连接不上服务，正在重试…
        </p>
      )}
      {/* Stays in the page while it says nothing, so that what it comes to say is read out. */}
      <p className="reading-line told" role="status">
        {told}
        {told !== undefined && (
          <button className="quiet" onClick={onTold}>
            知道了
          </button>
        )}
      </p>
    </>
  );
}

// The line above an earlier revision's text: which reading it is and of when, and the way
// back to what the item holds now.
export function EarlierLine({
  itemId,
  earlier,
  holding,
}: {
  itemId: string;
  earlier: Revision;
  holding: Revision | undefined;
}) {
  return (
    <p className="earlier-line">
      这是这一篇较早的一次读取：{revisionTold(earlier)}
      。这里只显示它当时读到的原文，没有译文和原件。
      <a href={itemLink(itemId)}>
        回到现在的版本{holding === undefined ? "" : `（${revisionName(holding)}）`}
      </a>
    </p>
  );
}
