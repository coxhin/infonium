import type { AreaText, Box } from "./areas";

// What a page's drawing shows, which is the one source of what is drawn on the page: the
// colours of the paper and the ink under each area, and whether a part of the page is blank
// paper.

// Colours are read from a grid of this many points across an area of the drawn page, each
// channel to this many levels; a colour this far from the paper's is ink.
const SAMPLES = 24;
const LEVELS = 16;
const INK_DISTANCE = 96;
const DARKEST_PART = 0.1;
// Edges drawn in colour lean this far to red or to blue, and both kinds make up at least this
// part of what stands out.
const FRINGE = 40;
const FRINGED_PART = 0.15;
const PAPER = "rgb(255, 255, 255)";
const INK = "rgb(20, 20, 20)";

export type Colours = { paper: string; ink: string };

// The canvas a page was last drawn on, and how many drawings of it have ended.
export type Drawn = { canvas: HTMLCanvasElement; count: number };

// Text of one kind on one paper is set in one ink across a page: the ink read from each area
// varies with how much of the letters' soft edges it caught, so the areas of a kind that lie on
// the same paper take one ink, the truest of those alike to the middle of their inks. An ink
// that stands out from that middle, as a red notice among black paragraphs, is the text's own
// colour and is kept.
export function evened(
  texts: AreaText[],
  read: ReadonlyMap<string, Colours>,
): ReadonlyMap<string, Colours> {
  const groups = new Map<string, string[]>();
  for (const text of texts) {
    const colours = read.get(text.key);
    if (colours !== undefined) {
      const group = groupOf(text, colours);
      groups.set(group, [...(groups.get(group) ?? []), colours.ink]);
    }
  }
  const middles = new Map<string, string>();
  groups.forEach((inks, group) => {
    const sorted = [...inks].sort((first, second) => brightnessOf(first) - brightnessOf(second));
    middles.set(group, sorted[Math.floor(sorted.length / 2)] ?? INK);
  });
  // Of the inks alike to the middle, the one furthest from the paper is the truest: letters
  // drawn small lose their middle to their soft edges and read lighter than they are printed.
  const truest = new Map<string, string>();
  for (const text of texts) {
    const colours = read.get(text.key);
    const group = colours === undefined ? undefined : groupOf(text, colours);
    const middle = group === undefined ? undefined : middles.get(group);
    if (colours === undefined || group === undefined || middle === undefined) {
      continue;
    }
    const paper = rgbOf(colours.paper);
    const kept = truest.get(group) ?? middle;
    const alike = distance(rgbOf(middle), rgbOf(colours.ink)) <= ALIKE_INKS;
    if (alike && distance(rgbOf(colours.ink), paper) > distance(rgbOf(kept), paper)) {
      truest.set(group, colours.ink);
    }
  }
  const evenedColours = new Map<string, Colours>();
  for (const text of texts) {
    const colours = read.get(text.key);
    if (colours !== undefined) {
      const middle = middles.get(groupOf(text, colours));
      const alike = middle !== undefined && distance(rgbOf(middle), rgbOf(colours.ink)) <= ALIKE_INKS;
      const ink = alike ? (truest.get(groupOf(text, colours)) ?? middle) : colours.ink;
      evenedColours.set(text.key, { paper: colours.paper, ink });
    }
  }
  return evenedColours;
}

// Inks closer than this, as a distance between colours, are one ink read unevenly; the greys
// read from black text lie within 60 of one another, a red is some 250 from them.
const ALIKE_INKS = 80;

function rgbOf(colour: string): Rgb {
  const [red = 0, green = 0, blue = 0] = (colour.match(/\d+/g) ?? []).map(Number);
  return [red, green, blue];
}

// Papers alike to the eye are one paper: each channel is taken to eight levels.
const PAPER_LEVEL = 32;

export function paperOf(colours: Colours): string {
  return (colours.paper.match(/\d+/g) ?? [])
    .map((channel) => Math.floor(Number(channel) / PAPER_LEVEL))
    .join(",");
}

function groupOf(text: AreaText, colours: Colours): string {
  return `${text.kind}|${paperOf(colours)}`;
}

function brightnessOf(colour: string): number {
  return (colour.match(/\d+/g) ?? []).reduce((sum, channel) => sum + Number(channel), 0);
}

type Rgb = [number, number, number];

function distance(first: Rgb, second: Rgb): number {
  return Math.hypot(first[0] - second[0], first[1] - second[1], first[2] - second[2]);
}

function rgb([red, green, blue]: Rgb): string {
  return `rgb(${red}, ${green}, ${blue})`;
}

