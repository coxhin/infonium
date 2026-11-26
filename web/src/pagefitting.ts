import { useEffect, useLayoutEffect, useState, type RefObject } from "react";
import { fitted, type Fitting, type Measure, type Setting } from "./fitting";
import { acrossWidth, cqw } from "./flowlengths";
import type { Flow } from "./flows";
import { stretches } from "./justification";

// When and how the flows of a drawn page are fitted: their text is measured as the browser
// lays it out, the page's fitting is worked out, and the areas whose justified lines would
// stretch are found; again when a block of the page arrives, when the fonts arrive and once
// the page's width has stood still.

// While a page is set, its text is set at its own size, not enlarged as a cut flow shown whole
// is; while lines are measured, its areas are set flush left.
const SETTING = "data-setting";
const MEASURING_LINES = "data-measuring-lines";
// A page whose width keeps changing, as while the reader zooms, keeps its setting, which scales
// with it, and is set again once its width has stood still this long, in milliseconds. A page
// made wider is drawn again sharper then too.
export const WIDTH_SETTLES = 300;

function areasIn(flow: HTMLElement): NodeListOf<HTMLElement> {
  return flow.querySelectorAll<HTMLElement>(":scope > .area");
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
export type Fitted = {
  flows: Flow[];
  fitting: Fitting;
  byArea: ReadonlyMap<string, Setting>;
  ragged: ReadonlySet<string>;
};

// The fitting of the flows drawn in a page's translation, the last worked out.
export function usePageFitting(
  root: RefObject<HTMLDivElement | null>,
  flows: Flow[],
  leadings: number[],
  shape: number,
  width: number,
): Fitted | undefined {
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
  }, [root, flows, leadings, shape, settledWidth, fontsIn]);
  return fitting;
}
