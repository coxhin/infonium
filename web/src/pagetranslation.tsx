import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
} from "react";
import {
  bodyPrintOn,
  percent,
  type AreaText,
  type BodyPrint,
  type Box,
  type Layout,
} from "./areas";
import { MarkedText } from "./blocks";
import type { PagePlace } from "./client";
import {
  columnsOf,
  fitted,
  flowsOf,
  leadingRatioOf,
  leadingsOf,
  type Fitting,
  type Flow,
  type Measure,
  type PageFacts,
  type Setting,
} from "./flows";
import {
  blankInto,
  coloursOf,
  evened,
  isBlank,
  paperOf,
  type Colours,
  type Drawn,
} from "./pagedrawing";
import { useSelectedBlock, type MarkState, type SelectionDrawing } from "./pagemarking";
import "./pagetranslation.css";

// How long a press waits, in milliseconds, to be told from the first of a double press.
const DOUBLE_PRESS = 250;
// An area is justified unless that would stretch a line other than its last by more than this
// part of the line's width: where the line's words take less than the rest.
const LEAST_FILLED_LINE = 0.85;
// While lines are measured, the areas of a page are set flush left.
const MEASURING_LINES = "data-measuring-lines";
const CITED = ".area[data-cited]";

// The colours read from each drawing of a page, by area: a translation that arrives block by
// block reads only the areas that are new.
const coloursRead = new WeakMap<Drawn, Map<string, Colours>>();

function coloursFrom(drawn: Drawn, texts: AreaText[]): ReadonlyMap<string, Colours> {
  const read = coloursRead.get(drawn) ?? new Map<string, Colours>();
  coloursRead.set(drawn, read);
  for (const text of texts) {
    if (!read.has(text.key)) {
      const colours = coloursOf(drawn.canvas, text.area.box);
      if (colours !== undefined) {
        read.set(text.key, colours);
      }
    }
  }
  return evened(texts, read);
}

// A length along a flow's run as a part of the page's width, which the text's sizes are
// given in so that the page scales without being set again.
function acrossWidth(flow: Flow, length: number, shape: number): number {
  return flow.vertical ? length : length * shape;
}

function cqw(part: number): string {
  return `${part * 100}cqw`;
}

// Whether an area's lines, set flush left, leave a line other than the last less filled than
// justifying it would bear. The words of a line are the pieces of text and the formulas that
// stand on it, told apart from the next line's by where they stand down the area.
function stretches(area: HTMLElement): boolean {
  const content = area.getBoundingClientRect();
  const padding = parseFloat(getComputedStyle(area).paddingInlineStart) * 2;
  const room = content.width - padding;
  const pieces: DOMRect[] = [];
  const walker = document.createTreeWalker(area, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    if (node.parentElement?.closest(".formula") === null) {
      range.selectNodeContents(node);
      pieces.push(...range.getClientRects());
    }
  }
  for (const formula of area.querySelectorAll(".formula")) {
    pieces.push(...formula.getClientRects());
  }
  const lines: { top: number; bottom: number; right: number }[] = [];
  for (const piece of pieces
    .filter((each) => each.width > 0)
    .sort((one, other) => one.top + one.bottom - (other.top + other.bottom))) {
    const middle = (piece.top + piece.bottom) / 2;
    const line = lines.at(-1);
    if (line !== undefined && middle <= line.bottom) {
      line.right = Math.max(line.right, piece.right);
    } else {
      lines.push({ top: piece.top, bottom: piece.bottom, right: piece.right });
    }
  }
  return lines
    .slice(0, -1)
    .some((line) => line.right - content.left - padding / 2 < LEAST_FILLED_LINE * room);
}

// What a page is told of its translation: the areas set over it, and what the document tells of
// every page, from which the page's columns, its body print and its leadings are worked out.
export type PageTexts = { texts: AreaText[]; layout: Layout; unspaced: boolean };

