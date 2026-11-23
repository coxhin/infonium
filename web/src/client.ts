import createClient from "openapi-fetch";
import type { components, paths } from "./protocol";
import { reasonOf } from "./loading";

type Schemas = components["schemas"];
export type ItemEntry = Schemas["ItemEntry"];
export type ItemDocument = Schemas["DocumentView"];
export type Block = ItemDocument["blocks"][number];
export type Mark = Schemas["Paragraph"]["marks"][number];
export type Place = Block["places"][number];
export type PagePlace = Schemas["PagePlace"];
export type TimePlace = Schemas["TimePlace"];
export type Style = Schemas["Style"];
export type Digest = Schemas["DigestView"];
export type Anchor = Schemas["Anchor"];
export type Statement = Schemas["Statement"];
export type Translation = Schemas["TranslationView"];
export type TranslatedBlock = Schemas["TranslatedBlock"];
export type Segment = Schemas["Segment"];
export type Page = Schemas["PageView"];
export type Conversation = Schemas["Conversation"];
export type Turn = Schemas["Turn"];
// What happens while a question is answered. The protocol lists these as the items of a
// stream, which the generated types leave out, so the kinds are named here.
export type ChatEvent =
  | Schemas["Drafted"]
  | Schemas["Searched"]
  | Schemas["Answered"]
  | Schemas["Unanswered"];
type Refusal = Schemas["Failure"] | Schemas["HTTPValidationError"];

const SOURCE_PATH = "/api/items/{item_id}/source" satisfies keyof paths;
const ITEM_PLACE = "{item_id}";

const api = createClient<paths>();

// Where the file an item's document was read from is served: the PDF, or the recording.
export function sourceLink(itemId: string): string {
  return SOURCE_PATH.replace(ITEM_PLACE, itemId);
}

function refusalText(refusal: Refusal | undefined): string {
  const detail = refusal?.detail;
  if (typeof detail === "string") {
    return detail;
  }
  return detail?.map((problem) => problem.msg).join("；") ?? "请求没有成功";
}

async function settled<Data>(request: Promise<{ data?: Data; error?: Refusal }>): Promise<Data> {
  const { data, error } = await request;
  if (data === undefined) {
    throw new Error(refusalText(error));
  }
  return data;
}

// A request that answers with nothing when it succeeds.
async function done(request: Promise<{ error?: Refusal; response: Response }>): Promise<void> {
  const { error, response } = await request;
  if (!response.ok) {
    throw new Error(refusalText(error));
  }
}

// The service answered a question with a status other than success, so it began no answer.
export class RefusedQuestion extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

// The question never reached the service: the request itself failed, before any answer.
// Any failure of a question that is neither this nor a refusal is a connection that broke.
export class UnreachedService extends Error {}

// The request is told apart from what is read after it only by the kind of its failure.
function reached<Answer>(request: Promise<Answer>): Promise<Answer> {
  return request.catch((error: unknown) => {
    throw new UnreachedService(reasonOf(error), { cause: error });
  });
}

// The events of one question, read as they arrive: each is one line of JSON.
async function* eventLines(
  request: Promise<{
    data?: ReadableStream<Uint8Array> | null;
    error?: Refusal;
    response: Response;
  }>,
): AsyncGenerator<ChatEvent> {
  const { data, error, response } = await reached(request);
  if (!response.ok) {
    throw new RefusedQuestion(refusalText(error), response.status);
  }
  if (data === undefined || data === null) {
    throw new Error("服务没有送来任何内容");
  }
  const arriving = data.getReader();
  const decoder = new TextDecoder();
  let unfinished = "";
  for (;;) {
    const { done: ended, value } = await arriving.read();
    const lines = `${unfinished}${decoder.decode(value, { stream: !ended })}`.split("\n");
    // The last piece is a line only once the stream has ended; until then more of it may come.
    unfinished = ended ? "" : (lines.pop() ?? "");
    for (const line of lines) {
      if (line.trim() !== "") {
        yield JSON.parse(line) as ChatEvent;
      }
    }
    if (ended) {
      return;
    }
  }
}

