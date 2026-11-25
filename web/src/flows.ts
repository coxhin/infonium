import { isBody, type AreaText, type BodyPrint, type Box } from "./areas";

// How the areas of a page's translation are set together. Areas of one column that follow one
// another down the page with nothing drawn between them are one flow, set as one run of text
// from the top of its first area's room; a page takes one leading, the widest at which its
// flows fit; a flow that does not fit takes blank paper under it, then is set smaller in steps,
// then is cut; and the room a flow leaves is spread between its blocks. What this must know of
// the page, whether a part of it is blank paper, and of the text, how far it reaches, it is
// told by the functions it is given.

// The left and right sides that blocks standing one above another share, as fractions of the
// page's width.
export type Column = { left: number; right: number };

// Runs whose sides overlap by more than this part of the narrower one are of one column.
const SAME_COLUMN = 0.5;
// The engine boxes lines in whole points, so a block's lines may stand out of their column by a
// point or two: a column holds an area that reaches past its sides by no more than this part of
// the page's width, 6 points of a letter page.
const SIDE_SLACK = 0.01;
// Rooms that meet are computed apart, and may miss each other by this much.
const TOUCHING = 1e-9;

function widthOf(column: Column): number {
  return column.right - column.left;
}

function overlapOf(one: Column, other: Column): number {
  return Math.min(one.right, other.right) - Math.max(one.left, other.left);
}

function sidesOf(box: Box): Column {
  return { left: box.left, right: box.left + box.width };
}

// The columns of a page, told by the runs of three lines or more of all its blocks: runs whose
// sides overlap by more than half of the narrower one are of one column, which reaches as far
// as the widest of them. The narrower runs are taken first, so that a run across two columns,
// which overlaps each of them so, stands as a column of its own rather than joining them.
export function columnsOf(runs: Box[]): Column[] {
  const columns: Column[] = [];
  const narrowestFirst = runs.map(sidesOf).sort((one, other) => widthOf(one) - widthOf(other));
  for (const run of narrowestFirst) {
    const joined = columns.filter(
      (column) =>
        overlapOf(column, run) > SAME_COLUMN * Math.min(widthOf(column), widthOf(run)),
    );
    const [only] = joined;
    if (only !== undefined && joined.length === 1) {
      columns[columns.indexOf(only)] = {
        left: Math.min(only.left, run.left),
        right: Math.max(only.right, run.right),
      };
    } else {
      columns.push(run);
    }
  }
  return columns;
}

// The narrowest column that holds a box.
function columnOf(box: Box, columns: Column[]): Column | undefined {
  const sides = sidesOf(box);
  return columns
    .filter(
      (column) =>
        sides.left >= column.left - SIDE_SLACK && sides.right <= column.right + SIDE_SLACK,
    )
    .sort((one, other) => widthOf(one) - widthOf(other))[0];
}

// The body's print where a flow measures it: as fractions of the page's height for lines
// written across, and for lines written downwards as fractions of its width.
type FlowPrint = { size: number; pitch: number };

// Areas set as one run of text. Its room's sides are its column's, or the area's own where it
// stands alone; its room reaches from the top of its first area's room to the foot of its last
// one's.
// Lengths along the run of its lines, the gaps and how far it may grow, are fractions of the
// page's height, or of its width for lines written downwards. Before each area stands the gap
// that the rooms leave on the page and a number of shares of the flow's spare room.
export type Flow = {
  key: string;
  texts: AreaText[];
  room: Box;
  vertical: boolean;
  gaps: number[];
  weights: number[];
  // How far under its room, or for lines written downwards beyond its left side, the page
  // shows blank paper that the flow may take.
  below: number;
  print: FlowPrint;
};

// What a flow is told of the page it is set on.
export type PageFacts = {
  // The paper a text's lines stand on, alike for papers alike to the eye.
  paperOf: (text: AreaText) => string | undefined;
  // Whether the page shows only a text's paper in a box.
  blank: (box: Box, text: AreaText) => boolean;
  // How far into a box, from its top, or from its right for lines written downwards, the page
  // shows only a text's paper.
  blankInto: (box: Box, vertical: boolean, text: AreaText) => number;
};

// A gap before a heading takes this many shares of a flow's spare room, a gap between two
// other blocks one, and a gap inside one block none.
const HEADING_SHARES = 2.5;
const BLOCK_SHARES = 1;
// A flow that does not fit takes no more blank paper under it than this many lines of the
// body's pitch.
const MOST_GROWTH_LINES = 2;

function footOf(box: Box): number {
  return box.top + box.height;
}

function rightOf(box: Box): number {
  return box.left + box.width;
}

function sharesBefore(text: AreaText, last: AreaText): number {
  if (text.blockNumber === last.blockNumber) {
    return 0;
  }
  return text.kind === "heading" ? HEADING_SHARES : BLOCK_SHARES;
}

