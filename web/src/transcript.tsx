import { memo, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { MarkedText } from "./blocks";
import { unitOffset, unitOffsets } from "./blocktext";
import type { Anchor, Block, Segment, TimePlace, TranslatedBlock } from "./client";
import type { Clock } from "./player";
import { inView, scrollerOf } from "./scrolling";
import type { Setting } from "./setting";
import {
  AgainControl,
  MARK_ATTRIBUTE,
  overlaps,
  POINTED_CLASS,
  type TextRange,
} from "./translated";
import type { Again } from "./translation";
import "./transcript.css";

// The element that holds the lines of one block carries the block's number in this
// attribute; it holds the block's words and their translation, so it is never the element
// a source block's number is carried by.
export const LINES_ATTRIBUTE = "data-lines";
const LINE_ATTRIBUTE = "data-line";
// A line carries the range of its block's text it stands for, in code points, and holds the
// words said there and their translation in elements of their own.
export const LINE_START_ATTRIBUTE = "data-start";
export const LINE_END_ATTRIBUTE = "data-end";
export const SAID_CLASS = "said";
export const SPOKEN_CLASS = "spoken-translation";
const SECONDS_IN_MINUTE = 60;
// What a reader does that scrolls the transcript's scroller: a wheel or a finger over it, a
// key that scrolls, or a press on its scroll bar. A scroll that follows one of these is the
// reader's; any other, as when a citation is pressed, is the reader's code bringing a passage
// or the line being spoken into view.
const SCROLLING_INPUTS = ["wheel", "touchmove", "keydown", "pointerdown"] as const;
const SCROLLING_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "]);
// A key pressed in one of these does its own work there and scrolls nothing.
const KEY_TAKERS = "input, textarea, select, button, a, audio, [contenteditable]";

function scrollsTheReader(event: Event, scroller: Element): boolean {
  const target = event.target;
  if (!(target instanceof Element)) {
    return false;
  }
  const page = scroller === document.scrollingElement;
  if (event instanceof PointerEvent) {
    // A press scrolls only on the scroll bar, which belongs to the scroller itself.
    return target === scroller || (page && target === document.documentElement);
  }
  if (event instanceof KeyboardEvent) {
    return SCROLLING_KEYS.has(event.key) && target.closest(KEY_TAKERS) === null;
  }
  const own = scrollerOf(target) ?? document.scrollingElement;
  return page ? own === scroller : scroller.contains(target) && own === scroller;
}
// How long without a scroll event counts as the end of a scroll, where the browser does not
// say when a scroll ends.
const SCROLL_SETTLES_MS = 150;
const SPOKEN_LINE = `.line[aria-current="true"]`;

// One line of a transcript: a range of a block's text, the words spoken there, their
// translation once there is one, and the stretch it is played from.
type Line = {
  start: number;
  end: number;
  words: string;
  translation: Segment | undefined;
  place: TimePlace | undefined;
};

// A line starts at the earliest of the block's stretches that overlap it.
function placeOf(places: TimePlace[], range: TextRange): TimePlace | undefined {
  return places
    .filter((place) => overlaps(place, range))
    .reduce<TimePlace | undefined>(
      (earliest, place) =>
        earliest === undefined || place.moment < earliest.moment ? place : earliest,
      undefined,
    );
}

// A translated block is a line a segment. One not translated yet is a line a stretch of its
// recording, so that its words follow the playing as well.
function linesOf(block: Block, translated: TranslatedBlock | undefined): Line[] {
  const places = block.places.filter((place) => place.kind === "time");
  const offsets = unitOffsets(block.text);
  const words = (start: number, end: number) =>
    block.text.slice(unitOffset(offsets, start), unitOffset(offsets, end));
  const length = offsets.length - 1;
  const ranges =
    translated?.segments.map((segment) => ({ ...segment, translation: segment })) ??
    (places.length > 0 ? places : [{ start: 0, end: length }]).map((place) => ({
      start: place.start,
      end: place.end,
      translation: undefined,
    }));
  return ranges.map(({ start, end, translation }) => ({
    start,
    end,
    words: words(start, end),
    translation,
    place: placeOf(places, { start, end }),
  }));
}

// The lines of a block are worked out once for each translation of it, and for its source.
const linesKept = new WeakMap<Block | TranslatedBlock, Line[]>();

function keptLines(block: Block, translated: TranslatedBlock | undefined): Line[] {
  const key = translated ?? block;
  const kept = linesKept.get(key);
  if (kept !== undefined) {
    return kept;
  }
  const lines = linesOf(block, translated);
  linesKept.set(key, lines);
  return lines;
}

function clockTime(seconds: number): string {
  const whole = Math.floor(seconds);
  const minutes = Math.floor(whole / SECONDS_IN_MINUTE);
  return `${minutes}:${String(whole % SECONDS_IN_MINUTE).padStart(2, "0")}`;
}

// The line being spoken: the last whose stretch has begun. The moments are those of the lines
// in their order, and a recording is transcribed in the order it is spoken.
function spokenLine(moments: (number | undefined)[], clock: Clock): number | undefined {
  if (!clock.started) {
    return undefined;
  }
  let spoken: number | undefined;
  moments.forEach((moment, index) => {
    if (moment !== undefined && moment <= clock.moment) {
      spoken = index;
    }
  });
  return spoken;
}

const BlockLines = memo(function BlockLines({
  number,
  lines,
  first,
  spoken,
  setting,
  translated,
  missing,
  again,
  pointed,
  onAgain,
  onPlay,
}: {
  number: number;
  lines: Line[];
  first: number;
  spoken: number | undefined;
  setting: Setting;
  translated: boolean;
  // Left in its source by a translation that stopped, though it is one to translate.
  missing: boolean;
  again: Again | undefined;
  pointed: Anchor | undefined;
  onAgain: (blockNumber: number) => void;
  onPlay: (place: TimePlace) => void;
}) {
  // A click that ends a selection of words is no request to play.
  function play(place: TimePlace | undefined) {
    if (place !== undefined && getSelection()?.isCollapsed !== false) {
      onPlay(place);
    }
  }
  return (
    <div className="line-group" {...{ [LINES_ATTRIBUTE]: number }}>
      {lines.map((line, index) => {
        const classes = ["line"];
        if (pointed !== undefined && overlaps(line, pointed)) {
          classes.push(POINTED_CLASS);
        }
        const showsWords = setting === "both" || line.translation === undefined;
        return (
          <div
            key={index}
            className={classes.join(" ")}
            aria-current={spoken === index ? "true" : undefined}
            {...{
              [LINE_ATTRIBUTE]: first + index,
              [LINE_START_ATTRIBUTE]: line.start,
              [LINE_END_ATTRIBUTE]: line.end,
            }}
          >
            <button
              className="moment"
              disabled={line.place === undefined}
              aria-label={line.place && `从 ${clockTime(line.place.moment)} 播放`}
              onClick={() => line.place && onPlay(line.place)}
              // The time is drawn by the stylesheet, so that a selection copies none of it.
              {...{ [MARK_ATTRIBUTE]: line.place ? clockTime(line.place.moment) : "" }}
            />
            <div className="words" onClick={() => play(line.place)}>
              {showsWords && <p className={SAID_CLASS}>{line.words}</p>}
              {line.translation !== undefined && (
                <p className={SPOKEN_CLASS}>
                  <MarkedText text={line.translation.text} marks={line.translation.marks} />
                </p>
              )}
            </div>
          </div>
        );
      })}
      {(translated || missing) && (
        <AgainControl number={number} again={again} onAgain={onAgain} missing={missing} />
      )}
    </div>
  );
});

// A recording's transcript as lines that follow the playing, like the lyrics of a song: the
// words spoken, their translation under them, or the translation alone. The line being spoken
// is marked and kept in view until the reader scrolls away; it is followed again when the
// reader brings it back into view, plays from a line, or asks for it.
export function Transcript({
  documentId,
  blocks,
  translations,
  setting,
  again,
  pointed,
  played,
  leftOut,
  clock,
  onAgain,
  onPlay,
}: {
  documentId: string;
  blocks: Block[];
  translations: ReadonlyMap<number, TranslatedBlock>;
  setting: Setting;
  again: ReadonlyMap<number, Again>;
  // The anchors of the passage pointed at, by the number of the block each lies in.
  pointed: ReadonlyMap<number, Anchor>;
  played: TimePlace[];
  // The blocks a translation that stopped has left in their source.
  leftOut: ReadonlySet<number>;
  clock: Clock;
  onAgain: (blockNumber: number) => void;
  onPlay: (place: TimePlace) => void;
}) {
  const transcript = useRef<HTMLDivElement>(null);
  const groups = useMemo(() => {
    let first = 0;
    return blocks.map((block, number) => {
      const lines = keptLines(block, translations.get(number));
      const group = { number, lines, first };
      first += lines.length;
      return group;
    });
  }, [blocks, translations]);
  const moments = useMemo(
    () => groups.flatMap((group) => group.lines.map((line) => line.place?.moment)),
    [groups],
  );
  const spoken = useSyncExternalStore(clock.subscribe, () => spokenLine(moments, clock));
  const [following, setFollowing] = useState(true);

  const spokenElement = (line: number | undefined) =>
    line === undefined
      ? null
      : (transcript.current?.querySelector(`[${LINE_ATTRIBUTE}="${line}"]`) ?? null);

  // A scroll that the reader makes decides, once it has ended, whether the transcript follows
  // the playing: it does when the line being spoken is still, or again, in view. A scroll has
  // ended when the browser says so, or, where a browser never says so, once no scroll has
  // come for a short while.
  useEffect(() => {
    const own = transcript.current;
    if (own === null) {
      return;
    }
    const scroller = scrollerOf(own) ?? document.scrollingElement ?? document.documentElement;
    // The page's scroll is told at the document, any other at the element that scrolls.
    const ofScroller = (event: Event) =>
      event.target === scroller ||
      (scroller === document.scrollingElement && event.target === document);
    let byReader = false;
    let settling: ReturnType<typeof setTimeout> | undefined;
    const readerScrolls = (event: Event) => {
      if (scrollsTheReader(event, scroller)) {
        byReader = true;
      }
    };
    const ended = () => {
      clearTimeout(settling);
      if (byReader) {
        byReader = false;
        const element = transcript.current?.querySelector(SPOKEN_LINE) ?? null;
        if (element !== null) {
          setFollowing(inView(element));
        }
      }
    };
    const scrolled = (event: Event) => {
      if (ofScroller(event)) {
        ended();
      }
    };
    const scrolling = (event: Event) => {
      if (ofScroller(event)) {
        clearTimeout(settling);
        settling = setTimeout(ended, SCROLL_SETTLES_MS);
      }
    };
    SCROLLING_INPUTS.forEach((input) => addEventListener(input, readerScrolls, true));
    addEventListener("scroll", scrolling, { capture: true, passive: true });
    addEventListener("scrollend", scrolled, true);
    return () => {
      clearTimeout(settling);
      SCROLLING_INPUTS.forEach((input) => removeEventListener(input, readerScrolls, true));
      removeEventListener("scroll", scrolling, { capture: true });
      removeEventListener("scrollend", scrolled, true);
    };
  }, []);

  useEffect(() => setFollowing(true), [played]);

  useEffect(() => {
    if (following) {
      spokenElement(spoken)?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [spoken, following]);

  return (
    <div className="transcript" ref={transcript}>
      {groups.map(({ number, lines, first }) => {
        const here = spoken !== undefined && spoken >= first && spoken < first + lines.length;
        const pointedHere = pointed.get(number);
        return (
          <BlockLines
            key={number}
            number={number}
            lines={lines}
            first={first}
            spoken={here ? spoken - first : undefined}
            setting={setting}
            translated={translations.has(number)}
            missing={leftOut.has(number)}
            again={again.get(number)}
            pointed={pointedHere?.document_id === documentId ? pointedHere : undefined}
            onAgain={onAgain}
            onPlay={onPlay}
          />
        );
      })}
      {!following && spoken !== undefined && (
        <button className="follow" onClick={() => setFollowing(true)}>
          回到正在播放的一句
        </button>
      )}
    </div>
  );
}
