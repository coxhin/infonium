import {
  useCallback,
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
  layoutOf,
  overlap,
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
  edgesOf,
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
  paperLevelsOf,
  type Colours,
  type Drawn,
} from "./pagedrawing";
import { useSelectedBlock, type MarkState, type SelectionDrawing } from "./pagemarking";
import "./pagetranslation.css";

// How long a press waits, in milliseconds, to be told from the first of a double press.
const DOUBLE_PRESS = 250;
// An area is justified unless that would widen the gaps of a line other than its last by more
// than this part of its type size each.
const MOST_WIDENING = 0.15;
// Two runs of text whose tops lie closer than this part of the type size stand on one line.
const SAME_LINE = 0.5;
// The places a browser widens to justify a line: characters of Han, kana and hangul and of
// their punctuation, and spaces.
const WIDENED =
  /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\u3000-\u303f\uff00-\uffef]|\s/gu;
// While a page is set, its text is set at its own size, not enlarged as a cut flow shown whole
// is; while lines are measured, its areas are set flush left.
const SETTING = "data-setting";
const MEASURING_LINES = "data-measuring-lines";
// A page whose width keeps changing, as while the reader zooms, keeps its setting, which scales
// with it, and is set again once its width has stood still this long, in milliseconds.
const WIDTH_SETTLES = 300;
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

function areasIn(flow: HTMLElement): NodeListOf<HTMLElement> {
  return flow.querySelectorAll<HTMLElement>(":scope > .area");
}

// A run of an area's text that stands on one line, or a formula, with where it stands, and
// whether it stands on the line itself rather than raised or lowered from it.
type Run = { top: number; bottom: number; right: number; text: string; level: boolean };

// The runs of a text node, each on one line: where the node's fragments begin a new line, the
// first character there is found by halving.
function runsOf(node: Text, range: Range, tolerance: number): { start: number; end: number }[] {
  range.selectNodeContents(node);
  const tops: number[] = [];
  for (const rect of range.getClientRects()) {
    if (tops.every((top) => Math.abs(top - rect.top) >= tolerance)) {
      tops.push(rect.top);
    }
  }
  tops.sort((one, other) => one - other);
  const topOf = (at: number) => {
    range.setStart(node, at);
    range.setEnd(node, Math.min(node.length, at + 1));
    return range.getClientRects()[0]?.top;
  };
  const starts = [0];
  for (const lineTop of tops.slice(1)) {
    let low = (starts.at(-1) ?? 0) + 1;
    let high = node.length;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      const top = topOf(middle);
      if (top !== undefined && top >= lineTop - tolerance) {
        high = middle;
      } else {
        low = middle + 1;
      }
    }
    starts.push(low);
  }
  return starts.map((start, index) => ({ start, end: starts[index + 1] ?? node.length }));
}

