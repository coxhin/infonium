import { useRef, useState } from "react";
import {
  askAboutItem,
  askAboutLibrary,
  forgetItemConversation,
  forgetLibraryConversation,
  readItemConversation,
  readLibraryConversation,
  type ChatEvent,
  type Conversation,
} from "./client";
import { reasonOf, type Loaded } from "./loading";

// What a conversation is about: the open item, or the whole library.
export type Scope = { kind: "item"; itemId: string } | { kind: "library" };

// A question on its way to an answer: what was searched for, and the statements of the
// answer as far as they are written.
export type Pending = { question: string; searches: string[]; statements: string[] };

export type Forgetting = "idle" | "confirming" | "underway";

// All the chat holds of one conversation, kept while another is on show.
export type Chat = {
  conversation: Loaded<Conversation>;
  pending: Pending | undefined;
  typed: string;
  problem: string | undefined;
  forgetting: Forgetting;
};

const LIBRARY_KEY = "library";
const UNREAD: Chat = {
  conversation: { state: "loading" },
  pending: undefined,
  typed: "",
  problem: undefined,
  forgetting: "idle",
};
const CUT_OFF = "连接中断，回答没有写完";

export function scopeKey(scope: Scope): string {
  // An item's id is hexadecimal, so it never reads as the library's key.
  return scope.kind === "item" ? scope.itemId : LIBRARY_KEY;
}

function readConversation(scope: Scope): Promise<Conversation> {
  return scope.kind === "item" ? readItemConversation(scope.itemId) : readLibraryConversation();
}

function asked(scope: Scope, question: string): AsyncGenerator<ChatEvent> {
  return scope.kind === "item" ? askAboutItem(scope.itemId, question) : askAboutLibrary(question);
}

function forgotten(scope: Scope): Promise<void> {
  return scope.kind === "item" ? forgetItemConversation(scope.itemId) : forgetLibraryConversation();
}

// A question that went unanswered goes back into the box, before anything typed since.
function givenBack(chat: Chat, question: string, reason: string): Chat {
  const typed = chat.typed.trim() === "" ? question : `${question}\n${chat.typed}`;
  return { ...chat, pending: undefined, typed, problem: reason };
}

function withPending(chat: Chat, change: (pending: Pending) => Pending): Chat {
  return chat.pending === undefined ? chat : { ...chat, pending: change(chat.pending) };
}

function withTurns(chat: Chat, conversation: Conversation): Chat {
  return { ...chat, conversation: { state: "ready", value: conversation } };
}

export type Conversations = {
  chatOf: (scope: Scope) => Chat;
  read: (scope: Scope) => void;
  type: (scope: Scope, text: string) => void;
  ask: (scope: Scope) => void;
  setForgetting: (scope: Scope, forgetting: Forgetting) => void;
  forget: (scope: Scope) => void;
};

export function canAsk(chat: Chat): boolean {
  return (
    chat.conversation.state === "ready" && chat.pending === undefined && chat.typed.trim() !== ""
  );
}

// The conversations the column has shown, each kept whole while another is on show, and each
// answered on, even when the reader has turned to another.
export function useConversations(): Conversations {
  const [chats, setChats] = useState<ReadonlyMap<string, Chat>>(new Map());
  // A conversation is read once, when it is first shown, and again only when asked to.
  const requested = useRef(new Set<string>());

  function update(scope: Scope, change: (chat: Chat) => Chat) {
    const key = scopeKey(scope);
    setChats((held) => new Map(held).set(key, change(held.get(key) ?? UNREAD)));
  }

  function chatOf(scope: Scope): Chat {
    return chats.get(scopeKey(scope)) ?? UNREAD;
  }

  function reread(scope: Scope) {
    readConversation(scope).then(
      (conversation) => update(scope, (chat) => withTurns(chat, conversation)),
      // The conversation on show stays; the reason for the latest failure is already said.
      () => undefined,
    );
  }

  function read(scope: Scope) {
    const key = scopeKey(scope);
    if (requested.current.has(key) && chats.get(key)?.conversation.state !== "failed") {
      return;
    }
    requested.current.add(key);
    update(scope, (chat) => ({ ...chat, conversation: { state: "loading" } }));
    readConversation(scope).then(
      (conversation) => update(scope, (chat) => withTurns(chat, conversation)),
      (error: unknown) =>
        update(scope, (chat) => ({
          ...chat,
          conversation: { state: "failed", reason: reasonOf(error) },
        })),
    );
  }

  async function answer(scope: Scope, question: string) {
    let heard = false;
    try {
      for await (const event of asked(scope, question)) {
        heard = true;
        switch (event.kind) {
          case "drafted":
            update(scope, (chat) =>
              withPending(chat, (pending) => ({ ...pending, statements: event.statements })),
            );
            break;
          case "searched":
            update(scope, (chat) =>
              withPending(chat, (pending) => ({
                ...pending,
                searches: [...pending.searches, event.query],
              })),
            );
            break;
          case "answered":
            update(scope, (chat) => {
              const turns =
                chat.conversation.state === "ready" ? chat.conversation.value.turns : [];
              return {
                ...withTurns(chat, { turns: [...turns, event.turn] }),
                pending: undefined,
              };
            });
            return;
          case "unanswered":
            update(scope, (chat) => givenBack(chat, question, `没有回答：${event.reason}`));
            return;
        }
      }
      update(scope, (chat) => givenBack(chat, question, `${CUT_OFF}。`));
      reread(scope);
    } catch (error) {
      // A refusal comes before any event; a failure after one is a connection cut off, after
      // which the service may still have kept the answer.
      if (heard) {
        update(scope, (chat) => givenBack(chat, question, `${CUT_OFF}：${reasonOf(error)}`));
        reread(scope);
      } else {
        update(scope, (chat) => givenBack(chat, question, `问题没有被接受：${reasonOf(error)}`));
      }
    }
  }

  function ask(scope: Scope) {
    const chat = chatOf(scope);
    if (!canAsk(chat)) {
      return;
    }
    const question = chat.typed.trim();
    update(scope, (held) => ({
      ...held,
      typed: "",
      problem: undefined,
      forgetting: "idle",
      pending: { question, searches: [], statements: [] },
    }));
    void answer(scope, question);
  }

  function forget(scope: Scope) {
    update(scope, (chat) => ({ ...chat, forgetting: "underway", problem: undefined }));
    forgotten(scope).then(
      () =>
        update(scope, (chat) => ({ ...withTurns(chat, { turns: [] }), forgetting: "idle" })),
      (error: unknown) =>
        update(scope, (chat) => ({
          ...chat,
          forgetting: "idle",
          problem: `没有清空：${reasonOf(error)}`,
        })),
    );
  }

  return {
    chatOf,
    read,
    type: (scope, text) => update(scope, (chat) => ({ ...chat, typed: text })),
    ask,
    setForgetting: (scope, forgetting) => update(scope, (chat) => ({ ...chat, forgetting })),
    forget,
  };
}