// The pixels of a part of the drawing, and where that part lies on the canvas.
type Pixels = { data: Uint8ClampedArray; width: number; height: number };

function pixelsOf(canvas: HTMLCanvasElement, box: Box): Pixels | undefined {
  const context = canvas.getContext("2d");
  if (context === null || canvas.width === 0) {
    return undefined;
  }
  const left = Math.max(0, Math.floor(box.left * canvas.width));
  const top = Math.max(0, Math.floor(box.top * canvas.height));
  const right = Math.min(canvas.width, Math.ceil((box.left + box.width) * canvas.width));
  const bottom = Math.min(canvas.height, Math.ceil((box.top + box.height) * canvas.height));
  if (right <= left || bottom <= top) {
    return undefined;
  }
  const width = right - left;
  const height = bottom - top;
  return { data: context.getImageData(left, top, width, height).data, width, height };
}

// The colour most of an area has, as the paper, and the colour most of what stands out from
// it has, as the ink: both read from the page as it is drawn.
export function coloursOf(canvas: HTMLCanvasElement, box: Box): Colours | undefined {
  const context = canvas.getContext("2d");
  if (context === null || canvas.width === 0) {
    return undefined;
  }
  const left = Math.floor(box.left * canvas.width);
  const top = Math.floor(box.top * canvas.height);
  const width = Math.max(1, Math.floor(box.width * canvas.width));
  const height = Math.max(1, Math.floor(box.height * canvas.height));
  const pixels = context.getImageData(left, top, width, height).data;
  const counts = new Map<string, number>();
  const samples: Rgb[] = [];
  const step = 256 / LEVELS;
  for (let row = 0; row < SAMPLES; row += 1) {
    for (let column = 0; column < SAMPLES * 2; column += 1) {
      const x = Math.floor(((column + 0.5) / (SAMPLES * 2)) * width);
      const y = Math.floor(((row + 0.5) / SAMPLES) * height);
      const at = (y * width + x) * 4;
      const colour: Rgb = [pixels[at] ?? 0, pixels[at + 1] ?? 0, pixels[at + 2] ?? 0];
      samples.push(colour);
      const level = colour.map((channel) => Math.floor(channel / step)).join(",");
      counts.set(level, (counts.get(level) ?? 0) + 1);
    }
  }
  const ranked = [...counts.entries()].sort((first, second) => second[1] - first[1]);
  const winning = ranked[0]?.[0];
  if (winning === undefined) {
    return undefined;
  }
  // The paper is the commonest exact colour of the commonest bin: a bin holds the paper and the
  // soft edges of letters near it alike, and the first sample to fall in may be such an edge.
  const exact = new Map<string, { colour: Rgb; count: number }>();
  for (const sample of samples) {
    if (sample.map((channel) => Math.floor(channel / step)).join(",") === winning) {
      const counted = exact.get(sample.join(",")) ?? { colour: sample, count: 0 };
      counted.count += 1;
      exact.set(sample.join(","), counted);
    }
  }
  const paper = [...exact.values()].sort((first, second) => second.count - first.count)[0]?.colour;
  if (paper === undefined) {
    return undefined;
  }
  // The ink is the darkest of what stands out from the paper, as the soft edges of letters are
  // lighter. The edges of small letters may be drawn in colour, reddish on one side and bluish
  // on the other; where both are found, the ink is taken to be grey, as dark as the darkest.
  const brightness = (colour: Rgb) => colour[0] + colour[1] + colour[2];
  const inked = samples
    .filter((sample) => distance(sample, paper) > INK_DISTANCE)
    .sort((first, second) => brightness(first) - brightness(second));
  const darkest = inked[Math.floor(inked.length * DARKEST_PART)];
  const reddish = inked.filter(([red, , blue]) => red - blue > FRINGE).length;
  const bluish = inked.filter(([red, , blue]) => blue - red > FRINGE).length;
  const fringed = Math.min(reddish, bluish) >= FRINGED_PART * inked.length;
  const grey = darkest === undefined ? undefined : Math.round(brightness(darkest) / 3);
  const ink: Rgb | undefined =
    darkest === undefined || grey === undefined ? undefined : fringed ? [grey, grey, grey] : darkest;
  const light = paper[0] + paper[1] + paper[2] > 384;
  return { paper: rgb(paper), ink: ink === undefined ? (light ? INK : PAPER) : rgb(ink) };
}