// Whether justifying an area would widen the gaps of a line other than its last by more than a
// part of its type size each. Its lines are set flush left to be measured: the width a line's
// words leave free is shared among its gaps, and a line with none has one. The lines are told
// by the runs of text set on them, and a raised or lowered run, or a formula, belongs to the
// line it stands across.
function stretches(area: HTMLElement): boolean {
  const content = area.getBoundingClientRect();
  const style = getComputedStyle(area);
  const size = parseFloat(style.fontSize);
  const start = content.left + parseFloat(style.paddingInlineStart);
  const room = content.width - parseFloat(style.paddingInlineStart) * 2;
  const range = document.createRange();
  const runs: Run[] = [];
  const walker = document.createTreeWalker(area, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    if (!(node instanceof Text) || node.parentElement?.closest(".formula") !== null) {
      continue;
    }
    const level = node.parentElement?.closest("sub, sup") === null;
    for (const run of runsOf(node, range, SAME_LINE * size)) {
      const text = node.data.slice(run.start, run.end);
      const words = text.trimEnd().length;
      range.setStart(node, run.start);
      range.setEnd(node, run.start + Math.max(words, 1));
      const rect = range.getBoundingClientRect();
      const right = words > 0 ? rect.right : start;
      runs.push({ top: rect.top, bottom: rect.bottom, right, text, level });
    }
  }
  for (const formula of area.querySelectorAll(".formula")) {
    for (const rect of formula.getClientRects()) {
      runs.push({ top: rect.top, bottom: rect.bottom, right: rect.right, text: "", level: false });
    }
  }
  const lines: Run[] = [];
  const byTop = (one: Run, other: Run) => one.top - other.top;
  for (const run of runs.filter((each) => each.level)) {
    const line = lines.find((each) => Math.abs(each.top - run.top) < SAME_LINE * size);
    if (line === undefined) {
      lines.push({ ...run });
    } else {
      line.right = Math.max(line.right, run.right);
      line.bottom = Math.max(line.bottom, run.bottom);
      line.text += run.text;
    }
  }
  for (const run of runs.filter((each) => !each.level)) {
    const across = (line: Run) =>
      Math.min(line.bottom, run.bottom) - Math.max(line.top, run.top);
    const line = [...lines].sort((one, other) => across(other) - across(one))[0];
    if (line === undefined || across(line) <= 0) {
      lines.push({ ...run });
    } else {
      line.right = Math.max(line.right, run.right);
      line.text += run.text;
    }
  }
  return lines
    .sort(byTop)
    .slice(0, -1)
    .some((line) => {
      const gaps = Math.max(1, line.text.trim().match(WIDENED)?.length ?? 0);
      return (room - (line.right - start)) / gaps > MOST_WIDENING * size;
    });
}

// What a page is told of its translation: the areas set over it, and what the document tells of
// every page, from which the page's columns, its body print and its leadings are worked out.
export type PageTexts = { areas: AreaText[]; layout: Layout; unspaced: boolean };

export const NO_PAGE_TEXTS: PageTexts = { areas: [], layout: layoutOf([]), unspaced: false };

// What each page of a document was last given, by the document's layout: a page whose areas
// are the very ones it was given before is given the same again, so that it is not set again
// when a translation under way is read again and its blocks are the same.
const pagesGiven = new WeakMap<Layout, Map<number, PageTexts>>();

export function pageTextsOf(
  onPages: Map<number, AreaText[]>,
  layout: Layout,
  unspaced: boolean,
): Map<number, PageTexts> {
  const before = pagesGiven.get(layout);
  const given = new Map<number, PageTexts>();
  onPages.forEach((areas, page) => {
    const kept = before?.get(page);
    const same =
      kept !== undefined &&
      kept.unspaced === unspaced &&
      kept.areas.length === areas.length &&
      kept.areas.every((area, index) => area === areas[index]);
    given.set(page, same ? kept : { areas, layout, unspaced });
  });
  pagesGiven.set(layout, given);
  return given;
}

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
    // A piece cut out of a line takes the flow's sides.
    marginInlineStart:
      area.vertical || flow.pieces.has(text.key) ? undefined : cqw(area.room.left - flow.room.left),
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
// pressing it shows the source under all its areas until it is pressed again. Whether it is
// open is its page's to keep, by its areas.
function TranslatedFlow({
  flow,
  setting,
  open,
  onTurn,
  onFocused,
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
  open: boolean;
  onTurn: (flow: Flow) => void;
  // Told when the flow takes the keyboard's focus, and when it gives it up while it stays on
  // show.
  onFocused: (flow: Flow | undefined) => void;
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
        onTurn(flow);
      }
    }, DOUBLE_PRESS);
  };
  const { room } = flow;
  const paper = colours.get(flow.key)?.paper;
  const place: Box = flow.vertical
    ? { ...room, left: room.left - setting.growth, width: room.width + setting.growth }
    : { ...room, height: room.height + setting.growth };
  // The paper is laid in the colour of the page's, and its edge in a part of the body's size.
  const paperStyle = (box: Box) => ({
    left: percent(box.left),
    top: percent(box.top),
    width: percent(box.width),
    height: percent(box.height),
    "--body": cqw(acrossWidth(flow, flow.print.size, shape)),
    backgroundColor: paper,
    "--paper": paper,
  });
  const edges = edgesOf(flow, setting.growth);
  const style = {
    ...paperStyle(place),
    "--edge-top": edges.top ? undefined : 0,
    "--edge-foot": edges.foot ? undefined : 0,
    "--leading": setting.leading,
    "--step": setting.step,
    "--share": cqw(acrossWidth(flow, setting.share, shape)),
  } as CSSProperties;
  const pressable = setting.cut || revealing;
  return (
    <>
      <div
        ref={element}
        className={flow.vertical ? "flow vertical" : "flow"}
        style={style}
        data-leading={setting.leading.toFixed(2)}
        data-step={setting.step.toFixed(2)}
        data-cut={setting.cut ? "" : undefined}
        data-open={open ? "" : undefined}
        data-bare={edges.top && edges.foot ? undefined : ""}
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
                  onTurn(flow);
                }
              }
            : undefined
        }
        onFocus={() => onFocused(flow)}
        onBlur={(event) => {
          // A flow taken off the page loses the focus too; only one still on show gave it up.
          const left = event.currentTarget;
          queueMicrotask(() => {
            if (left.isConnected) {
              onFocused(undefined);
            }
          });
        }}
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
      {flow.tails.map((tail, index) => (
        <div
          key={index}
          className="paper"
          aria-hidden
          data-open={open ? "" : undefined}
          style={paperStyle(tail) as CSSProperties}
        />
      ))}
    </>
  );
}

