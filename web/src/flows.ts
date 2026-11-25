import { isBody, overlap, type AreaText, type BodyPrint, type Box } from "./areas";

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
// stands alone. Its room reaches from the top of its first area's room, or from below what is
// drawn there above its lines, to the foot of its last one's, or to short of what is drawn
// there under its lines or of another block's lines that begin within it; parts of its own
// lines that stand below that foot are its tails, which its paper covers there alone.
// Lengths along the run of its lines, the gaps and how far it may go beyond its foot, are
// fractions of the page's height, or of its width for lines written downwards. Before each
// area stands the gap that the rooms leave on the page and a number of shares of the flow's
// spare room.
export type Flow = {
  key: string;
  texts: AreaText[];
  // The keys of its areas that are pieces the engine cut out of a line of another of them.
  pieces: ReadonlySet<string>;
  room: Box;
  tails: Box[];
  // How far from its top, and from its foot, the page shows something drawn, as far as it was
  // looked into; nothing where none was seen.
  drawn: { over: number | undefined; under: number | undefined };
  vertical: boolean;
  gaps: number[];
  weights: number[];
  // How far beyond its foot, under it or for lines written downwards beyond its left side,
  // the page shows blank paper that the flow may go over, a quarter of the body's size clear
  // of what is drawn next and of the next block's lines, and no further than two lines of the
  // body's pitch.
  clear: number;
  print: FlowPrint;
};

// The side of a box from which the page is looked into.
export type Side = "top" | "bottom" | "right";

// How far from a side of a box the page shows only blank paper, and whether something is drawn
// at that distance or the box ends there.
export type Blank = { blank: number; drawn: boolean };

// What a flow is told of the page it is set on.
export type PageFacts = {
  // The paper a text's lines stand on, alike for papers alike to the eye.
  paperOf: (text: AreaText) => string | undefined;
  // Whether the page shows only a text's paper in a box, its letters reaching in aside.
  blank: (box: Box, text: AreaText) => boolean;
  // How far into a box, from one of its sides, the page shows only a text's paper.
  blankInto: (box: Box, from: Side, text: AreaText) => Blank;
};

// A line of a block on the page.
export type PageLine = { blockNumber: number; box: Box };

// A gap before a heading takes this many shares of a flow's spare room, a gap between two
// other blocks one, and a gap inside one block none.
const HEADING_SHARES = 2.5;
const BLOCK_SHARES = 1;
// A flow goes no further beyond its foot than this many lines of the body's pitch.
const MOST_GROWTH_LINES = 2;
// A flow keeps this part of the body's size clear of what is drawn beyond its lines and of
// other blocks' lines.
const CLEARANCE = 0.25;
// The paper is laid past a flow by an edge of this part of the body's size, to cover the
// source's letters that reach past the boxes of their lines.
const EDGE = 0.2;

// How far a flow's paper may go into blank paper, kept clear of what is drawn there.
function clearOf({ blank, drawn }: Blank, print: FlowPrint): number {
  return drawn ? Math.max(0, blank - CLEARANCE * print.size) : blank;
}

// Whether a flow's paper may be laid past its top and past its foot, as far again as its edge,
// where it goes so far beyond its foot: not where that would bring the edge closer to what is
// drawn there than the clearance.
export function edgesOf(flow: Flow, growth: number): { top: boolean; foot: boolean } {
  const margin = (CLEARANCE + EDGE) * flow.print.size;
  const { over, under } = flow.drawn;
  return {
    top: over === undefined || over >= margin,
    foot: under === undefined || under - growth >= margin,
  };
}

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

// The blank paper beside an area's lines, between its own sides and its column's.
function besideIsBlank(text: AreaText, column: Column, facts: PageFacts): boolean {
  const { box } = text.area;
  const strips = [
    { left: column.left, top: box.top, width: box.left - column.left, height: box.height },
    { left: rightOf(box), top: box.top, width: column.right - rightOf(box), height: box.height },
  ];
  return strips.every((strip) => strip.width <= 0 || facts.blank(strip, text));
}

