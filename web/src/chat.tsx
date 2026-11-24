import {
  useEffect,
  useLayoutEffect,
  useRef,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
} from "react";
import type { Anchor, Anchors, Turn } from "./client";
import { Citations, LostCitations, type Citing } from "./citing";
import {
  canAsk,
  scopeKey,
  useConversations,
  type Chat,
  type Conversations,
  type Pending,
  type Place,
  type Scope,
} from "./conversations";
import { NOTE_GUTTER, NoteDown, statementTaking } from "./notedown";
import type { Seat } from "./seat";
import "./chat.css";

export type ScopeKind = Scope["kind"];
const SCOPE_NAMES: Record<ScopeKind, string> = { item: "本文", library: "整个库" };
const INVITATIONS: Record<ScopeKind, string> = {
  item: "问一个关于这一篇的问题。回答里的每一句都附上原文的出处。",
  library: "问一个关于整个库的问题。回答会检索库里的各篇，并附上出处。",
};
const PLACEHOLDERS: Record<ScopeKind, string> = {
  item: "问这一篇…",
  library: "问整个库…",
};
// How near the end, in pixels, the reader must be for the conversation to follow what is
// newest.
const FOLLOWING_SLACK = 24;

// Where a cited passage is: in the document on show, which the reader shows, or in another
// item, which is then opened at it.
export type Opener = (itemId: string, anchors: Anchors) => void;

function Spent({ answer }: { answer: Turn["answer"] }) {
  return (
    <footer className="spent">
      {answer.model} · 输入 {answer.input_tokens.toLocaleString()} · 输出{" "}
      {answer.output_tokens.toLocaleString()} tokens
    </footer>
  );
}

function Answer({
  turn,
  scope,
  citing,
  seat,
  onOpen,
}: {
  turn: Turn;
  scope: Scope;
  citing: Citing;
  seat: Seat | undefined;
  onOpen: Opener;
}) {
  const { answer } = turn;
  const sourceOf = (anchor: Anchor) =>
    answer.sources.find((source) => source.document === anchor.document_id);
  // The item a citation lies in: the conversation's own, or, in the library's, the item of the
  // source that holds the citation's document.
  const itemOf = (anchor: Anchor) =>
    scope.kind === "item" ? scope.itemId : sourceOf(anchor)?.item;

  function titleOf(anchor: Anchor): string {
    const source = sourceOf(anchor);
    return anchor.document_id === seat?.documentId || source === undefined
      ? anchor.quote
      : `${anchor.quote}\n——《${source.title}》`;
  }

  function point(anchor: Anchor) {
    const source = sourceOf(anchor);
    // A passage of the item on show, even of a document it no longer shows, is looked for in
    // the reader, which says when it is not there. A passage of the item that is opening, whose
    // reader is not on show yet, is shown once it is.
    if (
      seat !== undefined &&
      (anchor.document_id === seat.documentId || source?.item === seat.itemId)
    ) {
      citing.point([anchor]);
    } else if (source === undefined) {
      citing.fail("回答没有说这段引文出自哪一篇");
    } else {
      onOpen(source.item, [anchor]);
    }
  }

  return (
    <div className="answer">
      {answer.statements.map((statement, statementIndex) => (
        <p key={statementIndex} className={NOTE_GUTTER}>
          {statement.text}
          <Citations
            anchors={statement.anchors}
            citing={citing}
            titleOf={titleOf}
            onPoint={point}
          />
          <LostCitations statement={statement} citesAll={false} />
          <NoteDown
            taking={statementTaking([statement], itemOf)}
            label="记到笔记：这句回答"
            shape="mark"
          />
        </p>
      ))}
      {scope.kind === "library" && answer.sources.length > 0 && (
        <p className="sources">
          出处：
          {answer.sources.map((source) => (
            <span key={source.document} title={source.title}>
              {source.title}
            </span>
          ))}
        </p>
      )}
      <div className="answer-foot">
        <Spent answer={answer} />
        <NoteDown taking={statementTaking(answer.statements, itemOf)} label="记到笔记：整个回答" />
      </div>
    </div>
  );
}

function writingNote(pending: Pending): string {
  if (!pending.sent) {
    return "对话读完后发出…";
  }
  return pending.statements.length === 0 ? "正在回答…" : "正在写…";
}

// The library may be searched many times for one answer, with long queries, so only the
// latest is shown, with how many there were; all of them are the line's title.
function Draft({ pending }: { pending: Pending }) {
  const searches = pending.searches.length;
  return (
    <div className="answer draft" aria-busy="true">
      {searches > 0 && (
        <p className="searched" title={pending.searches.join("\n")}>
          {searches === 1 ? "检索" : `第 ${searches} 次检索`}：{pending.searches[searches - 1]}
        </p>
      )}
      {pending.statements.map((statement, statementIndex) => (
        <p key={statementIndex} className={NOTE_GUTTER}>{statement}</p>
      ))}
      <p className="writing">{writingNote(pending)}</p>
    </div>
  );
}

