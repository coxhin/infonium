import { isBody, type AreaText } from "./areas";
import type { Flow } from "./flows";

// How the flows of a page are fitted: the page takes one leading, the widest at which the flows
// that fit hold nine tenths of the letters of those that fit at the narrowest; a flow that fits
// at the narrowest but not there takes the widest at which it fits; a flow that does not fit
// at the narrowest takes blank paper beyond its foot, then is set smaller in steps, then is
// cut; and the room a flow leaves is spread between its blocks. How far a flow's text reaches
// it is told by the measure it is given.

// The leadings a page may take, from the narrowest: a floor, a cap, and every multiple of 0.05
// between. A script written without spaces between words wants more room between its lines
// than the source's Latin, up to the cap; any other is set no more loosely than the source.
const LEADING_STEP = 0.05;
const UNSPACED_LEAST = 1.25;
const UNSPACED_MOST = 1.6;
const SPACED_LEAST = 1.15;
// Leadings are told apart to two decimals.
const LEADING_DIGITS = 100;

export function leadingsOf(spacing: number, unspaced: boolean): number[] {
  const floor = Math.min(unspaced ? UNSPACED_LEAST : SPACED_LEAST, spacing);
  const cap = unspaced ? Math.max(UNSPACED_MOST, spacing) : spacing;
  const between: number[] = [];
  for (
    let multiple = Math.floor(floor / LEADING_STEP + 1);
    multiple * LEADING_STEP < cap - 1 / LEADING_DIGITS / 2;
    multiple += 1
  ) {
    between.push(Math.round(multiple * LEADING_STEP * LEADING_DIGITS) / LEADING_DIGITS);
  }
  return [
    ...new Set([floor, ...between.filter((leading) => leading > floor), cap]),
  ];
}

// Body text takes a flow's leading; text of another print, as a heading, is led in the same
// proportion to its own leading in the source.
export function leadingRatioOf(text: AreaText, spacing: number): number {
  return isBody(text.kind) ? 1 : text.area.spacing / spacing;
}

// What a flow's text is set at: its body's leading and the part of its print's size.
export type Trial = { leading: number; step: number };

// How a flow is set: at a leading and a step, its paper going so far beyond its foot over the
// blank paper there, cut at its foot or not, and with a share of its spare room added to each
// gap for each share the gap takes.
export type Setting = Trial & { growth: number; cut: boolean; share: number };

// The page's leading and how each of its flows, by key, is set.
export type Fitting = { leading: number; settings: Map<string, Setting> };

// How far each flow's text reaches at a trial, from the top of its room along the run of its
// lines, with no spare room added: the measure is the page's, which lays the text out.
export type Measure = (trials: ReadonlyMap<Flow, Trial>) => ReadonlyMap<Flow, number>;

// The parts of its print's size a flow that does not fit is set at, in order.
const STEPS = [1, 0.95, 0.9, 0.85, 0.8];
// A flow fits whose text ends no lower than this part of the body's size beyond its foot, as
// far as the blank paper there allows.
const FIT_SLACK = 0.25;
// A gap grows by no more than this part of a line of the body's pitch at the page's leading
// for each share it takes.
const SHARE_LINES = 0.5;

function extentOf(flow: Flow): number {
  return flow.vertical ? flow.room.width : flow.room.height;
}

// How far beyond its foot a flow may go: by a quarter of the body's size to fit, and by up to
// two lines of the body's pitch where it does not fit otherwise, over blank paper alone.
function slackOf(flow: Flow): number {
  return Math.min(FIT_SLACK * flow.print.size, flow.clear);
}

function fits(reach: number, flow: Flow, beyond: number): boolean {
  return reach <= extentOf(flow) + beyond;
}

// The paper a flow lays beyond its foot to cover its text there.
function growthOf(reach: number, flow: Flow, beyond: number): number {
  return Math.min(beyond, Math.max(0, reach - extentOf(flow)));
}

