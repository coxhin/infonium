import { useEffect, useState } from "react";

// Times as a reader says them: by minutes and hours near the present, by the day and the time of
// day further off; and the exact time, for the pointer to find on the words.

const MINUTE_MS = 60_000;
const DAY_MS = 24 * 60 * MINUTE_MS;
// Within this many minutes of the present, a time is told by how many minutes ago or ahead it
// is; within this many hours before the present, on the same day, by how many hours ago.
const MINUTES_TOLD = 60;
const HOURS_TOLD = 6;
// How often the words of a time are told again, as the present moves on.
const RETELL_MS = 30_000;

const CLOCK = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});
const DAY_OF_THIS_YEAR = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric" });
const DAY = new Intl.DateTimeFormat("zh-CN", { dateStyle: "long" });
const EXACT = new Intl.DateTimeFormat("zh-CN", { dateStyle: "long", timeStyle: "medium" });
// The days next to today, by how far they lie from it.
const NEAR_DAYS: ReadonlyMap<number, string> = new Map([
  [-2, "前天"],
  [-1, "昨天"],
  [0, "今天"],
  [1, "明天"],
  [2, "后天"],
]);

// The number of the day a moment falls on, where the reader is.
function dayNumber(date: Date): number {
  return Math.floor((date.getTime() - date.getTimezoneOffset() * MINUTE_MS) / DAY_MS);
}

function dayText(date: Date, now: Date): string {
  return date.getFullYear() === now.getFullYear()
    ? DAY_OF_THIS_YEAR.format(date)
    : DAY.format(date);
}

// How long ago or how soon, where that is less than an hour or a few hours ago; otherwise
// nothing.
function apartText(date: Date, now: Date): string | undefined {
  const apart = date.getTime() - now.getTime();
  const minutes = Math.round(Math.abs(apart) / MINUTE_MS);
  if (minutes < 1) {
    return apart < 0 ? "刚刚" : "马上";
  }
  if (minutes < MINUTES_TOLD) {
    return apart < 0 ? `${minutes} 分钟前` : `${minutes} 分钟后`;
  }
  const hours = Math.floor(minutes / MINUTES_TOLD);
  return apart < 0 && hours <= HOURS_TOLD && dayNumber(date) === dayNumber(now)
    ? `${hours} 小时前`
    : undefined;
}

// A moment with its time of day: 10 分钟前, 3 小时前, 明天 08:30, 11月20日 14:05.
function toldMoment(date: Date, now: Date): string {
  const apart = apartText(date, now);
  if (apart !== undefined) {
    return apart;
  }
  const near = NEAR_DAYS.get(dayNumber(date) - dayNumber(now));
  return `${near ?? dayText(date, now)} ${CLOCK.format(date)}`;
}

// A moment that has come, as when a poll was made. The service's clock and the browser's may
// differ by a little, so a moment the service says has come is never told as ahead.
export function toldPast(at: string, now: Date): string {
  return toldMoment(new Date(Math.min(Date.parse(at), now.getTime())), now);
}

// A moment to come, as when a poll is due; one that is overdue is told as coming now.
export function toldAhead(at: string, now: Date): string {
  return toldMoment(new Date(Math.max(Date.parse(at), now.getTime())), now);
}

// The day a thing came out, with its time of day only while that is recent: 3 小时前,
// 昨天 14:05, 11月20日, 2025年3月1日.
export function toldDay(at: string, now: Date): string {
  const date = new Date(Math.min(Date.parse(at), now.getTime()));
  const apart = apartText(date, now);
  if (apart !== undefined) {
    return apart;
  }
  const near = NEAR_DAYS.get(dayNumber(date) - dayNumber(now));
  return near === undefined ? dayText(date, now) : `${near} ${CLOCK.format(date)}`;
}

export function exactTime(at: string): string {
  return EXACT.format(new Date(at));
}

// The present, as the component is drawn; it is drawn again every half minute at least, so that
// what is told of a time stays true.
export function useNow(): Date {
  const [, setTold] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTold((before) => before + 1), RETELL_MS);
    return () => clearInterval(timer);
  }, []);
  return new Date();
}

// A time as a reader says it, with the exact time on hover.
export function Moment({ at, told }: { at: string; told: string }) {
  return (
    <time dateTime={at} title={exactTime(at)}>
      {told}
    </time>
  );
}