export function listItems(): Promise<ItemEntry[]> {
  return settled(api.GET("/api/items"));
}

export function importLink(url: string): Promise<ItemEntry> {
  return settled(api.POST("/api/items", { body: { url } }));
}

export function importUpload(file: File): Promise<ItemEntry> {
  return settled(
    api.POST("/api/items/upload", {
      // The protocol describes the file as a string of bytes; the form carries the file.
      body: { file: file as unknown as string },
      bodySerializer: (body) => {
        const form = new FormData();
        form.append("file", body.file);
        return form;
      },
    }),
  );
}

export function readEntry(itemId: string): Promise<ItemEntry> {
  return settled(api.GET("/api/items/{item_id}", { params: { path: { item_id: itemId } } }));
}

export function readDocument(itemId: string): Promise<ItemDocument> {
  return settled(
    api.GET("/api/items/{item_id}/document", { params: { path: { item_id: itemId } } }),
  );
}

export function rewriteDigest(itemId: string): Promise<Digest> {
  return settled(
    api.POST("/api/items/{item_id}/digest", { params: { path: { item_id: itemId } } }),
  );
}

// Writing a digest takes a model call, so views that ask for the same item's digest while one
// request is under way share that request.
const digestsUnderWay = new Map<string, Promise<Digest>>();

export function ensureDigest(itemId: string): Promise<Digest> {
  const underWay =
    digestsUnderWay.get(itemId) ??
    settled(
      api.PUT("/api/items/{item_id}/digest", { params: { path: { item_id: itemId } } }),
    ).finally(() => digestsUnderWay.delete(itemId));
  digestsUnderWay.set(itemId, underWay);
  return underWay;
}

export function readTranslation(itemId: string): Promise<Translation> {
  return settled(
    api.GET("/api/items/{item_id}/translation", { params: { path: { item_id: itemId } } }),
  );
}

// Starts the translation unless it is under way, done or unneeded.
export function ensureTranslation(itemId: string): Promise<Translation> {
  return settled(
    api.PUT("/api/items/{item_id}/translation", { params: { path: { item_id: itemId } } }),
  );
}

export function translateAnew(itemId: string): Promise<Translation> {
  return settled(
    api.POST("/api/items/{item_id}/translation", { params: { path: { item_id: itemId } } }),
  );
}

// The service refused a request, with this status, which says what kind of refusal it is.
export class RefusedRequest extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

// Translating one block again is refused for reasons a reader is told apart: the translation
// was never asked for, the block is never translated, or the model failed.
export async function retranslateBlock(
  itemId: string,
  blockNumber: number,
): Promise<TranslatedBlock> {
  const { data, error, response } = await api.POST(
    "/api/items/{item_id}/translation/blocks/{block_number}",
    { params: { path: { item_id: itemId, block_number: blockNumber } } },
  );
  if (data === undefined) {
    throw new RefusedRequest(refusalText(error), response.status);
  }
  return data;
}

export function readPage(itemId: string): Promise<Page> {
  return settled(api.GET("/api/items/{item_id}/page", { params: { path: { item_id: itemId } } }));
}

export function readItemConversation(itemId: string): Promise<Conversation> {
  return settled(
    api.GET("/api/items/{item_id}/conversation", { params: { path: { item_id: itemId } } }),
  );
}

export function askAboutItem(itemId: string, text: string): AsyncGenerator<ChatEvent> {
  return eventLines(
    api.POST("/api/items/{item_id}/conversation", {
      params: { path: { item_id: itemId } },
      body: { text },
      parseAs: "stream",
    }),
  );
}

export function forgetItemConversation(itemId: string): Promise<void> {
  return done(
    api.DELETE("/api/items/{item_id}/conversation", { params: { path: { item_id: itemId } } }),
  );
}

export function readLibraryConversation(): Promise<Conversation> {
  return settled(api.GET("/api/conversation"));
}

export function askAboutLibrary(text: string): AsyncGenerator<ChatEvent> {
  return eventLines(api.POST("/api/conversation", { body: { text }, parseAs: "stream" }));
}

export function forgetLibraryConversation(): Promise<void> {
  return done(api.DELETE("/api/conversation"));
}