function trialsOf(flows: Flow[], trial: Trial): Map<Flow, Trial> {
  return new Map(flows.map((flow) => [flow, trial]));
}

// The spare room a flow's text leaves above its foot, spread over its gaps: a share for each
// share a gap takes, no more than half a line of the body's pitch at the page's leading.
function shareOf(flow: Flow, spare: number, leading: number): number {
  const shares = flow.weights.reduce((sum, weight) => sum + weight, 0);
  if (spare <= 0 || shares === 0) {
    return 0;
  }
  return Math.min(spare / shares, SHARE_LINES * flow.print.size * leading);
}

// The page takes the widest leading at which the flows that fit there hold at least this part
// of the letters of all its flows that fit at the narrowest, so that a few tight flows do not
// hold the rest back.
const HELD_LETTERS = 0.9;
const LETTER = /[\p{L}\p{N}]/u;

// A flow's letters: the letters and digits of its areas' translation, each formula counting as
// one.
function lettersOf(flow: Flow): number {
  let count = 0;
  for (const { piece } of flow.texts) {
    const characters = [...piece.text];
    const formulas = piece.marks.filter((mark) => mark.kind === "formula");
    count += formulas.length;
    characters.forEach((character, at) => {
      const inFormula = formulas.some((mark) => mark.start <= at && at < mark.end);
      if (!inFormula && LETTER.test(character)) {
        count += 1;
      }
    });
  }
  return count;
}

// How far the flows' texts reach at the leadings tried, by the leading's place in the list, at
// the full size: measured as they are first asked for, and kept.
type Reaches = {
  tryAt: (places: ReadonlyMap<Flow, number>) => void;
  reachAt: (flow: Flow, index: number) => number;
};

function reachesOf(flows: Flow[], leadings: number[], measure: Measure): Reaches {
  const reached = new Map<Flow, Map<number, number>>(flows.map((flow) => [flow, new Map()]));
  const tryAt = (places: ReadonlyMap<Flow, number>) => {
    const asked = new Map(
      [...places].filter(([flow, index]) => reached.get(flow)?.has(index) !== true),
    );
    if (asked.size === 0) {
      return;
    }
    const trials = new Map(
      [...asked].map(([flow, index]) => [flow, { leading: leadings[index] ?? 0, step: 1 }]),
    );
    measure(trials).forEach((reach, flow) => {
      const index = asked.get(flow);
      if (index !== undefined) {
        reached.get(flow)?.set(index, reach);
      }
    });
  };
  const reachAt = (flow: Flow, index: number) => reached.get(flow)?.get(index) ?? Infinity;
  return { tryAt, reachAt };
}

function fitsAt(reaches: Reaches, flow: Flow, index: number): boolean {
  return fits(reaches.reachAt(flow, index), flow, slackOf(flow));
}

