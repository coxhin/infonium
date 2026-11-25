import type { Flow } from "./flows";

// The lengths a flow is drawn and measured with, as its page's CSS is given them: parts of the
// page's width, in units of the page's own width.

// A length along a flow's run as a part of the page's width, which the text's sizes are
// given in so that the page scales without being set again.
export function acrossWidth(flow: Flow, length: number, shape: number): number {
  return flow.vertical ? length : length * shape;
}

export function cqw(part: number): string {
  return `${part * 100}cqw`;
}
