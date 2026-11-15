import createClient from "openapi-fetch";
import type { components, paths } from "./protocol";

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
