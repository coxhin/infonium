import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { bodyPrintOn, overlap, type AreaText, type BodyPrint } from "./areas";
import type { PagePlace } from "./client";
import { leadingsOf, type Setting } from "./fitting";
import { columnsOf, flowsOf, type Flow, type PageFacts } from "./flows";
import {
  blankInto,
  coloursOf,
  evened,
  isBlank,
  paperLevelsOf,
  type Colours,
  type Drawn,
} from "./pagedrawing";
import { usePageFitting } from "./pagefitting";
import type { MarkState, SelectionDrawing } from "./pagemarking";
import type { PageTexts } from "./pagetexts";
import { TranslatedFlow } from "./translatedflow";
import "./pagetranslation.css";

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
      paperLevelsOf: (text) => {
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

  const fitting = usePageFitting(root, flows, leadings, shape, width);
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
