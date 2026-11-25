import type { Box } from "./areas";

// Reading the print off a drawn page: how much ink each column across a line's box holds, how
// many words that shows, and where they stand.

// How much ink each column across a line's box holds, for each darkness ink is taken at; the
// box's length along the line and its depth across it, in pixels of the page as it is read.
export type Ink = { run: number; depth: number; profiles: Uint16Array[] };

// A line's box on its page, as fractions of the page, and whether its text runs downwards.
export type LineBox = { box: Box; downward: boolean };

// A page is read at this many pixels across at most, whatever its drawing's size: a letter page
// read so holds its 10 point print at about 20 pixels to the size, and a space between two
// words at 4 or 5, which is enough to find it; a drawing for a display of twice the density
// would cost four times as much to read and find no more.
const READING_WIDTH = 1224;
// A pixel is ink where it is this much darker than the paper, the paper being as light as this
// share of the line's pixels are at most. The soft edges of scanned letters fill the narrow
// spaces of a tight line, so where the spaces are not found among the gaps of the ink, they are
// sought again among those of its darkest part.
const PAPER_SHARE = 0.9;
const INK_CONTRASTS = [110, 130];
// The gaps of a line's ink that are spaces, for counting its words, are those wider than the
// rest by a clear step: the widest gap that is no space is at most this part of the narrowest
// space, and a space is this part of the line's mean character wide at least. A line whose
// gaps make no such step tells no count.
const SPACE_STEP = 0.55;
const LEAST_PRINTED_SPACE = 0.3;
// The spaces between a line's words are found among its gaps, each near where the widths of
// the words put it: a gap is taken for a space at a cost of its distance from there, in the
// line's mean characters, and of this much more the narrower it is than a space of the line,
// as a gap between letters printed apart is. The gaps are sought first among those as wide as a
// space is set, then, for a line set tight, among narrower ones too, each then no further from
// where it is expected than a few characters.
const NARROW_COST = 3;
const SPACE_WIDTH = 0.5;
const SEARCHES = [
  { least: 0.3, farthest: Infinity },
  { least: 0.12, farthest: 1.5 },
];

let reader: CanvasRenderingContext2D | null | undefined;

// The ink of each of a page's lines, read from one copy of its drawing at the reading width.
export function readInk(canvas: HTMLCanvasElement, lines: LineBox[]): (Ink | undefined)[] {
  if (canvas.width === 0 || canvas.height === 0 || lines.length === 0) {
    return lines.map(() => undefined);
  }
  const scale = Math.min(1, READING_WIDTH / canvas.width);
  const width = Math.max(1, Math.round(canvas.width * scale));
  const height = Math.max(1, Math.round(canvas.height * scale));
  if (reader === undefined) {
    reader = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  }
  if (reader === null) {
    return lines.map(() => undefined);
  }
  reader.canvas.width = width;
  reader.canvas.height = height;
  reader.drawImage(canvas, 0, 0, width, height);
  const pixels = reader.getImageData(0, 0, width, height).data;
  return lines.map((line) => inkOf(pixels, width, height, line));
}

