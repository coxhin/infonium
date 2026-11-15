import type { Anchor, Block, PagePlace, Place, TimePlace } from "./client";

export type Surface = "flow" | Place["kind"];

// The places of one document are all of one kind, and that kind says how its source is shown:
// pages to turn to, a recording to play, or, with no places at all, only the flow of the text.
export function surfaceOf(blocks: Block[]): Surface {
  return blocks.find((block) => block.places.length > 0)?.places[0]?.kind ?? "flow";
}

// An anchor is a range of a block's text; it reaches the source through the places whose
// ranges overlap it.
export function placesOf(blocks: Block[], anchor: Anchor): Place[] {
  const places = blocks[anchor.block_number]?.places ?? [];
  return places.filter((place) => place.start < anchor.end && anchor.start < place.end);
}

export function pagePlaces(places: Place[]): PagePlace[] {
  return places.filter((place) => place.kind === "page");
}

export function timePlaces(places: Place[]): TimePlace[] {
  return places.filter((place) => place.kind === "time");
}