// One area of a flow: it begins at its own left side and runs to the flow's right, after the
// gap its room leaves above it and the shares of spare room the gap takes. Its text is set at
// its print's size times its flow's step, led as its flow's leading says of its print.
function TranslatedArea({
  text,
  flow,
  index,
  shape,
  spacing,
  colours,
  ragged,
  pointed,
  cited,
  marked,
  drawing,
}: {
  text: AreaText;
  flow: Flow;
  index: number;
  shape: number;
  spacing: number;
  colours: Colours | undefined;
  ragged: boolean;
  // Whether the area holds the translation of a block a passage pointed at runs through: it
  // is then marked whole, as the text view marks the translated block.
  pointed: boolean;
  // Whether it holds the translation of the first line pointed at on its page.
  cited: boolean;
  // How the strongest of the highlights that run through the area's block is drawn, if any
  // does; and the selection, which the area is drawn in where it takes text of its block.
  marked: MarkState | undefined;
  drawing: SelectionDrawing;
}) {
  const selected = useSelectedBlock(drawing, text.blockNumber);
  const { area, piece } = text;
  const size = area.vertical ? area.typeset.size : area.typeset.size * shape;
  const ratio = leadingRatioOf(text, spacing);
  const gap = acrossWidth(flow, flow.gaps[index] ?? 0, shape);
  const style = {
    "--size": cqw(size),
    marginInlineStart: area.vertical ? undefined : cqw(area.room.left - flow.left),
    marginBlockStart: `calc(${cqw(gap)} + var(--share) * ${flow.weights[index] ?? 0})`,
    lineHeight: `calc(var(--leading) * ${ratio})`,
    color: colours?.ink,
  } as CSSProperties;
  return (
    <div
      className={[
        "area",
        pointed ? "pointed" : "",
        marked !== undefined ? "marked" : "",
        selected ? "selected" : "",
      ]
        .filter(Boolean)
        .join(" ")}
      data-mark={marked}
      style={style}
      data-block-translated={text.blockNumber}
      data-kind={text.kind}
      data-ragged={ragged ? "" : undefined}
      data-cited={cited ? "" : undefined}
    >
      {text.marker !== undefined && <span className="marker">{text.marker}</span>}
      <MarkedText text={piece.text} marks={piece.marks} />
    </div>
  );
}

// A flow of areas set as one run of text over the paper of its room. Pressing a cut flow or
// focusing it shows its text whole; where the source is to be seen under the translation,
// pressing it shows the source under all its areas until it is pressed again.
function TranslatedFlow({
  flow,
  setting,
  ragged,
  shape,
  spacing,
  colours,
  revealing,
  pointedBlocks,
  cited,
  markedBlocks,
  drawing,
}: {
  flow: Flow;
  setting: Setting;
  ragged: ReadonlySet<string>;
  shape: number;
  spacing: number;
  colours: ReadonlyMap<string, Colours>;
  revealing: boolean;
  pointedBlocks: ReadonlySet<number>;
  cited: string | undefined;
  markedBlocks: ReadonlyMap<number, MarkState>;
  drawing: SelectionDrawing;
}) {
  const element = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  // A press turns the flow once it is plain that it selected nothing: a drag that selects its
  // text ends in a click too, and a double click begins with one.
  const turning = useRef<number>(undefined);
  useEffect(() => () => window.clearTimeout(turning.current), []);
  const pressed = (event: MouseEvent) => {
    window.clearTimeout(turning.current);
    if (event.detail > 1) {
      return;
    }
    turning.current = window.setTimeout(() => {
      const selection = getSelection();
      const selects =
        selection !== null &&
        !selection.isCollapsed &&
        element.current !== null &&
        selection.containsNode(element.current, true) &&
        selection.toString().trim() !== "";
      if (!selects) {
        setOpen((before) => !before);
      }
    }, DOUBLE_PRESS);
  };
  const { room } = flow;
  const paper = colours.get(flow.key)?.paper;
  const place: Box = flow.vertical
    ? { ...room, left: room.left - setting.growth, width: room.width + setting.growth }
    : {
        left: flow.left,
        top: room.top,
        width: flow.right - flow.left,
        height: room.height + setting.growth,
      };
  const style = {
    left: percent(place.left),
    top: percent(place.top),
    width: percent(place.width),
    height: percent(place.height),
    "--body": cqw(acrossWidth(flow, flow.print.size, shape)),
    "--leading": setting.leading,
    "--step": setting.step,
    "--share": cqw(acrossWidth(flow, setting.share, shape)),
    backgroundColor: paper,
    "--paper": paper,
  } as CSSProperties;
  const pressable = setting.cut || revealing;
  return (
    <div
      ref={element}
      className={flow.vertical ? "flow vertical" : "flow"}
      style={style}
      data-leading={setting.leading.toFixed(2)}
      data-step={setting.step.toFixed(2)}
      data-cut={setting.cut ? "" : undefined}
      data-open={open ? "" : undefined}
      tabIndex={pressable ? 0 : undefined}
      role={pressable ? "button" : undefined}
      aria-pressed={pressable ? open : undefined}
      aria-label={revealing ? "按下看这几段的原文" : undefined}
      onClick={pressable ? pressed : undefined}
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
      {flow.texts.map((text, index) => (
        <TranslatedArea
          key={text.key}
          text={text}
          flow={flow}
          index={index}
          shape={shape}
          spacing={spacing}
          colours={colours.get(text.key)}
          ragged={ragged.has(text.key)}
          pointed={pointedBlocks.has(text.blockNumber)}
          cited={cited === text.key}
          marked={markedBlocks.get(text.blockNumber)}
          drawing={drawing}
        />
      ))}
    </div>
  );
}

