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
export type DigestStyle = Schemas["DigestStyle"];
export type StyleOffer = Schemas["DigestStyleView"];
// A built-in style is sent without instructions: openapi-fetch's type of a request's body
// refuses every value of a property that can only be null, as theirs is in the protocol.
export type ChosenStyle = Omit<Schemas["RecipeChoice"], "instructions"> | Schemas["CustomChoice"];
export type Anchor = Schemas["Anchor"];
export type Statement = Schemas["Statement"];
export type Translation = Schemas["TranslationView"];
export type ModelsOffered = Schemas["ModelsView"];
export type TranslatedBlock = Schemas["TranslatedBlock"];
export type Segment = Schemas["Segment"];
export type PageBlocks = Schemas["PageBlocks"];
export type Conversation = Schemas["Conversation"];
export type Turn = Schemas["Turn"];
export type Passage = Schemas["Passage"];
export type Standing = Schemas["Found"] | Schemas["Lost"];
export type Revision = Schemas["Revision"];
export type ItemRevisions = Schemas["ItemRevisions"];
export type Highlight = Schemas["HighlightView"];
export type ItemHighlights = Schemas["ItemHighlights"];
export type NoteEntry = Schemas["NoteEntry"];
export type Note = Schemas["NoteView"];
export type NoteDraft = Schemas["NoteDraft"];
export type LinkedPassage = Schemas["LinkedPassage"];
export type Subscription = Schemas["SubscriptionView"];
export type Follows = Schemas["Follows"];
export type Poll = Subscription["poll"];
export type Posting = Schemas["PostingView"];
export type Admission = Posting["admission"];
// The anchors of a passage: one at least, of one document, in the order of its blocks.
export type Anchors = [Anchor, ...Anchor[]];
// What happens while a question is answered. The protocol lists these as the items of a
// stream, which the generated types leave out, so the kinds are named here.
export type ChatEvent =
  | Schemas["Drafted"]
  | Schemas["Searched"]
  | Schemas["Answered"]
  | Schemas["Unanswered"];
type Refusal = Schemas["Failure"] | Schemas["HTTPValidationError"];

const SOURCE_PATH = "/api/items/{item_id}/source" satisfies keyof paths;
const PAGE_PATH = "/api/items/{item_id}/page" satisfies keyof paths;
const PICTURE_PATH = "/api/documents/{document_id}/pictures/{name}" satisfies keyof paths;
const ITEM_PLACE = "{item_id}";
const DOCUMENT_PLACE = "{document_id}";
const NAME_PLACE = "{name}";

const api = createClient<paths>();

// Where the file an item's document was read from is served: the PDF, or the recording.
export function sourceLink(itemId: string): string {
  return SOURCE_PATH.replace(ITEM_PLACE, itemId);
}

// Where the web page an item was read from is served, inert, for a frame to show.
export function pageLink(itemId: string): string {
  return PAGE_PATH.replace(ITEM_PLACE, itemId);
}

// Where the picture the library keeps of a figure of a document is served, by its name.
export function pictureLink(documentId: string, name: string): string {
  return PICTURE_PATH.replace(DOCUMENT_PLACE, documentId).replace(NAME_PLACE, name);
}

// How the target of a link in a note's Markdown begins when it leads to one of the note's
// passages; the passage's id follows.
export const PASSAGE_LINK = "passage:";

// The anchors of a passage the service gave, which holds one at least.
export function anchorsOf(passage: Passage): Anchors {
  const [first, ...rest] = passage.anchors;
  if (first === undefined) {
    throw new Error("这段文字没有位置");
  }
  return [first, ...rest];
}

function refusalText(refusal: Refusal | undefined): string {
  const detail = refusal?.detail;
  if (typeof detail === "string") {
    return detail;
  }
  return detail?.map((problem) => problem.msg).join("；") ?? "请求没有成功";
}

// The service answered a request with a status other than success, so it did nothing of what
// was asked. The status tells the kinds of refusal apart where a reader is told them apart.
export class Refused extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function settled<Data>(
  request: Promise<{ data?: Data; error?: Refusal; response: Response }>,
): Promise<Data> {
  const { data, error, response } = await request;
  if (data === undefined) {
    throw new Refused(refusalText(error), response.status);
  }
  return data;
}