function inkOf(
  pixels: Uint8ClampedArray,
  pageWidth: number,
  pageHeight: number,
  { box, downward }: LineBox,
): Ink | undefined {
  const left = Math.max(0, Math.floor(box.left * pageWidth));
  const top = Math.max(0, Math.floor(box.top * pageHeight));
  const width = Math.min(pageWidth - left, Math.ceil(box.width * pageWidth));
  const height = Math.min(pageHeight - top, Math.ceil(box.height * pageHeight));
  if (width <= 0 || height <= 0) {
    return undefined;
  }
  // How light each pixel is, from 0 to 255, and how many pixels are of each lightness.
  const lightness = new Uint8Array(width * height);
  const counts = new Uint32Array(256);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const at = ((top + y) * pageWidth + left + x) * 4;
      const light = Math.round(
        ((pixels[at] ?? 0) + (pixels[at + 1] ?? 0) + (pixels[at + 2] ?? 0)) / 3,
      );
      lightness[y * width + x] = light;
      counts[light] = (counts[light] ?? 0) + 1;
    }
  }
  let darkest = 0;
  while (darkest < 255 && counts[darkest] === 0) {
    darkest += 1;
  }
  let paper = 0;
  for (
    let below = 0;
    paper < 255 && below + (counts[paper] ?? 0) <= PAPER_SHARE * lightness.length;
    paper += 1
  ) {
    below += counts[paper] ?? 0;
  }
  const run = downward ? height : width;
  const limits = INK_CONTRASTS.filter((contrast) => paper - darkest >= contrast).map(
    (contrast) => paper - contrast,
  );
  if (limits.length === 0) {
    return undefined;
  }
  // The pixels are gone over once for both darknesses: a pixel dark enough for the darker
  // limit is so for the lighter one too.
  const lighter = new Uint16Array(run);
  const darker = new Uint16Array(run);
  const [lighterLimit = 0, darkerLimit = -1] = limits;
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const light = lightness[y * width + x] ?? 255;
      if (light < lighterLimit) {
        const along = downward ? y : x;
        lighter[along] = (lighter[along] ?? 0) + 1;
        if (light < darkerLimit) {
          darker[along] = (darker[along] ?? 0) + 1;
        }
      }
    }
  }
  return {
    run,
    depth: downward ? width : height,
    profiles: limits.length > 1 ? [lighter, darker] : [lighter],
  };
}

// The runs of columns without ink inside the ink of a line, and where its ink begins and ends.
function gapsOf(
  profile: Uint16Array,
): { first: number; last: number; gaps: [number, number][] } | undefined {
  let first = 0;
  while (first < profile.length && profile[first] === 0) {
    first += 1;
  }
  let last = profile.length - 1;
  while (last >= 0 && profile[last] === 0) {
    last -= 1;
  }
  if (first > last) {
    return undefined;
  }
  const gaps: [number, number][] = [];
  for (let at = first; at <= last; at += 1) {
    if (profile[at] === 0) {
      let end = at;
      while (end <= last && profile[end] === 0) {
        end += 1;
      }
      gaps.push([at, end]);
      at = end;
    }
  }
  return { first, last, gaps };
}

// The words a line's ink shows, as their lengths along the run, given how many characters its
// text holds: between the gaps of its spaces, found where the ink is taken at its lightest;
// none where its gaps leave the spaces in doubt.
export function printedWords(ink: Ink, characters: number): number[] | undefined {
  const profile = ink.profiles[0];
  const found = profile === undefined || characters === 0 ? undefined : gapsOf(profile);
  if (found === undefined) {
    return undefined;
  }
  const mean = ink.run / characters;
  const widths = found.gaps.map((gap) => gap[1] - gap[0]).sort((one, other) => other - one);
  const wide = widths.filter((width) => width >= LEAST_PRINTED_SPACE * mean).length;
  // The widest step down among the widths, where it falls among the gaps wide enough.
  let spaces = 0;
  let best = 0;
  for (let index = 1; index <= wide; index += 1) {
    const ratio = (widths[index] ?? 0) / (widths[index - 1] ?? 1);
    if (index === 1 || ratio < best) {
      best = ratio;
      spaces = index;
    }
  }
  if (wide > 0 && best > SPACE_STEP) {
    return undefined;
  }
  const least = spaces === 0 ? Infinity : (widths[spaces - 1] ?? Infinity);
  const bounds = [
    found.first,
    ...found.gaps.filter((gap) => gap[1] - gap[0] >= least).flat(),
    found.last + 1,
  ];
  const words: number[] = [];
  for (let index = 0; index + 1 < bounds.length; index += 2) {
    words.push((bounds[index + 1] ?? 0) - (bounds[index] ?? 0));
  }
  return words;
}

// Where the words of a line stand along its run, in pixels from the start of its box, given
// their widths at one size and a space's at that size, and how many characters the line holds:
// between the spaces found among the gaps in its ink.
export function inkedWords(
  ink: Ink,
  widths: number[],
  space: number,
  characters: number,
): [number, number][] | undefined {
  if (widths.length === 0) {
    return undefined;
  }
  for (const profile of ink.profiles) {
    const words = wordsInProfile(profile, widths, space, ink.run / characters);
    if (words !== undefined) {
      return words;
    }
  }
  return undefined;
}