// The blank paper beside an area, between its own sides and its column's, over the height of
// its room.
function besideIsBlank(text: AreaText, column: Column, facts: PageFacts): boolean {
  const { room } = text.area;
  const strips = [
    { left: column.left, top: room.top, width: room.left - column.left, height: room.height },
    {
      left: rightOf(room),
      top: room.top,
      width: column.right - rightOf(room),
      height: room.height,
    },
  ];
  return strips.every((strip) => strip.width <= 0 || facts.blank(strip, text));
}

// Whether the next area goes on from the last in one flow: on paper of one colour, below its
// lines, with only blank paper between their rooms across the column. Rooms of blocks that do
// not stand level may overlap a little, as each takes half a leading.
function goesOn(last: AreaText, next: AreaText, column: Column, facts: PageFacts): boolean {
  const paper = facts.paperOf(last);
  if (paper === undefined || paper !== facts.paperOf(next)) {
    return false;
  }
  if (next.area.room.top < footOf(last.area.box) - TOUCHING) {
    return false;
  }
  const foot = footOf(last.area.room);
  const between = {
    left: column.left,
    top: foot,
    width: widthOf(column),
    height: next.area.room.top - foot,
  };
  return between.height <= 0 || facts.blank(between, last);
}

// A flow's own body print: the page's for lines written across, the area's own for lines
// written downwards, which are measured across the page.
function flowPrintOf(texts: AreaText[], vertical: boolean, body: BodyPrint): FlowPrint {
  const first = texts[0];
  if (!vertical || first === undefined) {
    return { size: body.size, pitch: body.pitch };
  }
  const { typeset, spacing } = first.area;
  return { size: typeset.size, pitch: typeset.pitch ?? typeset.size * spacing };
}

// How far the page shows blank paper under a flow's room, or beyond its left for lines written
// downwards: no further than the page's edge, the lines of any block or the room of any other
// area that stands there, or two lines of the body's pitch.
function belowOf(
  flow: Omit<Flow, "below">,
  lines: Box[],
  rooms: Box[],
  facts: PageFacts,
): number {
  const first = flow.texts[0];
  if (first === undefined) {
    return 0;
  }
  const most = MOST_GROWTH_LINES * flow.print.pitch;
  const { room } = flow;
  if (flow.vertical) {
    const reach = [...lines, ...rooms]
      .filter(
        (other) =>
          rightOf(other) <= room.left + TOUCHING &&
          other.top < footOf(room) &&
          room.top < footOf(other),
      )
      .map((other) => room.left - rightOf(other));
    const bound = Math.max(0, Math.min(most, room.left, ...reach));
    return facts.blankInto(
      { left: room.left - bound, top: room.top, width: bound, height: room.height },
      true,
      first,
    );
  }
  const foot = footOf(room);
  const reach = [...lines, ...rooms]
    .filter(
      (other) =>
        other.top >= foot - TOUCHING && other.left < rightOf(room) && room.left < rightOf(other),
    )
    .map((other) => other.top - foot);
  const bound = Math.max(0, Math.min(most, 1 - foot, ...reach));
  return facts.blankInto(
    { left: room.left, top: foot, width: room.width, height: bound },
    false,
    first,
  );
}

function flowOf(
  texts: AreaText[],
  column: Column | undefined,
  body: BodyPrint,
): Omit<Flow, "below"> {
  const [first] = texts;
  const last = texts.at(-1);
  if (first === undefined || last === undefined) {
    throw new Error("A flow holds at least one area.");
  }
  const vertical = first.area.vertical;
  const sides = column ?? sidesOf(first.area.room);
  const top = first.area.room.top;
  const room = vertical
    ? first.area.room
    : { left: sides.left, top, width: widthOf(sides), height: footOf(last.area.room) - top };
  return {
    key: first.key,
    texts,
    room,
    vertical,
    gaps: texts.map((text, index) => {
      const before = texts[index - 1];
      return before === undefined ? 0 : Math.max(0, text.area.room.top - footOf(before.area.room));
    }),
    weights: texts.map((text, index) => {
      const before = texts[index - 1];
      return before === undefined ? 0 : sharesBefore(text, before);
    }),
    print: flowPrintOf(texts, vertical, body),
  };
}