function Question({ children }: { children: ReactNode }) {
  return <p className="question">{children}</p>;
}

// The turns of a conversation, followed by the question being answered. It follows what is
// newest unless the reader has scrolled away from the end, and keeps its place while hidden.
// It starts at the place it was left at, and hands each new place back to be kept for when
// its item is opened again.
function Thread({
  chat,
  kind,
  shown,
  startPlace,
  onPlace,
  onRetry,
  children,
}: {
  chat: Chat;
  kind: ScopeKind;
  shown: boolean;
  startPlace: Place;
  onPlace: (place: Place) => void;
  onRetry: () => void;
  children: (turn: Turn) => ReactNode;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const place = useRef(startPlace);
  const { conversation, pending } = chat;

  function moveTo(next: Place) {
    place.current = next;
    onPlace(next);
  }

  function scrolled() {
    const element = scroller.current;
    if (element !== null && shown) {
      moveTo({
        top: element.scrollTop,
        following:
          element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOWING_SLACK,
      });
    }
  }

  // A question just sent is followed whatever the reader was looking at.
  useLayoutEffect(() => {
    if (pending !== undefined && pending.statements.length === 0) {
      moveTo({ ...place.current, following: true });
    }
  }, [pending?.question]);

  useLayoutEffect(() => {
    const element = scroller.current;
    if (element !== null && shown) {
      element.scrollTop = place.current.following ? element.scrollHeight : place.current.top;
    }
  });

  // An answer that has just arrived and is taller than the thread is shown from its question,
  // so that it is read from its start; the thread then stays there.
  const heard = useRef(chat.heard);
  useLayoutEffect(() => {
    const element = scroller.current;
    const arrived = element?.querySelector("article:last-of-type");
    if (
      chat.heard !== heard.current &&
      element !== null &&
      arrived instanceof HTMLElement &&
      shown &&
      place.current.following &&
      arrived.offsetHeight > element.clientHeight
    ) {
      element.scrollTop +=
        arrived.getBoundingClientRect().top - element.getBoundingClientRect().top;
      moveTo({ top: element.scrollTop, following: false });
    }
    heard.current = chat.heard;
  }, [chat.heard]);

  return (
    // The thread takes the focus so that it can be scrolled from the keyboard.
    <div
      className="thread"
      ref={scroller}
      onScroll={scrolled}
      tabIndex={0}
      role="region"
      aria-label={`对话：${SCOPE_NAMES[kind]}`}
    >
      {conversation.state === "loading" && <p className="status">正在读取对话…</p>}
      {conversation.state === "failed" && (
        <p className="status failed">
          读不到对话：{conversation.reason}{" "}
          <button className="quiet" onClick={onRetry}>
            重新读取
          </button>
        </p>
      )}
      {conversation.state === "ready" &&
        conversation.value.turns.length === 0 &&
        pending === undefined && <p className="status">{INVITATIONS[kind]}</p>}
      {conversation.state === "ready" &&
        conversation.value.turns.map((turn, turnIndex) => (
          <article key={turnIndex}>
            <Question>{turn.question}</Question>
            {children(turn)}
          </article>
        ))}
      {pending !== undefined && (
        <article>
          <Question>{pending.question}</Question>
          <Draft pending={pending} />
        </article>
      )}
    </div>
  );
}

function Forget({
  chat,
  scope,
  conversations,
  questionBox,
}: {
  chat: Chat;
  scope: Scope;
  conversations: Conversations;
  questionBox: RefObject<HTMLTextAreaElement | null>;
}) {
  const forgetButton = useRef<HTMLButtonElement>(null);
  const cancelled = useRef(false);
  const empty = chat.conversation.state !== "ready" || chat.conversation.value.turns.length === 0;

  // The control that was pressed goes away, so the focus is put where the reader goes on.
  useLayoutEffect(() => {
    if (cancelled.current && chat.forgetting === "idle") {
      cancelled.current = false;
      forgetButton.current?.focus();
    }
  }, [chat.forgetting]);

  function cancel() {
    cancelled.current = true;
    conversations.setForgetting(scope, "idle");
  }

  function confirm() {
    questionBox.current?.focus();
    conversations.forget(scope);
  }

  switch (chat.forgetting) {
    case "idle":
      return (
        <button
          ref={forgetButton}
          className="quiet"
          disabled={empty || chat.pending !== undefined}
          onClick={() => conversations.setForgetting(scope, "confirming")}
        >
          清空
        </button>
      );
    case "confirming":
      return (
        <span
          className="confirm"
          role="group"
          aria-label="确认清空"
          onKeyDown={(event) => event.key === "Escape" && cancel()}
        >
          清空这段对话？
          <button className="danger" onClick={confirm}>
            清空
          </button>
          <button autoFocus onClick={cancel}>
            取消
          </button>
        </span>
      );
    case "underway":
      return <span className="confirm">正在清空…</span>;
  }
}

function Composer({
  chat,
  scope,
  conversations,
  questionBox,
}: {
  chat: Chat;
  scope: Scope;
  conversations: Conversations;
  questionBox: RefObject<HTMLTextAreaElement | null>;
}) {
  function keyed(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter while an input method is composing picks a candidate; it sends nothing.
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      conversations.ask(scope);
    }
  }

  return (
    <form
      className="composer"
      onSubmit={(event) => {
        event.preventDefault();
        conversations.ask(scope);
      }}
    >
      {chat.problem !== undefined && (
        <p className="status failed" role="alert">
          {chat.problem}
        </p>
      )}
      <textarea
        ref={questionBox}
        aria-label={`提问：${SCOPE_NAMES[scope.kind]}`}
        placeholder={PLACEHOLDERS[scope.kind]}
        rows={3}
        value={chat.typed}
        onChange={(event) => conversations.type(scope, event.target.value)}
        onKeyDown={keyed}
      />
      <div className="send">
        <small>Enter 发送 · Shift+Enter 换行</small>
        <button disabled={!canAsk(chat)}>
          {chat.pending === undefined ? "发送" : "回答中…"}
        </button>
      </div>
    </form>
  );
}