function wordsInProfile(
  profile: Uint16Array,
  widths: number[],
  space: number,
  mean: number,
): [number, number][] | undefined {
  const found = gapsOf(profile);
  if (found === undefined) {
    return undefined;
  }
  const { first, last, gaps } = found;
  const expected = expectedSpaces(widths, space, first, last);
  let chosen: [number, number][] | undefined = widths.length === 1 ? [] : undefined;
  for (const search of SEARCHES) {
    chosen ??= spacesAmong(
      gaps.filter((gap) => gap[1] - gap[0] >= search.least * mean),
      expected,
      mean,
      search.farthest,
    );
  }
  if (chosen === undefined) {
    return undefined;
  }
  const bounds = [first, ...chosen.flat(), last + 1];
  const words: [number, number][] = [];
  for (let index = 0; index < widths.length; index += 1) {
    words.push([bounds[2 * index] ?? first, bounds[2 * index + 1] ?? last + 1]);
  }
  return words;
}

// Where the middle of each space between the words of a line is expected, in pixels along the
// run: the words at their widths and the spaces between them, stretched over the line's ink.
function expectedSpaces(widths: number[], space: number, first: number, last: number): number[] {
  const total = widths.reduce((sum, width) => sum + width, 0) + space * (widths.length - 1);
  const scale = total > 0 ? (last + 1 - first) / total : 0;
  const expected: number[] = [];
  let reached = 0;
  widths.slice(0, -1).forEach((width) => {
    reached += width;
    expected.push(first + (reached + space / 2) * scale);
    reached += space;
  });
  return expected;
}

// The gaps that are the spaces between a line's words: one for each space, in order, each near
// where its space is expected and as wide as a space is. Where too narrow or too far a gap is
// all there is for a space, as where two words touch, none.
function spacesAmong(
  gaps: [number, number][],
  expected: number[],
  mean: number,
  farthest: number,
): [number, number][] | undefined {
  // A space is as wide as the narrowest of the widest gaps, one for each space, as a line set
  // even spreads its room over its spaces alike.
  const widest = gaps.map((gap) => gap[1] - gap[0]).sort((one, other) => other - one);
  const spaceWidth = Math.max(SPACE_WIDTH * mean, widest[expected.length - 1] ?? 0);
  const cost = (space: number, gap: [number, number]) =>
    Math.abs((gap[0] + gap[1]) / 2 - (expected[space] ?? 0)) / mean +
    NARROW_COST * Math.max(0, 1 - (gap[1] - gap[0]) / spaceWidth);
  // For each space and each gap: the least cost of setting the spaces up to this one with this
  // one on that gap, and the gap the space before it is then set on.
  const steps: { cost: number; from: number }[][] = [];
  expected.forEach((_, space) => {
    const before = steps[space - 1];
    let best = { cost: space === 0 ? 0 : Infinity, at: -1 };
    steps.push(
      gaps.map((gap, index) => {
        const step = { cost: best.cost + cost(space, gap), from: best.at };
        const here = before?.[index]?.cost ?? Infinity;
        if (here < best.cost) {
          best = { cost: here, at: index };
        }
        return step;
      }),
    );
  });
  const lastSteps = steps.at(-1) ?? [];
  let at = lastSteps.reduce(
    (found, step, index) => (step.cost < (lastSteps[found]?.cost ?? Infinity) ? index : found),
    0,
  );
  if (!Number.isFinite(lastSteps[at]?.cost ?? Infinity)) {
    return undefined;
  }
  const chosen: [number, number][] = [];
  for (let space = expected.length - 1; space >= 0; space -= 1) {
    const gap = gaps[at];
    if (
      gap === undefined ||
      Math.abs((gap[0] + gap[1]) / 2 - (expected[space] ?? 0)) > farthest * mean
    ) {
      return undefined;
    }
    chosen.unshift(gap);
    at = steps[space]?.[at]?.from ?? -1;
  }
  return chosen;
}
