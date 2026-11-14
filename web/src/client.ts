import createClient from "openapi-fetch";
import type { components, paths } from "./protocol";

type Schemas = components["schemas"];
export type ItemEntry = Schemas["ItemEntry"];
export type ItemReading = Schemas["ItemReading"];
export type Block = Schemas["Document"]["blocks"][number];
export type Mark = Schemas["Paragraph"]["marks"][number];
export type Style = Schemas["Style"];
export type Digest = Schemas["Digest"];
export type Anchor = Schemas["Anchor"];

const api = createClient<paths>();

function failureText(error: unknown): string {
  const detail = (error as { detail?: unknown } | undefined)?.detail;
  return typeof detail === "string" ? detail : "请求没有成功";
}

async function settled<Data>(request: Promise<{ data?: Data; error?: unknown }>): Promise<Data> {
  const { data, error } = await request;
  if (data === undefined) {
    throw new Error(failureText(error));
  }
  return data;
}

export function listedItems(): Promise<ItemEntry[]> {
  return settled(api.GET("/api/items"));
}

export function importedPage(url: string): Promise<ItemEntry> {
  return settled(api.POST("/api/items", { body: { url } }));
}

export function itemReading(itemId: string): Promise<ItemReading> {
  return settled(api.GET("/api/items/{item_id}", { params: { path: { item_id: itemId } } }));
}

export function rewrittenDigest(itemId: string): Promise<Digest> {
  return settled(
    api.POST("/api/items/{item_id}/digest", { params: { path: { item_id: itemId } } }),
  );
}

// Writing a digest takes a model call, so views that ask for the same item's digest while one
// request is under way share that request.
const digestsUnderWay = new Map<string, Promise<Digest>>();

export function ensuredDigest(itemId: string): Promise<Digest> {
  const underWay =
    digestsUnderWay.get(itemId) ??
    settled(
      api.PUT("/api/items/{item_id}/digest", { params: { path: { item_id: itemId } } }),
    ).finally(() => digestsUnderWay.delete(itemId));
  digestsUnderWay.set(itemId, underWay);
  return underWay;
}