// The flows of a page's translation, from its areas, the page's columns and the line boxes of
// all its blocks. Areas of one column are taken down the page; an area that has something
// drawn beside it within its column stands alone with its own sides, and so does one that
// belongs to no column and one of lines written downwards.
export function flowsOf(
  texts: AreaText[],
  columns: Column[],
  lines: Box[],
  body: BodyPrint,
  facts: PageFacts,
): Flow[] {
  const alone: { texts: AreaText[]; column: Column | undefined }[] = [];
  const byColumn = new Map<Column, AreaText[]>();
  for (const text of texts) {
    const column = text.area.vertical ? undefined : columnOf(text.area.room, columns);
    if (column === undefined) {
      alone.push({ texts: [text], column: undefined });
    } else {
      byColumn.set(column, [...(byColumn.get(column) ?? []), text]);
    }
  }
  const runs = [...alone];
  byColumn.forEach((inColumn, column) => {
    const down = [...inColumn].sort((one, other) => one.area.room.top - other.area.room.top);
    let current: AreaText[] = [];
    for (const text of down) {
      const last = current.at(-1);
      if (!besideIsBlank(text, column, facts)) {
        if (current.length > 0) {
          runs.push({ texts: current, column });
        }
        runs.push({ texts: [text], column: undefined });
        current = [];
      } else if (last === undefined || goesOn(last, text, column, facts)) {
        current.push(text);
      } else {
        runs.push({ texts: current, column });
        current = [text];
      }
    }
    if (current.length > 0) {
      runs.push({ texts: current, column });
    }
  });
  const flows = runs.map((run) => flowOf(run.texts, run.column, body));
  return flows.map((flow) => {
    const others = texts.filter((text) => !flow.texts.includes(text));
    return { ...flow, below: belowOf(flow, lines, others.map((text) => text.area.room), facts) };
  });
}

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

// How a flow is set: at a leading and a step, taking so much blank paper under its room, cut
// at its foot or not, and with a share of its spare room added to each gap for each share the
// gap takes.
export type Setting = Trial & { growth: number; cut: boolean; share: number };

// The page's leading and how each of its flows, by key, is set.
export type Fitting = { leading: number; settings: Map<string, Setting> };

// How far each flow's text reaches at a trial, from the top of its room along the run of its
// lines, with no spare room added: the measure is the page's, which lays the text out.
export type Measure = (trials: ReadonlyMap<Flow, Trial>) => ReadonlyMap<Flow, number>;

// The parts of its print's size a flow that does not fit is set at, in order.
const STEPS = [1, 0.95, 0.9, 0.85, 0.8];
// A flow fits whose text ends no lower than this part of the body's size below its foot.
const FIT_SLACK = 0.25;
// A gap grows by no more than this part of a line of the body's pitch at the page's leading
// for each share it takes.
const SHARE_LINES = 0.5;

function extentOf(flow: Flow): number {
  return flow.vertical ? flow.room.width : flow.room.height;
}

function fits(flow: Flow, reach: number, growth: number): boolean {
  return reach <= extentOf(flow) + growth + FIT_SLACK * flow.print.size;
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

// Fits a page's flows: the page takes the widest of the leadings at which every flow that fits
// at the narrowest fits, and each such flow takes it. A flow that does not fit at the narrowest
// takes that one, the blank paper under it that it needs, and the first step of its print's
// size at which it fits; at the last step it is cut at its foot.
export function fitted(flows: Flow[], leadings: number[], measure: Measure): Fitting {
  const floor = leadings[0];
  if (floor === undefined) {
    throw new Error("A page has at least one leading to take.");
  }
  const settings = new Map<string, Setting>();
  const atFloor = measure(trialsOf(flows, { leading: floor, step: 1 }));
  const fitting = flows.filter((flow) => fits(flow, atFloor.get(flow) ?? Infinity, 0));
  let widest = 0;
  let reaches = atFloor;
  let low = 1;
  let high = fitting.length > 0 ? leadings.length - 1 : 0;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const leading = leadings[middle] ?? floor;
    const tried = measure(trialsOf(fitting, { leading, step: 1 }));
    if (fitting.every((flow) => fits(flow, tried.get(flow) ?? Infinity, 0))) {
      widest = middle;
      reaches = tried;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  const leading = leadings[widest] ?? floor;
  for (const flow of fitting) {
    const reach = reaches.get(flow) ?? extentOf(flow);
    const share = shareOf(flow, extentOf(flow) - reach, leading);
    settings.set(flow.key, { leading, step: 1, growth: 0, cut: false, share });
  }
  let left = flows.filter((flow) => !fitting.includes(flow));
  for (const step of STEPS) {
    if (left.length === 0) {
      break;
    }
    const tried = step === 1 ? atFloor : measure(trialsOf(left, { leading: floor, step }));
    left = left.filter((flow) => {
      const reach = tried.get(flow) ?? Infinity;
      if (!fits(flow, reach, flow.below)) {
        return true;
      }
      const growth = Math.min(flow.below, Math.max(0, reach - extentOf(flow)));
      const share = shareOf(flow, extentOf(flow) + growth - reach, leading);
      settings.set(flow.key, { leading: floor, step, growth, cut: false, share });
      return false;
    });
  }
  const least = STEPS.at(-1) ?? 1;
  for (const flow of left) {
    const growth = flow.below;
    settings.set(flow.key, { leading: floor, step: least, growth, cut: true, share: 0 });
  }
  return { leading, settings };
}
