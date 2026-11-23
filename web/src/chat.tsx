import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type { Anchor, Turn } from "./client";
import { Citations, type Citing } from "./citing";
import {
  canAsk,
  scopeKey,
  useConversations,
  type Chat,
  type Conversations,
  type Pending,
  type Scope,
} from "./conversations";
import "./chat.css";

type ScopeKind = Scope["kind"];
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
export type Opener = (itemId: string, anchor: Anchor) => void;

type Seen = { itemId: string | undefined; documentId: string | undefined };

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
  citing,
  seen,
  onOpen,
}: {
  turn: Turn;
  citing: Citing;
  seen: Seen;
  onOpen: Opener;
}) {
  const { answer } = turn;
  const sourceOf = (anchor: Anchor) =>
    answer.sources.find((source) => source.document === anchor.document_id);

  function titleOf(anchor: Anchor): string {
    const source = sourceOf(anchor);
    return anchor.document_id === seen.documentId || source === undefined
      ? anchor.quote
      : `${anchor.quote}\n——《${source.title}》`;
  }

  function point(anchor: Anchor) {
    const source = sourceOf(anchor);
    // A passage of the item on show, even of a document it no longer shows, is looked for in
    // the reader, which says when it is not there.
    if (anchor.document_id === seen.documentId || source?.item === seen.itemId) {
      citing.point(anchor);
    } else if (source === undefined) {
      citing.fail("回答没有说这段引文出自哪一篇");
    } else {
      citing.pointedElsewhere(anchor);
      onOpen(source.item, anchor);
    }
  }

  return (
    <div className="answer">
      {answer.statements.map((statement, statementIndex) => (
        <p key={statementIndex}>
          {statement.text}
          <Citations
            anchors={statement.anchors}
            citing={citing}
            titleOf={titleOf}
            onPoint={point}
          />
        </p>
      ))}
      <Spent answer={answer} />
    </div>
  );
}

function Draft({ pending }: { pending: Pending }) {
  return (
    <div className="answer draft" aria-busy="true">
      {pending.searches.map((query, queryIndex) => (
        <p key={queryIndex} className="searched">
          检索：{query}
        </p>
      ))}
      {pending.statements.map((statement, statementIndex) => (
        <p key={statementIndex}>{statement}</p>
      ))}
      <p className="writing">{pending.statements.length === 0 ? "正在回答…" : "正在写…"}</p>
    </div>
  );
}

function Question({ children }: { children: ReactNode }) {
  return <p className="question">{children}</p>;
}

// The turns of a conversation, followed by the question being answered. It follows what is
// newest unless the reader has scrolled away from the end, and keeps its place while hidden.
function Thread({
  chat,
  kind,
  shown,
  onRetry,
  children,
}: {
  chat: Chat;
  kind: ScopeKind;
  shown: boolean;
  onRetry: () => void;
  children: (turn: Turn) => ReactNode;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const place = useRef(0);
  const { conversation, pending } = chat;

  function scrolled() {
    const element = scroller.current;
    if (element !== null && shown) {
      place.current = element.scrollTop;
      following.current =
        element.scrollHeight - element.scrollTop - element.clientHeight <= FOLLOWING_SLACK;
    }
  }

  // A question just sent is followed whatever the reader was looking at.
  useLayoutEffect(() => {
    if (pending !== undefined && pending.statements.length === 0) {
      following.current = true;
    }
  }, [pending?.question]);

  useLayoutEffect(() => {
    const element = scroller.current;
    if (element !== null && shown) {
      element.scrollTop = following.current ? element.scrollHeight : place.current;
    }
  });

  return (
    <div className="thread" ref={scroller} onScroll={scrolled} tabIndex={0}>
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
}: {
  chat: Chat;
  scope: Scope;
  conversations: Conversations;
}) {
  const empty = chat.conversation.state !== "ready" || chat.conversation.value.turns.length === 0;
  switch (chat.forgetting) {
    case "idle":
      return (
        <button
          className="quiet"
          disabled={empty || chat.pending !== undefined}
          onClick={() => conversations.setForgetting(scope, "confirming")}
        >
          清空
        </button>
      );
    case "confirming":
      return (
        <span className="confirm" role="group" aria-label="确认清空">
          清空这段对话？
          <button className="danger" onClick={() => conversations.forget(scope)}>
            清空
          </button>
          <button autoFocus onClick={() => conversations.setForgetting(scope, "idle")}>
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
}: {
  chat: Chat;
  scope: Scope;
  conversations: Conversations;
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
  seen,
  onOpen,
}: {
  scope: Scope;
  shown: boolean;
  conversations: Conversations;
  citing: Citing;
  seen: Seen;
  onOpen: Opener;
}) {
  const chat = conversations.chatOf(scope);
  useEffect(() => {
    if (shown) {
      conversations.read(scope);
    }
  }, [shown, scopeKey(scope)]);

  return (
    <div className="chat-scope" hidden={!shown}>
      <Thread chat={chat} kind={scope.kind} shown={shown} onRetry={() => conversations.read(scope)}>
        {(turn) => <Answer turn={turn} citing={citing} seen={seen} onOpen={onOpen} />}
      </Thread>
      <div className="forget">
        <Forget chat={chat} scope={scope} conversations={conversations} />
      </div>
      <Composer chat={chat} scope={scope} conversations={conversations} />
    </div>
  );
}

// The chat about the item on show or about the whole library. With no item on show, only the
// library can be asked.
export function ChatPanel({
  itemId,
  documentId,
  shown,
  citing,
  onOpen,
}: {
  itemId: string | undefined;
  documentId: string | undefined;
  shown: boolean;
  citing: Citing;
  onOpen: Opener;
}) {
  const conversations = useConversations();
  const [chosen, setChosen] = useState<ScopeKind>("item");
  useEffect(() => {
    if (itemId === undefined) {
      setChosen("library");
    }
  }, [itemId]);
  const kind: ScopeKind = itemId === undefined ? "library" : chosen;
  const seen: Seen = { itemId, documentId };

  return (
    <div className="chat">
      <div className="scopes" role="group" aria-label="对话的范围">
        {(Object.keys(SCOPE_NAMES) as ScopeKind[]).map((name) => (
          <button
            key={name}
            aria-pressed={kind === name}
            disabled={name === "item" && itemId === undefined}
            onClick={() => setChosen(name)}
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
          seen={seen}
          onOpen={onOpen}
        />
      )}
      <ChatScope
        scope={{ kind: "library" }}
        shown={shown && kind === "library"}
        conversations={conversations}
        citing={citing}
        seen={seen}
        onOpen={onOpen}
      />
    </div>
  );
}