// A width that is taken once it has stood still for a moment; the first is taken at once.
function useSettled(width: number): number {
  const [settled, setSettled] = useState(width);
  useEffect(() => {
    const settling = window.setTimeout(() => setSettled(width), WIDTH_SETTLES);
    return () => window.clearTimeout(settling);
  }, [width]);
  return settled;
}

// The flows of a page with how each is fitted, how each area's flow was fitted, and the areas
// set flush left.
type Fitted = {
  flows: Flow[];
  fitting: Fitting;
  byArea: ReadonlyMap<string, Setting>;
  ragged: ReadonlySet<string>;
};

const NO_AREAS: ReadonlySet<string> = new Set();

// The translation set over one drawn page, as flows. The colours and what is blank are read from
// the drawing; the flows are fitted when the page is drawn, when a block of the page arrives,
// when the fonts arrive and when the page's width has changed. Where the source is to be seen
// under the translation, a flow pressed shows it.
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
  const colours = useMemo(() => coloursFrom(drawn, texts.areas), [drawn, texts.areas]);
  const flows = useMemo(() => {
    const paperUnder = (text: AreaText) => colours.get(text.key)?.paper;
    const facts: PageFacts = {
      paperOf: (text) => {
        const read = colours.get(text.key);
        return read === undefined ? undefined : paperLevelsOf(read);
      },
      blank: (box, text) => {
        const paper = paperUnder(text);
        return paper !== undefined && isBlank(drawn.canvas, box, paper, body.size);
      },
      blankInto: (box, from, text) => {
        const paper = paperUnder(text);
        return paper === undefined
          ? { blank: 0, drawn: true }
          : blankInto(drawn.canvas, box, from, paper, body.size);
      },
    };
    const lines = layout.boxes.get(page) ?? [];
    return flowsOf(texts.areas, columnsOf(layout.runs.get(page) ?? []), lines, body, facts);
  }, [texts.areas, colours, drawn, layout, page, body]);
  const cited = useMemo(() => {
    const [first] = pointed;
    return first === undefined
      ? undefined
      : texts.areas.find((text) => text.area.lines.some((line) => overlap(line, first) > 0))?.key;
  }, [pointed, texts.areas]);

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

  const settledWidth = useSettled(width);
  const [fitting, setFitting] = useState<Fitted>();
  const current = fitting?.flows === flows ? fitting : undefined;
  // What is open is kept by the areas, so that a flow stays open when a block arrives in it or
  // joins it to another: a flow is open while any of its areas is.
  const [openAreas, setOpenAreas] = useState<ReadonlySet<string>>(NO_AREAS);
  const turn = useCallback((flow: Flow) => {
    setOpenAreas((before) => {
      const open = flow.texts.some((text) => before.has(text.key));
      const after = new Set(before);
      flow.texts.forEach((text) => (open ? after.delete(text.key) : after.add(text.key)));
      return after;
    });
  }, []);
  // The first area of the flow that holds the keyboard's focus.
  const focused = useRef<string>(undefined);
  useLayoutEffect(() => {
    const host = root.current;
    if (host === null) {
      return;
    }
    const shown = [...host.querySelectorAll<HTMLElement>(":scope > .flow")];
    const elements = new Map<Flow, HTMLElement>();
    flows.forEach((flow, index) => {
      const element = shown[index];
      if (element !== undefined) {
        elements.set(flow, element);
      }
    });
    const set = (flow: Flow, values: { leading: number; step: number; share: number }) => {
      const element = elements.get(flow);
      element?.style.setProperty("--leading", String(values.leading));
      element?.style.setProperty("--step", String(values.step));
      element?.style.setProperty("--share", cqw(acrossWidth(flow, values.share, shape)));
    };
    host.setAttribute(SETTING, "");
    const pageBox = host.getBoundingClientRect();
    const measure: Measure = (trials) => {
      trials.forEach((trial, flow) => set(flow, { ...trial, share: 0 }));
      const reaches = new Map<Flow, number>();
      trials.forEach((_, flow) => {
        const element = elements.get(flow);
        const last = element === undefined ? undefined : [...areasIn(element)].at(-1);
        if (element === undefined || last === undefined) {
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
      areasIn(element).forEach((area, index) => {
        const text = flow.texts[index];
        if (text !== undefined && !flow.vertical && stretches(area)) {
          ragged.add(text.key);
        }
      });
    });
    host.removeAttribute(MEASURING_LINES);
    host.removeAttribute(SETTING);
    const byArea = new Map<string, Setting>();
    flows.forEach((flow) => {
      const setting = result.settings.get(flow.key);
      flow.texts.forEach((text) => setting !== undefined && byArea.set(text.key, setting));
    });
    setFitting({ flows, fitting: result, byArea, ragged });
  }, [flows, leadings, shape, settledWidth, fontsIn]);

  useEffect(() => {
    const area = root.current?.querySelector<HTMLElement>(CITED);
    if (current !== undefined && area !== null && area !== undefined) {
      onMarked(area);
    }
  }, [current, cited, onMarked]);

  // A flow that held the keyboard's focus and was taken off the page, as when a block arrives
  // at its top, hands it on to the flow that now holds its first area.
  useLayoutEffect(() => {
    const kept = focused.current;
    const host = root.current;
    if (current === undefined || kept === undefined || host === null) {
      return;
    }
    const active = document.activeElement;
    if (active !== null && active !== document.body && host.contains(active)) {
      return;
    }
    if (active !== null && active !== document.body) {
      focused.current = undefined;
      return;
    }
    const index = current.flows.findIndex((flow) => flow.texts.some((text) => text.key === kept));
    host.querySelectorAll<HTMLElement>(":scope > .flow")[index]?.focus({ preventScroll: true });
  }, [current]);

  const first: Setting = {
    leading: leadings[0] ?? body.spacing,
    step: 1,
    growth: 0,
    cut: false,
    share: 0,
  };
  // Until the flows are fitted again, each keeps on show the setting its areas had.
  const settingOf = (flow: Flow): Setting =>
    (current === undefined
      ? flow.texts.map((text) => fitting?.byArea.get(text.key)).find(Boolean)
      : current.fitting.settings.get(flow.key)) ?? first;
  const shownLeading = (current ?? fitting)?.fitting.leading;
  return (
    <div
      ref={root}
      className={revealing ? "page-translation revealing" : "page-translation"}
      data-leading={
        shownLeading !== undefined && flows.length > 0 ? shownLeading.toFixed(2) : undefined
      }
    >
      {flows.map((flow) => (
        <TranslatedFlow
          key={flow.key}
          flow={flow}
          setting={settingOf(flow)}
          open={flow.texts.some((text) => openAreas.has(text.key))}
          onTurn={turn}
          onFocused={(shown) => {
            focused.current = shown?.texts[0]?.key;
          }}
          ragged={(current ?? fitting)?.ragged ?? NO_AREAS}
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
