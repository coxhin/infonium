import type { Revision } from "./client";

// How a revision is called wherever the app names one: by its number and the day and time it
// was read, in the reader's own time zone.
const WHEN_READ = new Intl.DateTimeFormat("zh-CN", { dateStyle: "long", timeStyle: "short" });

export function whenRead(revision: Revision): string {
  return WHEN_READ.format(new Date(revision.read));
}

export function revisionName(revision: Revision): string {
  return `第 ${revision.number} 次读取`;
}

// A revision named with when it was read, as a sentence says it.
export function revisionTold(revision: Revision): string {
  return `${revisionName(revision)}（${whenRead(revision)}）`;
}