// A pixel this far from the paper's colour, as a distance between colours, is drawn on: the
// soft edge of a letter and a light grey rule as much as ink.
const DRAWN_DISTANCE = 48;
// Letters reach past the boxes of their lines, and so into the strips beside them, by up to
// this part of the body's size: what is drawn that near a strip's sides is taken for them. In
// two papers, descenders reached one or two pixel rows into the strip under their line, and
// letters at a line's end two columns into the strip beside it, at twice the printed size.
const LETTERS_REACH = 0.25;
// A run of pixels drawn on, across or down, this many times the body's size long is a rule or
// the side of a frame wherever it stands: a part of a letter is shorter.
const RULE_LENGTH = 2;
// Fewer pixels drawn on than this, away from a strip's sides, are specks on blank paper.
const SPECKS = 3;

// What is drawn on a part of the drawing, line by line, by rows or by columns: how many pixels
// of each line are drawn on between two places along it, and its longest run of pixels drawn
// on.
type DrawnLines = { drawn: number[]; longest: number[] };

function drawnLines(
  pixels: Pixels,
  paper: Rgb,
  byColumn: boolean,
  from: number,
  to: number,
): DrawnLines {
  const count = byColumn ? pixels.width : pixels.height;
  const along = byColumn ? pixels.height : pixels.width;
  const drawn = new Array<number>(count).fill(0);
  const longest = new Array<number>(count).fill(0);
  const reach = DRAWN_DISTANCE * DRAWN_DISTANCE;
  for (let line = 0; line < count; line += 1) {
    let run = 0;
    for (let place = 0; place < along; place += 1) {
      const pixel = byColumn ? place * pixels.width + line : line * pixels.width + place;
      const at = pixel * 4;
      const red = (pixels.data[at] ?? 0) - paper[0];
      const green = (pixels.data[at + 1] ?? 0) - paper[1];
      const blue = (pixels.data[at + 2] ?? 0) - paper[2];
      if (red * red + green * green + blue * blue > reach) {
        run += 1;
        longest[line] = Math.max(longest[line] ?? 0, run);
        if (place >= from && place < to) {
          drawn[line] = (drawn[line] ?? 0) + 1;
        }
      } else {
        run = 0;
      }
    }
  }
  return { drawn, longest };
}

// How many pixels of the drawing a part of the body's size takes.
function pixelsFor(canvas: HTMLCanvasElement, size: number, part: number): number {
  return Math.ceil(part * size * canvas.height);
}

// Whether the drawing shows only a paper in a box, given the size of the body's text as a part
// of the page's height: no rule or side of a frame anywhere in it, and nothing but specks away
// from its sides, where a formula, a figure or a page number would stand.
export function isBlank(
  canvas: HTMLCanvasElement,
  box: Box,
  paper: string,
  size: number,
): boolean {
  const pixels = pixelsOf(canvas, box);
  if (pixels === undefined) {
    return true;
  }
  const colour = rgbOf(paper);
  const reach = pixelsFor(canvas, size, LETTERS_REACH);
  const rule = pixelsFor(canvas, size, RULE_LENGTH);
  const rows = drawnLines(pixels, colour, false, reach, pixels.width - reach);
  const columns = drawnLines(pixels, colour, true, 0, 0);
  if ([...rows.longest, ...columns.longest].some((longest) => longest >= rule)) {
    return false;
  }
  const inside = rows.drawn
    .slice(reach, Math.max(reach, pixels.height - reach))
    .reduce((sum, drawn) => sum + drawn, 0);
  return inside < SPECKS;
}

// How far into a box the drawing shows only a paper, from its top, or from its right where the
// run of lines goes across, as a part of the page's height or width: up to the first line of
// pixels that holds a rule or more than specks, past the reach of the letters above it, and
// short of it by as much again, so that the edge of a paper laid there and the letters set on
// it do not reach what is drawn.
export function blankInto(
  canvas: HTMLCanvasElement,
  box: Box,
  across: boolean,
  paper: string,
  size: number,
): number {
  const pixels = pixelsOf(canvas, box);
  if (pixels === undefined) {
    return 0;
  }
  const reach = pixelsFor(canvas, size, LETTERS_REACH);
  const rule = pixelsFor(canvas, size, RULE_LENGTH);
  const along = across ? pixels.height : pixels.width;
  const lines = drawnLines(pixels, rgbOf(paper), across, 0, along);
  const inOrder = across ? [...lines.drawn.keys()].reverse() : [...lines.drawn.keys()];
  const first = inOrder.findIndex(
    (line, index) =>
      (lines.longest[line] ?? 0) >= rule || (index >= reach && (lines.drawn[line] ?? 0) >= SPECKS),
  );
  const blank = first === -1 ? inOrder.length : Math.max(0, first - reach);
  return across
    ? Math.min(box.width, blank / canvas.width)
    : Math.min(box.height, blank / canvas.height);
}
