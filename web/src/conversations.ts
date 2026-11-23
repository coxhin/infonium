import { useRef, useState } from "react";
import {
  askAboutItem,
  askAboutLibrary,
  forgetItemConversation,
  forgetLibraryConversation,
  readItemConversation,
  readLibraryConversation,
  Refused,
  UnreachedService,
  type ChatEvent,
  type Conversation,
  type Turn,
} from "./client";
import { reasonOf, type Loaded } from "./loading";

// What a conversation is about: the open item, or the whole library.
export type Scope = { kind: "item"; itemId: string } | { kind: "library" };

// A question on its way to an answer: whether it has been sent, what was searched for, and
// the statements of the answer as far as they are written. A question asked while its
// conversation is being read waits, unsent, until the reading is in.
export type Pending = {
  question: string;
  sent: boolean;
  searches: string[];
  statements: string[];
};

export type Forgetting = "idle" | "confirming" | "underway";

// All the chat holds of one conversation, kept while another is on show. What is heard is
// told to a screen reader once: the latest answer that arrived.
export type Chat = {
  conversation: Loaded<Conversation>;
  pending: Pending | undefined;
  typed: string;
  problem: string | undefined;
  forgetting: Forgetting;
  heard: string;
};

// Where a conversation is scrolled to, and whether it follows what is newest. It outlives the
// view of the conversation, which is made anew when its item is opened again.
export type Place = { top: number; following: boolean };

const START: Place = { top: 0, following: true };

const LIBRARY_KEY = "library";
const UNREAD: Chat = {
  conversation: { state: "loading" },
  pending: undefined,
  typed: "",
  problem: undefined,
  forgetting: "idle",
  heard: "",
};
const CUT_OFF = "连接中断，回答没有写完。";
const UNREACHED = "连不上服务，问题没有发出。";
const NOT_READ_AGAIN = "对话也没能重新读取，刷新页面可以再读一次。";
// The service refuses a question, or a clearing, of a conversation that is answering.
const ANSWERING_STATUS = 409;
const ANSWERING = "这段对话正在回答另一个问题，等它答完再问。";
const CLEARING_WHILE_ANSWERING = "这段对话正在回答一个问题，答完后才能清空。";
// A reading after a broken connection is tried this many times, this far apart, since the
// connection may break when this machine's network changes, which settles within a second.
const REREADS = 3;
const REREAD_PAUSE_MS = 1500;