// Whether the next area goes on from the last in one flow: on paper of one colour, below its
// lines, with only blank paper between their lines across the column. What is drawn there ends
// the flow though it lie within a room, as the rule above a footnote does.
function goesOn(last: AreaText, next: AreaText, column: Column, facts: PageFacts): boolean {
  const paper = facts.paperOf(last);
  if (paper === undefined || paper !== facts.paperOf(next)) {
    return false;
  }
  const foot = footOf(last.area.box);
  if (next.area.box.top < foot - TOUCHING) {
    return false;
  }
  const between = {
    left: column.left,
    top: foot,
    width: widthOf(column),
    height: next.area.box.top - foot,
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

type Span = { left: number; width: number };

function overlapsAcross(one: Span, other: Span): boolean {
  return one.left < other.left + other.width && other.left < one.left + one.width;
}

function overlapsDown(one: Box, other: Box): boolean {
  return one.top < footOf(other) && other.top < footOf(one);
}

// Where a flow's paper lies and how far beyond its foot it may go, from its areas' rooms and
// lines, the lines of other blocks and the rooms of other areas on the page, and what the page
// shows.
type Reach = Pick<Flow, "room" | "tails" | "drawn" | "clear">;

function verticalReachOf(
  first: AreaText,
  others: Box[],
  print: FlowPrint,
  facts: PageFacts,
): Reach {
  const { room } = first.area;
  const clearance = CLEARANCE * print.size;
  const bound = Math.max(
    0,
    Math.min(
      MOST_GROWTH_LINES * print.pitch,
      room.left,
      ...others
        .filter((other) => rightOf(other) <= room.left + TOUCHING && overlapsDown(other, room))
        .map((other) => room.left - rightOf(other) - clearance),
    ),
  );
  const beyond = facts.blankInto(
    { left: room.left - bound, top: room.top, width: bound, height: room.height },
    "right",
    first,
  );
  const drawn = { over: undefined, under: undefined };
  return { room, tails: [], drawn, clear: clearOf(beyond, print) };
}

// The page is looked into from a flow's lines beyond the top of its first area's room and the
// foot of its last one's, by as far as its paper's edge and the clearance before it reach, and
// below that by as far as the flow may go beyond its foot.
function reachOf(
  texts: AreaText[],
  blocks: ReadonlySet<number>,
  sides: Column,
  lines: PageLine[],
  rooms: Box[],
  print: FlowPrint,
  facts: PageFacts,
): Reach {
  const [first] = texts;
  const last = texts.at(-1);
  if (first === undefined || last === undefined) {
    throw new Error("A flow holds at least one area.");
  }
  const clearance = CLEARANCE * print.size;
  const margin = (CLEARANCE + EDGE) * print.size;
  const across = { left: sides.left, width: widthOf(sides) };
  const firstLines = first.area.box;
  const lastLines = last.area.box;
  // Above: the paper begins at the top of the first room, or a clearance below what is drawn
  // there above the lines.
  const roomTop = first.area.room.top;
  const overTop = Math.max(0, roomTop - margin);
  const overhead = facts.blankInto(
    { ...across, top: overTop, height: firstLines.top - overTop },
    "bottom",
    first,
  );
  const drawnAbove = firstLines.top - overhead.blank;
  const top =
    overhead.drawn && drawnAbove > roomTop
      ? Math.min(firstLines.top, drawnAbove + clearance)
      : roomTop;
  // Below: the paper ends at the foot of the last room, or a clearance above what is drawn
  // there under the lines, or above another block's lines that begin within the room; it may go
  // beyond that foot only where it reaches it.
  const roomFoot = footOf(last.area.room);
  const ownLines = texts.flatMap((text) => text.area.lines);
  const otherLines = lines
    .filter((line) => !blocks.has(line.blockNumber))
    .map((line) => line.box);
  const linesUnder = otherLines.filter(
    (other) => overlapsAcross(other, across) && other.top > firstLines.top,
  );
  const linesWithin = linesUnder.filter((other) => other.top < roomFoot);
  const roomsUnder = rooms.filter(
    (other) => overlapsAcross(other, across) && other.top >= roomFoot - TOUCHING,
  );
  const lastFoot = footOf(lastLines);
  // The paper goes beyond its foot no further than two lines, other blocks' lines or other
  // areas' rooms; the page is looked into as far again as its edge and the clearance before it.
  const growthEnd = Math.min(
    1,
    roomFoot + MOST_GROWTH_LINES * print.pitch,
    ...linesUnder.map((other) => other.top - clearance),
    ...roomsUnder.map((other) => other.top),
  );
  const lookedTo = Math.max(lastFoot, Math.min(1, Math.max(growthEnd, roomFoot) + margin));
  const underneath = facts.blankInto(
    { ...across, top: lastFoot, height: lookedTo - lastFoot },
    "top",
    last,
  );
  const drawnBelow = lastFoot + underneath.blank;
  const clearBelow = underneath.drawn ? drawnBelow - clearance : lookedTo;
  // What is drawn within the room under the lines ends the paper short of it, but leaves the
  // lines whole; another block's lines that begin within the room, beside the last ones, end
  // the paper across the column short of them.
  const drawnWithin = underneath.drawn && drawnBelow < roomFoot;
  const foot = Math.max(
    top,
    Math.min(
      drawnWithin ? Math.max(lastFoot, clearBelow) : roomFoot,
      ...linesWithin.map((other) => other.top - clearance),
    ),
  );
  const room = { ...across, top, height: foot - top };
  const tails = ownLines
    .filter((line) => footOf(line) > foot + TOUCHING)
    .map((line) => ({ ...line, top: foot, height: footOf(line) - foot }));
  const drawn = {
    over: overhead.drawn ? top - drawnAbove : undefined,
    under: underneath.drawn ? drawnBelow - foot : undefined,
  };
  const clear =
    foot < roomFoot - TOUCHING ? 0 : Math.max(0, Math.min(clearBelow, growthEnd) - foot);
  return { room, tails, drawn, clear };
}

function flowOf(
  texts: AreaText[],
  pieces: ReadonlySet<AreaText>,
  column: Column | undefined,
  lines: PageLine[],
  rooms: Box[],
  body: BodyPrint,
  facts: PageFacts,
): Flow {
  const whole = texts.filter((text) => !pieces.has(text));
  const [first] = whole;
  if (first === undefined) {
    throw new Error("A flow holds at least one area that is no piece.");
  }
  const vertical = first.area.vertical;
  const print = flowPrintOf(whole, vertical, body);
  const blocks = new Set(texts.map((text) => text.blockNumber));
  const reach = vertical
    ? verticalReachOf(first, [...lines.map((line) => line.box), ...rooms], print, facts)
    : reachOf(whole, blocks, column ?? sidesOf(first.area.room), lines, rooms, print, facts);
  // A piece adds no room: the gap before it is none, and the gap after it is the one before
  // the area it was cut from.
  const gaps: number[] = [];
  const weights: number[] = [];
  let before: AreaText | undefined;
  for (const text of texts) {
    if (pieces.has(text) || before === undefined) {
      gaps.push(0);
      weights.push(0);
    } else {
      gaps.push(Math.max(0, text.area.room.top - footOf(before.area.room)));
      weights.push(sharesBefore(text, before));
    }
    before = pieces.has(text) ? (before ?? undefined) : text;
  }
  return {
    key: first.key,
    texts,
    pieces: new Set([...pieces].filter((text) => texts.includes(text)).map((text) => text.key)),
    ...reach,
    vertical,
    gaps,
    weights,
    print,
  };
}

// A part of its line box an area's line must lie within a line of another area to be a piece
// of it.
const PIECE_PART = 0.5;

// The pieces the engine cut out of a line and read apart, as "43rd" from the line of a footnote:
// each area whose every line lies for the most part within a line of another, larger area, by
// the area it was cut from.
export function piecesOf(texts: AreaText[]): Map<AreaText, AreaText> {
  const sizeOf = (text: AreaText) => text.area.box.width * text.area.box.height;
  const pieces = new Map<AreaText, AreaText>();
  for (const text of texts) {
    const from = texts.find(
      (other) =>
        other !== text &&
        !other.area.vertical &&
        sizeOf(other) > sizeOf(text) &&
        text.area.lines.every((line) =>
          other.area.lines.some(
            (host) => overlap(line, host) > PIECE_PART * line.width * line.height,
          ),
        ),
    );
    if (from !== undefined) {
      pieces.set(text, from);
    }
  }
  // A piece of a piece is a piece of the area its own was cut from.
  pieces.forEach((from, text) => {
    let host = from;
    while (pieces.has(host)) {
      host = pieces.get(host) ?? host;
    }
    pieces.set(text, host);
  });
  return pieces;
}

// A piece stands in the flow of the area it was cut from as a block of its own: before the
// areas of that area's block where its own block comes earlier in the document, after them
// otherwise.
function withPieces(texts: AreaText[], pieces: Map<AreaText, AreaText>): AreaText[] {
  let placed = [...texts];
  pieces.forEach((from, piece) => {
    const ofBlock = placed
      .map((text, index) => (text.blockNumber === from.blockNumber ? index : -1))
      .filter((index) => index !== -1);
    const firstIndex = ofBlock[0];
    const lastIndex = ofBlock.at(-1);
    if (firstIndex === undefined || lastIndex === undefined) {
      return;
    }
    const at = piece.blockNumber < from.blockNumber ? firstIndex : lastIndex + 1;
    placed = [...placed.slice(0, at), piece, ...placed.slice(at)];
  });
  return placed;
}

// The flows of a page's translation, from its areas, the page's columns and the lines of all
// its blocks. Areas of one column are taken down the page; an area that has something drawn
// beside its lines within its column stands alone with its own sides, and so does one that
// belongs to no column and one of lines written downwards.
export function flowsOf(
  texts: AreaText[],
  columns: Column[],
  lines: PageLine[],
  body: BodyPrint,
  facts: PageFacts,
): Flow[] {
  const pieces = piecesOf(texts);
  const alone: { texts: AreaText[]; column: Column | undefined }[] = [];
  const byColumn = new Map<Column, AreaText[]>();
  for (const text of texts.filter((each) => !pieces.has(each))) {
    const column = text.area.vertical ? undefined : columnOf(text.area.room, columns);
    if (column === undefined) {
      alone.push({ texts: [text], column: undefined });
    } else {
      byColumn.set(column, [...(byColumn.get(column) ?? []), text]);
    }
  }
  const runs = [...alone];
  byColumn.forEach((inColumn, column) => {
    const down = [...inColumn].sort((one, other) => one.area.box.top - other.area.box.top);
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
  return runs.map((run) => {
    const own = new Map([...pieces].filter(([, from]) => run.texts.includes(from)));
    const placed = withPieces(run.texts, own);
    const rooms = texts.filter((text) => !placed.includes(text)).map((text) => text.area.room);
    return flowOf(placed, new Set(own.keys()), run.column, lines, rooms, body, facts);
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
  const settings = new Map<string, Setting>();
  const atFloor = measure(trialsOf(flows, { leading: floor, step: 1 }));
  const fitting = flows.filter((flow) => fits(atFloor.get(flow) ?? Infinity, flow, slackOf(flow)));
  // How far each flow's text reaches at each leading tried, by the leading's place in the list.
  const reached = new Map<Flow, Map<number, number>>(
    flows.map((flow) => [flow, new Map([[0, atFloor.get(flow) ?? Infinity]])]),
  );
  const reachAt = (flow: Flow, index: number) => reached.get(flow)?.get(index) ?? Infinity;
  const tryAt = (places: ReadonlyMap<Flow, number>) => {
    const asked = new Map(
      [...places].filter(([flow, index]) => reached.get(flow)?.has(index) !== true),
    );
    if (asked.size === 0) {
      return;
    }
    const trials = new Map(
      [...asked].map(([flow, index]) => [flow, { leading: leadings[index] ?? floor, step: 1 }]),
    );
    measure(trials).forEach((reach, flow) => {
      const index = asked.get(flow);
      if (index !== undefined) {
        reached.get(flow)?.set(index, reach);
      }
    });
  };
  const fitsAt = (flow: Flow, index: number) => fits(reachAt(flow, index), flow, slackOf(flow));
  const letters = new Map(fitting.map((flow) => [flow, lettersOf(flow)]));
  const allLetters = fitting.reduce((sum, flow) => sum + (letters.get(flow) ?? 0), 0);
  const holds = (index: number) => {
    tryAt(new Map(fitting.map((flow) => [flow, index])));
    const held = fitting
      .filter((flow) => fitsAt(flow, index))
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
  // Each flow that fits at the narrowest but not at the page's leading looks for the widest at
  // which it fits, all of them at once.
  const own = new Map<Flow, number>(fitting.map((flow) => [flow, widest]));
  const searched = new Map(
    fitting
      .filter((flow) => !fitsAt(flow, widest))
      .map((flow) => [flow, { low: 1, high: widest - 1, found: 0 }]),
  );
  while ([...searched.values()].some((range) => range.low <= range.high)) {
    const middles = new Map<Flow, number>();
    searched.forEach((range, flow) => {
      if (range.low <= range.high) {
        middles.set(flow, Math.floor((range.low + range.high) / 2));
      }
    });
    tryAt(middles);
    middles.forEach((middle, flow) => {
      const range = searched.get(flow);
      if (range === undefined) {
        return;
      }
      if (fitsAt(flow, middle)) {
        range.found = middle;
        range.low = middle + 1;
      } else {
        range.high = middle - 1;
      }
    });
  }
  searched.forEach((range, flow) => own.set(flow, range.found));
  const leading = leadings[widest] ?? floor;
  for (const flow of fitting) {
    const index = own.get(flow) ?? 0;
    const reach = reachAt(flow, index);
    const growth = growthOf(reach, flow, slackOf(flow));
    const share = shareOf(flow, extentOf(flow) - reach, leading);
    const ownLeading = leadings[index] ?? floor;
    settings.set(flow.key, { leading: ownLeading, step: 1, growth, cut: false, share });
  }
  let left = flows.filter((flow) => !fitting.includes(flow));
  for (const step of STEPS) {
    if (left.length === 0) {
      break;
    }
    const tried = step === 1 ? atFloor : measure(trialsOf(left, { leading: floor, step }));
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
  return { leading, settings };
}