function overlaps(one: Box, other: Box): boolean {
  return (
    one.left < other.left + other.width &&
    other.left < one.left + one.width &&
    one.top < other.top + other.height &&
    other.top < one.top + one.height
  );
}

// The flows of a page with how each is fitted, and the areas set flush left.
type Fitted = { flows: Flow[]; fitting: Fitting; ragged: ReadonlySet<string> };

const NO_RAGGED: ReadonlySet<string> = new Set();

// The translation set over one drawn page, as flows. The colours and what is blank are read from
// the drawing; the flows are fitted when the page is drawn, when a block of the page arrives,
// when the fonts arrive and when the page's width changes. Where the source is to be seen under
// the translation, a flow pressed shows it.
export function PageTranslation({
  page,
  texts,
  shape,
  drawn,
  revealing,
  width,
  pointed,
  pointedBlocks,
  onMarked,
  markedBlocks,
  drawing,
}: {
  page: number;
  texts: PageTexts;
  shape: number;
  drawn: Drawn;
  revealing: boolean;
  width: number;
  // The lines of the page a passage pointed at runs through.
  pointed: PagePlace[];
  pointedBlocks: ReadonlySet<number>;
  // Told of the area that holds the first line pointed at, each time it is set.
  onMarked: (area: HTMLElement) => void;
  markedBlocks: ReadonlyMap<number, MarkState>;
  drawing: SelectionDrawing;
}) {
  const root = useRef<HTMLDivElement>(null);
  const { layout, unspaced } = texts;
  const body: BodyPrint = useMemo(() => bodyPrintOn(layout, page), [layout, page]);
  const leadings = useMemo(() => leadingsOf(body.spacing, unspaced), [body, unspaced]);
  const colours = useMemo(() => coloursFrom(drawn, texts.texts), [drawn, texts.texts]);
  const flows = useMemo(() => {
    const paperUnder = (text: AreaText) => colours.get(text.key)?.paper;
    const facts: PageFacts = {
      paperOf: (text) => {
        const read = colours.get(text.key);
        return read === undefined ? undefined : paperOf(read);
      },
      blank: (box, text) => {
        const paper = paperUnder(text);
        return paper !== undefined && isBlank(drawn.canvas, box, paper, body.size);
      },
      blankInto: (box, across, text) => {
        const paper = paperUnder(text);
        return paper === undefined ? 0 : blankInto(drawn.canvas, box, across, paper, body.size);
      },
    };
    const lines = (layout.boxes.get(page) ?? []).map((line) => line.box);
    return flowsOf(texts.texts, columnsOf(layout.runs.get(page) ?? []), lines, body, facts);
  }, [texts.texts, colours, drawn, layout, page, body]);
  const cited = useMemo(() => {
    const [first] = pointed;
    return first === undefined
      ? undefined
      : texts.texts.find((text) => text.area.lines.some((line) => overlaps(line, first)))?.key;
  }, [pointed, texts.texts]);

  // Fonts that come after the first fitting, a formula's above all, change what the text
  // takes, so the page is fitted again once they are in.
  const [fontsIn, setFontsIn] = useState(false);
  useEffect(() => {
    let current = true;
    void document.fonts.ready.then(() => current && setFontsIn(true));
    return () => {
      current = false;
    };
  }, []);

  const [fitting, setFitting] = useState<Fitted>();
  const current = fitting?.flows === flows ? fitting : undefined;
  useLayoutEffect(() => {
    const host = root.current;
    if (host === null) {
      return;
    }
    const elements = new Map(
      [...host.querySelectorAll<HTMLElement>(":scope > .flow")].map((element, index) => [
        flows[index],
        element,
      ]),
    );
    const set = (flow: Flow, values: { leading: number; step: number; share: number }) => {
      const element = elements.get(flow);
      element?.style.setProperty("--leading", String(values.leading));
      element?.style.setProperty("--step", String(values.step));
      element?.style.setProperty("--share", cqw(acrossWidth(flow, values.share, shape)));
    };
    const pageBox = host.getBoundingClientRect();
    const measure: Measure = (trials) => {
      trials.forEach((trial, flow) => set(flow, { ...trial, share: 0 }));
      const reaches = new Map<Flow, number>();
      trials.forEach((_, flow) => {
        const element = elements.get(flow);
        const last = element?.lastElementChild;
        if (element === undefined || last === null || last === undefined) {
          return;
        }
        const flowBox = element.getBoundingClientRect();
        const lastBox = last.getBoundingClientRect();
        reaches.set(
          flow,
          flow.vertical
            ? (flowBox.right - lastBox.left) / pageBox.width
            : (lastBox.bottom - flowBox.top) / pageBox.height,
        );
      });
      return reaches;
    };
    const result = fitted(flows, leadings, measure);
    flows.forEach((flow) => {
      const setting = result.settings.get(flow.key);
      if (setting !== undefined) {
        set(flow, setting);
      }
    });
    host.setAttribute(MEASURING_LINES, "");
    const ragged = new Set<string>();
    elements.forEach((element, flow) => {
      [...element.children].forEach((area, index) => {
        const text = flow?.texts[index];
        const across = text !== undefined && flow?.vertical === false;
        if (across && area instanceof HTMLElement && stretches(area)) {
          ragged.add(text.key);
        }
      });
    });
    host.removeAttribute(MEASURING_LINES);
    setFitting({ flows, fitting: result, ragged });
  }, [flows, leadings, shape, width, fontsIn]);

  useEffect(() => {
    const area = root.current?.querySelector<HTMLElement>(CITED);
    if (current !== undefined && area !== null && area !== undefined) {
      onMarked(area);
    }
  }, [current, cited, onMarked]);

  const first: Setting = {
    leading: leadings[0] ?? body.spacing,
    step: 1,
    growth: 0,
    cut: false,
    share: 0,
  };
  return (
    <div
      ref={root}
      className={revealing ? "page-translation revealing" : "page-translation"}
      data-leading={
        current !== undefined && flows.length > 0
          ? current.fitting.leading.toFixed(2)
          : undefined
      }
    >
      {flows.map((flow) => (
        <TranslatedFlow
          key={flow.key}
          flow={flow}
          setting={current?.fitting.settings.get(flow.key) ?? first}
          ragged={current?.ragged ?? NO_RAGGED}
          shape={shape}
          spacing={body.spacing}
          colours={colours}
          revealing={revealing}
          pointedBlocks={pointedBlocks}
          cited={cited}
          markedBlocks={markedBlocks}
          drawing={drawing}
        />
      ))}
    </div>
  );
}