function paused(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

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

function heardOf(turn: Turn): string {
  return `回答已写好：${turn.answer.statements.map((statement) => statement.text).join(" ")}`;
}

export type Conversations = {
  chatOf: (scope: Scope) => Chat;
  placeOf: (scope: Scope) => Place;
  keepPlace: (scope: Scope, place: Place) => void;
  read: (scope: Scope) => void;
  type: (scope: Scope, text: string) => void;
  ask: (scope: Scope) => void;
  setForgetting: (scope: Scope, forgetting: Forgetting) => void;
  forget: (scope: Scope) => void;
};

// A question may be asked while its conversation is being read, and is sent once it is read;
// not while the conversation cannot be read, nor while it is being cleared.
export function canAsk(chat: Chat): boolean {
  return (
    chat.conversation.state !== "failed" &&
    chat.forgetting !== "underway" &&
    chat.pending === undefined &&
    chat.typed.trim() !== ""
  );
}

// The conversations the column has shown, each kept whole while another is on show, and each
// answered on, even when the reader has turned to another.
export function useConversations(): Conversations {
  const [chats, setChats] = useState<ReadonlyMap<string, Chat>>(new Map());
  // A conversation is read once, when it is first shown, and again only when asked to.
  const requested = useRef(new Set<string>());
  const places = useRef(new Map<string, Place>());
  // The conversations being read, and the questions that wait for their reading. A reading
  // never comes in after a question was sent, so it never takes the place of an answer.
  const reading = useRef(new Set<string>());
  const waiting = useRef(new Map<string, string>());
  // How often each conversation was cleared. A reading that began before a clearing brings
  // what was cleared, and is not shown.
  const clearings = useRef(new Map<string, number>());

  function clearingsOf(scope: Scope): number {
    return clearings.current.get(scopeKey(scope)) ?? 0;
  }

  // A reading comes in: it takes the place of the conversation on show unless the
  // conversation was cleared since the reading began.
  function readIn(scope: Scope, conversation: Conversation, begun: number) {
    if (clearingsOf(scope) === begun) {
      update(scope, (chat) => withTurns(chat, conversation));
    }
    readingDone(scope, undefined);
  }

  function update(scope: Scope, change: (chat: Chat) => Chat) {
    const key = scopeKey(scope);
    setChats((held) => new Map(held).set(key, change(held.get(key) ?? UNREAD)));
  }

  function chatOf(scope: Scope): Chat {
    return chats.get(scopeKey(scope)) ?? UNREAD;
  }

  function placeOf(scope: Scope): Place {
    return places.current.get(scopeKey(scope)) ?? START;
  }

  function keepPlace(scope: Scope, place: Place) {
    places.current.set(scopeKey(scope), place);
  }

  // The reading is in, or has failed: a question that waited for it is sent now, or given
  // back when there is no conversation to ask in.
  function readingDone(scope: Scope, given: string | undefined) {
    const key = scopeKey(scope);
    reading.current.delete(key);
    const question = waiting.current.get(key);
    waiting.current.delete(key);
    if (question === undefined) {
      return;
    }
    if (given === undefined) {
      update(scope, (chat) => withPending(chat, (pending) => ({ ...pending, sent: true })));
      void answer(scope, question);
    } else {
      update(scope, (chat) => givenBack(chat, question, given));
    }
  }

  // After a connection broke, the service may still have kept the answer, so the
  // conversation is read again. The one on show stays when it cannot be.
  async function reread(scope: Scope) {
    reading.current.add(scopeKey(scope));
    const begun = clearingsOf(scope);
    for (let attempt = 0; attempt < REREADS; attempt += 1) {
      try {
        readIn(scope, await readConversation(scope), begun);
        return;
      } catch {
        // Tried again after a pause; only the last failure is said, below.
        await paused(REREAD_PAUSE_MS);
      }
    }
    update(scope, (chat) => ({ ...chat, problem: `${chat.problem ?? ""}${NOT_READ_AGAIN}` }));
    readingDone(scope, undefined);
  }

  // The browser's reason for a broken connection is a word of its own, such as "network
  // error", which tells a reader nothing more, so it is not shown.
  function cutOff(scope: Scope, question: string) {
    update(scope, (chat) => givenBack(chat, question, CUT_OFF));
    void reread(scope);
  }

  function read(scope: Scope) {
    const key = scopeKey(scope);
    if (requested.current.has(key) && chats.get(key)?.conversation.state !== "failed") {
      return;
    }
    requested.current.add(key);
    reading.current.add(key);
    const begun = clearingsOf(scope);
    update(scope, (chat) => ({ ...chat, conversation: { state: "loading" } }));
    readConversation(scope).then(
      (conversation) => readIn(scope, conversation, begun),
      (error: unknown) => {
        update(scope, (chat) => ({
          ...chat,
          conversation: { state: "failed", reason: reasonOf(error) },
        }));
        readingDone(scope, "对话没有读到，问题没有发出。");
      },
    );
  }

  async function answer(scope: Scope, question: string) {
    try {
      for await (const event of asked(scope, question)) {
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
                heard: heardOf(event.turn),
              };
            });
            return;
          case "unanswered":
            update(scope, (chat) => givenBack(chat, question, `没有回答：${event.reason}`));
            return;
        }
      }
      cutOff(scope, question);
    } catch (error) {
      if (error instanceof Refused) {
        const reason =
          error.status === ANSWERING_STATUS ? ANSWERING : `问题没有被接受：${error.message}`;
        update(scope, (chat) => givenBack(chat, question, reason));
      } else if (error instanceof UnreachedService) {
        // The question was never received, so the conversation has nothing new to read.
        update(scope, (chat) => givenBack(chat, question, UNREACHED));
      } else {
        cutOff(scope, question);
      }
    }
  }

  function ask(scope: Scope) {
    const chat = chatOf(scope);
    if (!canAsk(chat)) {
      return;
    }
    const question = chat.typed.trim();
    const key = scopeKey(scope);
    const sent = !reading.current.has(key);
    update(scope, (held) => ({
      ...held,
      typed: "",
      problem: undefined,
      forgetting: "idle",
      pending: { question, sent, searches: [], statements: [] },
    }));
    if (sent) {
      void answer(scope, question);
    } else {
      waiting.current.set(key, question);
    }
  }

  function forget(scope: Scope) {
    update(scope, (chat) => ({ ...chat, forgetting: "underway", problem: undefined }));
    forgotten(scope).then(
      () => {
        clearings.current.set(scopeKey(scope), clearingsOf(scope) + 1);
        update(scope, (chat) => ({ ...withTurns(chat, { turns: [] }), forgetting: "idle" }));
      },
      (error: unknown) =>
        update(scope, (chat) => ({
          ...chat,
          forgetting: "idle",
          problem:
            error instanceof Refused && error.status === ANSWERING_STATUS
              ? CLEARING_WHILE_ANSWERING
              : `没有清空：${reasonOf(error)}`,
        })),
    );
  }

  return {
    chatOf,
    placeOf,
    keepPlace,
    read,
    type: (scope, text) => update(scope, (chat) => ({ ...chat, typed: text })),
    ask,
    setForgetting: (scope, forgetting) => update(scope, (chat) => ({ ...chat, forgetting })),
    forget,
  };
}