// One conversation: its turns, the control that clears them, and the box that asks.
function ChatScope({
  scope,
  shown,
  conversations,
  citing,
  seat,
  onOpen,
}: {
  scope: Scope;
  shown: boolean;
  conversations: Conversations;
  citing: Citing;
  seat: Seat | undefined;
  onOpen: Opener;
}) {
  const chat = conversations.chatOf(scope);
  const questionBox = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (shown) {
      conversations.read(scope);
    }
  }, [shown, scopeKey(scope)]);

  return (
    <div className="chat-scope" hidden={!shown}>
      <Thread
        chat={chat}
        kind={scope.kind}
        shown={shown}
        startPlace={conversations.placeOf(scope)}
        onPlace={(place) => conversations.keepPlace(scope, place)}
        onRetry={() => conversations.read(scope)}
      >
        {(turn) => (
          <Answer turn={turn} scope={scope} citing={citing} seat={seat} onOpen={onOpen} />
        )}
      </Thread>
      {/* An answer that has arrived is told to a screen reader once, not as it grows. */}
      <p className="heard" role="status">
        {chat.heard}
      </p>
      <div className="forget">
        <Forget
          chat={chat}
          scope={scope}
          conversations={conversations}
          questionBox={questionBox}
        />
      </div>
      <Composer
        chat={chat}
        scope={scope}
        conversations={conversations}
        questionBox={questionBox}
      />
    </div>
  );
}

// The chat about the item on show or about the whole library. With no item on show, only the
// library can be asked, whatever was chosen.
export function ChatPanel({
  itemId,
  seat,
  scope,
  onScope,
  shown,
  citing,
  onOpen,
}: {
  itemId: string | undefined;
  seat: Seat | undefined;
  scope: ScopeKind;
  onScope: (scope: ScopeKind) => void;
  shown: boolean;
  citing: Citing;
  onOpen: Opener;
}) {
  const conversations = useConversations();
  const kind: ScopeKind = itemId === undefined ? "library" : scope;

  return (
    <div className="chat">
      <div className="scopes" role="group" aria-label="对话的范围">
        {(Object.keys(SCOPE_NAMES) as ScopeKind[]).map((name) => (
          <button
            key={name}
            aria-pressed={kind === name}
            disabled={name === "item" && itemId === undefined}
            onClick={() => onScope(name)}
          >
            {SCOPE_NAMES[name]}
          </button>
        ))}
      </div>
      {itemId !== undefined && (
        <ChatScope
          key={itemId}
          scope={{ kind: "item", itemId }}
          shown={shown && kind === "item"}
          conversations={conversations}
          citing={citing}
          seat={seat}
          onOpen={onOpen}
        />
      )}
      <ChatScope
        scope={{ kind: "library" }}
        shown={shown && kind === "library"}
        conversations={conversations}
        citing={citing}
        seat={seat}
        onOpen={onOpen}
      />
    </div>
  );
}