// The place of the page's leading: the widest at which the flows that fit there, of those that
// fit at the narrowest, hold nine tenths of their letters, found by halving.
function pageLeadingOf(fitting: Flow[], leadings: number[], reaches: Reaches): number {
  const letters = new Map(fitting.map((flow) => [flow, lettersOf(flow)]));
  const allLetters = fitting.reduce((sum, flow) => sum + (letters.get(flow) ?? 0), 0);
  const holds = (index: number) => {
    reaches.tryAt(new Map(fitting.map((flow) => [flow, index])));
    const held = fitting
      .filter((flow) => fitsAt(reaches, flow, index))
      .reduce((sum, flow) => sum + (letters.get(flow) ?? 0), 0);
    return held >= HELD_LETTERS * allLetters;
  };
  let widest = 0;
  let low = 1;
  let high = fitting.length > 0 ? leadings.length - 1 : 0;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (holds(middle)) {
      widest = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return widest;
}

// The place of each flow's own leading: the page's where it fits there, and where it does not,
// the widest at which it fits, found for all such flows at once by halving.
function ownLeadingsOf(fitting: Flow[], widest: number, reaches: Reaches): Map<Flow, number> {
  const own = new Map<Flow, number>(fitting.map((flow) => [flow, widest]));
  const searched = new Map(
    fitting
      .filter((flow) => !fitsAt(reaches, flow, widest))
      .map((flow) => [flow, { low: 1, high: widest - 1, found: 0 }]),
  );
  while ([...searched.values()].some((range) => range.low <= range.high)) {
    const middles = new Map<Flow, number>();
    searched.forEach((range, flow) => {
      if (range.low <= range.high) {
        middles.set(flow, Math.floor((range.low + range.high) / 2));
      }
    });
    reaches.tryAt(middles);
    middles.forEach((middle, flow) => {
      const range = searched.get(flow);
      if (range === undefined) {
        return;
      }
      if (fitsAt(reaches, flow, middle)) {
        range.found = middle;
        range.low = middle + 1;
      } else {
        range.high = middle - 1;
      }
    });
  }
  searched.forEach((range, flow) => own.set(flow, range.found));
  return own;
}

// How each flow that does not fit at the narrowest leading is set: at that leading, with the
// blank paper beyond its foot that it needs, at the first step of its print's size at which it
// fits, or cut at the last.
function stepsOf(
  flows: Flow[],
  floor: number,
  leading: number,
  reaches: Reaches,
  measure: Measure,
): Map<string, Setting> {
  const settings = new Map<string, Setting>();
  let left = flows;
  for (const step of STEPS) {
    if (left.length === 0) {
      break;
    }
    const tried =
      step === 1
        ? new Map(left.map((flow) => [flow, reaches.reachAt(flow, 0)]))
        : measure(trialsOf(left, { leading: floor, step }));
    left = left.filter((flow) => {
      const reach = tried.get(flow) ?? Infinity;
      if (!fits(reach, flow, flow.clear)) {
        return true;
      }
      const growth = growthOf(reach, flow, flow.clear);
      const share = shareOf(flow, extentOf(flow) + growth - reach, leading);
      settings.set(flow.key, { leading: floor, step, growth, cut: false, share });
      return false;
    });
  }
  const least = STEPS.at(-1) ?? 1;
  for (const flow of left) {
    const growth = flow.clear;
    settings.set(flow.key, { leading: floor, step: least, growth, cut: true, share: 0 });
  }
  return settings;
}

// Fits a page's flows. Of the flows that fit at the narrowest leading, the page takes the
// widest leading at which those that fit hold nine tenths of their letters, and each of them
// takes the page's or, where it does not fit there, the widest at which it fits. A flow that
// does not fit at the narrowest takes that one, the blank paper beyond its foot that it needs,
// and the first step of its print's size at which it fits; at the last step it is cut at its
// foot.
export function fitted(flows: Flow[], leadings: number[], measure: Measure): Fitting {
  const floor = leadings[0];
  if (floor === undefined) {
    throw new Error("A page has at least one leading to take.");
  }
  const reaches = reachesOf(flows, leadings, measure);
  reaches.tryAt(new Map(flows.map((flow) => [flow, 0])));
  const fitting = flows.filter((flow) => fitsAt(reaches, flow, 0));
  const widest = pageLeadingOf(fitting, leadings, reaches);
  const own = ownLeadingsOf(fitting, widest, reaches);
  const leading = leadings[widest] ?? floor;
  const settings = new Map<string, Setting>();
  for (const flow of fitting) {
    const index = own.get(flow) ?? 0;
    const reach = reaches.reachAt(flow, index);
    const growth = growthOf(reach, flow, slackOf(flow));
    const share = shareOf(flow, extentOf(flow) - reach, leading);
    const ownLeading = leadings[index] ?? floor;
    settings.set(flow.key, { leading: ownLeading, step: 1, growth, cut: false, share });
  }
  const others = flows.filter((flow) => !fitting.includes(flow));
  stepsOf(others, floor, leading, reaches, measure).forEach((setting, key) =>
    settings.set(key, setting),
  );
  return { leading, settings };
}