// The statuses a gateway answers with when the service behind it does not answer. The service
// itself answers 502 only where a model call failed, which a request that calls no model never
// meets.
const UNANSWERED_STATUSES: ReadonlySet<number> = new Set([502, 503, 504]);
const UNANSWERED = "连不上服务";

// Why a request that calls no model failed, as the reader is told it: a service that did not
// answer is said as such rather than by the gateway's bare refusal.
export function requestReason(error: unknown): string {
  return error instanceof Refused && UNANSWERED_STATUSES.has(error.status)
    ? UNANSWERED
    : reasonOf(error);
}

// A request that answers with nothing when it succeeds.
async function done(request: Promise<{ error?: Refusal; response: Response }>): Promise<void> {
  const { error, response } = await request;
  if (!response.ok) {
    throw new Refused(refusalText(error), response.status);
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
    throw new Refused(refusalText(error), response.status);
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

// The model an operation asks, as the reader chose it; none, the service's default.
function chosen(model: string | undefined) {
  return model === undefined ? {} : { model };
}

export function readModels(): Promise<ModelsOffered> {
  return settled(api.GET("/api/models"));
}

// The styles a digest may be written in, in the order they are offered.
export function readDigestStyles(): Promise<StyleOffer[]> {
  return settled(api.GET("/api/digest-styles"));
}

// The digests kept of the document the item holds now, one of each style written; nothing is
// written.
export function readDigests(itemId: string): Promise<Digest[]> {
  return settled(
    api.GET("/api/items/{item_id}/digests", { params: { path: { item_id: itemId } } }),
  );
}

// A writing of a digest that failed: the style it asked, with the instructions of a custom one,
// and why it failed.
export type FailedWriting = { state: "failed"; asked: ChosenStyle; reason: string };
// What a writing of a digest comes to: the digest, written and kept, or a failure.
export type WritingEnd = { state: "written"; digest: Digest } | FailedWriting;
type DigestUnderWay = { state: "writing"; end: Promise<WritingEnd> };
// A digest asked for that the service has not kept: its writing under way, or the last one,
// which failed.
export type DigestWriting = DigestUnderWay | FailedWriting;

// Writing a digest takes a model call, so views that ask for the same item's digest of the same
// style while one request is under way share that request. A writing that fails stays, with
// why, until the item's digest of that style is asked for again, so that a view that opens
// later shows it. The writings are kept by item, then by style.
const writingsByItem = new Map<string, Map<DigestStyle, DigestWriting>>();

// The item's digests asked for and not kept, by style: each one's writing under way, or the last
// one, which failed.
export function digestWritings(itemId: string): ReadonlyMap<DigestStyle, DigestWriting> {
  return new Map(writingsByItem.get(itemId));
}

// Writes the item's digest of the style chosen with the model, in place of the one of that
// style kept before.
export function writeDigest(
  itemId: string,
  style: ChosenStyle,
  model: string | undefined,
): DigestUnderWay {
  const writings = writingsByItem.get(itemId) ?? new Map<DigestStyle, DigestWriting>();
  writingsByItem.set(itemId, writings);
  const before = writings.get(style.style);
  if (before?.state === "writing") {
    return before;
  }
  const end = settled(
    api.POST("/api/items/{item_id}/digest", {
      params: { path: { item_id: itemId }, query: chosen(model) },
      body: style,
    }),
  ).then(
    (digest): WritingEnd => ({ state: "written", digest }),
    (error: unknown): WritingEnd => ({ state: "failed", asked: style, reason: reasonOf(error) }),
  );
  end.then((ended) => {
    if (ended.state === "written") {
      writings.delete(style.style);
    } else {
      writings.set(style.style, ended);
    }
  });
  const writing: DigestUnderWay = { state: "writing", end };
  writings.set(style.style, writing);
  return writing;
}

export function readTranslation(itemId: string): Promise<Translation> {
  return settled(
    api.GET("/api/items/{item_id}/translation", { params: { path: { item_id: itemId } } }),
  );
}

// Starts the translation with the model unless it is under way, done or unneeded; one that
// stopped goes on with it.
export function ensureTranslation(itemId: string, model: string | undefined): Promise<Translation> {
  return settled(
    api.PUT("/api/items/{item_id}/translation", {
      params: { path: { item_id: itemId }, query: chosen(model) },
    }),
  );
}

export function translateAnew(itemId: string, model: string | undefined): Promise<Translation> {
  return settled(
    api.POST("/api/items/{item_id}/translation", {
      params: { path: { item_id: itemId }, query: chosen(model) },
    }),
  );
}

export function retranslateBlock(
  itemId: string,
  blockNumber: number,
  model: string | undefined,
): Promise<TranslatedBlock> {
  return settled(
    api.POST("/api/items/{item_id}/translation/blocks/{block_number}", {
      params: { path: { item_id: itemId, block_number: blockNumber }, query: chosen(model) },
    }),
  );
}

export function readPageBlocks(itemId: string): Promise<PageBlocks> {
  return settled(
    api.GET("/api/items/{item_id}/page/blocks", { params: { path: { item_id: itemId } } }),
  );
}

export function readItemConversation(itemId: string): Promise<Conversation> {
  return settled(
    api.GET("/api/items/{item_id}/conversation", { params: { path: { item_id: itemId } } }),
  );
}

export function askAboutItem(
  itemId: string,
  text: string,
  model: string | undefined,
): AsyncGenerator<ChatEvent> {
  return eventLines(
    api.POST("/api/items/{item_id}/conversation", {
      params: { path: { item_id: itemId }, query: chosen(model) },
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

export function askAboutLibrary(text: string, model: string | undefined): AsyncGenerator<ChatEvent> {
  return eventLines(
    api.POST("/api/conversation", {
      params: { query: chosen(model) },
      body: { text },
      parseAs: "stream",
    }),
  );
}

export function forgetLibraryConversation(): Promise<void> {
  return done(api.DELETE("/api/conversation"));
}

// Starts a new reading of the item's source; the document it holds stays until that has given
// another.
export function readAgain(itemId: string): Promise<ItemEntry> {
  return settled(
    api.POST("/api/items/{item_id}/reading", { params: { path: { item_id: itemId } } }),
  );
}

export function readRevisions(itemId: string): Promise<ItemRevisions> {
  return settled(
    api.GET("/api/items/{item_id}/revisions", { params: { path: { item_id: itemId } } }),
  );
}

export function readRevisionDocument(itemId: string, revision: number): Promise<ItemDocument> {
  return settled(
    api.GET("/api/items/{item_id}/revisions/{number}/document", {
      params: { path: { item_id: itemId, number: revision } },
    }),
  );
}

// Where a passage of any revision of the item stands in the document the item holds now.
export function readStanding(itemId: string, passage: Passage): Promise<Standing> {
  return settled(
    api.POST("/api/items/{item_id}/standing", {
      params: { path: { item_id: itemId } },
      body: passage,
    }),
  );
}

export function readHighlights(itemId: string): Promise<Highlight[]> {
  return settled(
    api.GET("/api/items/{item_id}/highlights", { params: { path: { item_id: itemId } } }),
  );
}

export function makeHighlight(
  itemId: string,
  passage: Passage,
  comment: string,
): Promise<Highlight> {
  return settled(
    api.POST("/api/items/{item_id}/highlights", {
      params: { path: { item_id: itemId } },
      body: { passage, comment },
    }),
  );
}

export function commentHighlight(
  itemId: string,
  highlightId: string,
  text: string,
): Promise<Highlight> {
  return settled(
    api.PUT("/api/items/{item_id}/highlights/{highlight_id}/comment", {
      params: { path: { item_id: itemId, highlight_id: highlightId } },
      body: { text },
    }),
  );
}

export function removeHighlight(itemId: string, highlightId: string): Promise<void> {
  return done(
    api.DELETE("/api/items/{item_id}/highlights/{highlight_id}", {
      params: { path: { item_id: itemId, highlight_id: highlightId } },
    }),
  );
}

// Every item that has highlights, with them.
export function readLibraryHighlights(): Promise<ItemHighlights[]> {
  return settled(api.GET("/api/highlights"));
}

// The requests about notes tell a request that reached no service from a refusal, as a reader
// is told the two apart: the text is kept once the service is back, and not after it refused.
export function listNotes(): Promise<NoteEntry[]> {
  return settled(reached(api.GET("/api/notes")));
}

// Keeps a new note. One made as the page is left is sent so that the request outlives the page,
// as rewriteNote's is.
export function writeNote(draft: NoteDraft, leaving = false): Promise<Note> {
  return settled(reached(api.POST("/api/notes", { body: draft, keepalive: leaving })));
}

export function readNote(noteId: string): Promise<Note> {
  return settled(
    reached(api.GET("/api/notes/{note_id}", { params: { path: { note_id: noteId } } })),
  );
}

// Puts the draft in place of what the note held, unless the note holds an edition as late as
// the draft's. A draft sent as the page is left is sent so that the request outlives the page;
// the browser refuses such a request above 64 KiB.
export function rewriteNote(noteId: string, draft: NoteDraft, leaving = false): Promise<Note> {
  return settled(
    reached(
      api.PUT("/api/notes/{note_id}", {
        params: { path: { note_id: noteId } },
        body: draft,
        keepalive: leaving,
      }),
    ),
  );
}

export function removeNote(noteId: string): Promise<void> {
  return done(
    reached(api.DELETE("/api/notes/{note_id}", { params: { path: { note_id: noteId } } })),
  );
}

// The reader has the item's document on show; the first time is kept as when it was seen.
export function seeItem(itemId: string): Promise<ItemEntry> {
  return settled(api.PUT("/api/items/{item_id}/seen", { params: { path: { item_id: itemId } } }));
}

// The subscriptions, the one added last first.
export function listSubscriptions(): Promise<Subscription[]> {
  return settled(api.GET("/api/subscriptions"));
}

// Subscribes to the feed the address leads to and admits as many of its newest postings as
// given. An address that leads to no feed that can be read is refused with the reason (400).
export function subscribe(address: string, admit: number): Promise<Subscription> {
  return settled(api.POST("/api/subscriptions", { body: { address, admit } }));
}

export function readSubscription(subscriptionId: string): Promise<Subscription> {
  return settled(
    api.GET("/api/subscriptions/{subscription_id}", {
      params: { path: { subscription_id: subscriptionId } },
    }),
  );
}

// Sets how the subscription follows its feed from now on.
export function chooseFollows(subscriptionId: string, follows: Follows): Promise<Subscription> {
  return settled(
    api.PUT("/api/subscriptions/{subscription_id}/follows", {
      params: { path: { subscription_id: subscriptionId } },
      body: { follows },
    }),
  );
}

// Starts a poll of the subscription's feed now; its end is read from the subscription.
export function pollSubscription(subscriptionId: string): Promise<Subscription> {
  return settled(
    api.POST("/api/subscriptions/{subscription_id}/poll", {
      params: { path: { subscription_id: subscriptionId } },
    }),
  );
}

// Takes every item of the subscription's postings that is read and not seen for seen now.
export function seeSubscription(subscriptionId: string): Promise<Subscription> {
  return settled(
    api.PUT("/api/subscriptions/{subscription_id}/seen", {
      params: { path: { subscription_id: subscriptionId } },
    }),
  );
}

// Removes the subscription and the list of its postings; the items it brought stay.
export function unsubscribe(subscriptionId: string): Promise<void> {
  return done(
    api.DELETE("/api/subscriptions/{subscription_id}", {
      params: { path: { subscription_id: subscriptionId } },
    }),
  );
}

// The postings the subscription lists, the newest first: those older than the posting of the
// number given, or the newest of all, as many as the limit at most.
export function listPostings(
  subscriptionId: string,
  before: number | undefined,
  limit: number,
): Promise<Posting[]> {
  return settled(
    api.GET("/api/subscriptions/{subscription_id}/postings", {
      params: {
        path: { subscription_id: subscriptionId },
        query: before === undefined ? { limit } : { before, limit },
      },
    }),
  );
}

// Has the posting read at the reader's word: admitted now where it is not, read again where
// its item's reading failed.
export function readPosting(subscriptionId: string, number: number): Promise<Posting> {
  return settled(
    api.POST("/api/subscriptions/{subscription_id}/postings/{number}/reading", {
      params: { path: { subscription_id: subscriptionId, number } },
    }),
  );
}
