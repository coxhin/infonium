import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { AreaText, Box } from "./areas";
import { MarkedText } from "./blocks";
import "./pagetranslation.css";

const PLAIN_SPACING = 1.25;
// The line spacing of the source is kept between these, as a part of the size of the text.
const SPACING_BOUNDS = [1, 2.4] as const;
// Text is made smaller to fit its area, but never below this part of the source's size.
export const FLOOR = 0.6;
const FITTING_STEPS = 7;
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
// the same paper take the middle of their inks.
function evened(
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
  const evenedColours = new Map<string, Colours>();
  for (const text of texts) {
    const colours = read.get(text.key);
    if (colours !== undefined) {
      const ink = middles.get(groupOf(text, colours)) ?? colours.ink;
      evenedColours.set(text.key, { paper: colours.paper, ink });
    }
  }
  return evenedColours;
}

// Papers alike to the eye are one paper: each channel is taken to eight levels.
const PAPER_LEVEL = 32;

function groupOf(text: AreaText, colours: Colours): string {
  const paper = (colours.paper.match(/\d+/g) ?? []).map((channel) =>
    Math.floor(Number(channel) / PAPER_LEVEL),
  );
  return `${text.kind}|${paper.join(",")}`;
}

function brightnessOf(colour: string): number {
  return (colour.match(/\d+/g) ?? []).reduce((sum, channel) => sum + Number(channel), 0);
}

function percent(fraction: number): string {
  return `${fraction * 100}%`;
}

type Rgb = [number, number, number];

function distance(first: Rgb, second: Rgb): number {
  return Math.hypot(first[0] - second[0], first[1] - second[1], first[2] - second[2]);
}

function rgb([red, green, blue]: Rgb): string {
  return `rgb(${red}, ${green}, ${blue})`;
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
  const counts = new Map<string, { colour: Rgb; count: number }>();
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
      const counted = counts.get(level) ?? { colour, count: 0 };
      counted.count += 1;
      counts.set(level, counted);
    }
  }
  const ranked = [...counts.values()].sort((first, second) => second.count - first.count);
  const paper = ranked[0]?.colour;
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

// One area of a page with the part of a translation set in it: at the size of the source's
// lines, made smaller only as far as it must be to fit, and cut, with a mark, where it does not
// fit at the floor. Pressing it or focusing it shows the text whole; where the source is to be
// seen under the translation, pressing it shows the source until it is pressed again.
function TranslatedArea({
  text,
  shape,
  colours,
  revealing,
  width,
}: {
  text: AreaText;
  shape: number;
  colours: Colours | undefined;
  revealing: boolean;
  width: number;
}) {
  const element = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState<{ ratio: number; cut: boolean }>({ ratio: 1, cut: false });
  const [open, setOpen] = useState(false);
  const { area, piece } = text;
  const { size: lineSize, pitch } = area.typeset;
  // The size of the text, and its leading, as parts of the page's width.
  const size = area.vertical ? lineSize : lineSize * shape;
  const spacing =
    pitch === undefined
      ? PLAIN_SPACING
      : Math.min(
          SPACING_BOUNDS[1],
          Math.max(SPACING_BOUNDS[0], (pitch * (area.vertical ? 1 : shape)) / size),
        );
  // The source's lines hold their leading between them; the room their text takes is that of
  // its lines with the leading of each, half above the first and half below the last.
  const leading = (spacing - 1) * size;

  useLayoutEffect(() => {
    const box = element.current;
    if (box === null) {
      return;
    }
    const overflows = (ratio: number) => {
      box.style.setProperty("--fit", String(ratio));
      return area.vertical
        ? box.scrollWidth > box.clientWidth + 1
        : box.scrollHeight > box.clientHeight + 1;
    };
    let found: { ratio: number; cut: boolean } = { ratio: 1, cut: false };
    if (overflows(1)) {
      if (overflows(FLOOR)) {
        found = { ratio: FLOOR, cut: true };
      } else {
        let low = FLOOR;
        let high = 1;
        for (let step = 0; step < FITTING_STEPS; step += 1) {
          const middle = (low + high) / 2;
          if (overflows(middle)) {
            high = middle;
          } else {
            low = middle;
          }
        }
        found = { ratio: low, cut: false };
      }
    }
    box.style.setProperty("--fit", String(found.ratio));
    setFit(found);
  }, [piece, size, spacing, width]);

  const half = `${(leading / 2) * 100}cqw`;
  const whole = `${leading * 100}cqw`;
  const style = {
    left: area.vertical ? `calc(${percent(area.box.left)} - ${half})` : percent(area.box.left),
    top: area.vertical ? percent(area.box.top) : `calc(${percent(area.box.top)} - ${half})`,
    width: area.vertical ? `calc(${percent(area.box.width)} + ${whole})` : percent(area.box.width),
    height: area.vertical ? percent(area.box.height) : `calc(${percent(area.box.height)} + ${whole})`,
    "--size": `${size * 100}cqw`,
    "--fit": fit.ratio,
    lineHeight: spacing,
    background: colours?.paper,
    "--paper": colours?.paper,
    color: colours?.ink,
  } as CSSProperties;
  const pressable = fit.cut || revealing;
  return (
    <div
      ref={element}
      className={area.vertical ? "area vertical" : "area"}
      style={style}
      data-fit={fit.ratio.toFixed(2)}
      data-cut={fit.cut ? "" : undefined}
      data-open={open ? "" : undefined}
      data-block-translated={text.blockNumber}
      data-kind={text.kind}
      tabIndex={pressable ? 0 : undefined}
      role={pressable ? "button" : undefined}
      aria-pressed={pressable ? open : undefined}
      aria-label={revealing ? "按下看这一段的原文" : undefined}
      onClick={pressable ? () => setOpen(!open) : undefined}
      onKeyDown={
        pressable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                setOpen(!open);
              }
            }
          : undefined
      }
    >
      {text.marker !== undefined && <span className="marker">{text.marker}</span>}
      <MarkedText text={piece.text} marks={piece.marks} />
    </div>
  );
}

// The translation set over one drawn page. The colours are read from the drawing once it is
// there; until then the areas wait. Where the source is to be seen under the translation, an
// area pressed shows it.
export function PageTranslation({
  texts,
  shape,
  drawn,
  revealing,
  width,
}: {
  texts: AreaText[];
  shape: number;
  drawn: Drawn;
  revealing: boolean;
  width: number;
}) {
  const [read, setRead] = useState<ReadonlyMap<string, Colours>>(new Map());
  useEffect(() => {
    setRead((before) => {
      const after = new Map(before);
      for (const text of texts) {
        if (!after.has(text.key)) {
          const colours = coloursOf(drawn.canvas, text.area.box);
          if (colours !== undefined) {
            after.set(text.key, colours);
          }
        }
      }
      return after;
    });
  }, [texts, drawn]);
  const colours = useMemo(() => evened(texts, read), [texts, read]);
  return (
    <div className={revealing ? "page-translation revealing" : "page-translation"}>
      {texts.map((text) => (
        <TranslatedArea
          key={text.key}
          text={text}
          shape={shape}
          colours={colours.get(text.key)}
          revealing={revealing}
          width={width}
        />
      ))}
    </div>
  );
}
