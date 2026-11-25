// Whether justifying an area of a page's translation would stretch its lines: the measure of
// how much justifying would widen the gaps of each line, taken on the area as it is set.

// An area is justified unless that would widen the gaps of a line other than its last by more
// than this part of its type size each.
const MOST_WIDENING = 0.15;
// Two runs of text whose tops lie closer than this part of the type size stand on one line.
const SAME_LINE = 0.5;
// The places a browser widens to justify a line: characters of Han, kana and hangul and of
// their punctuation, and spaces.
const WIDENED =
  /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\u3000-\u303f\uff00-\uffef]|\s/gu;

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
export function stretches(area: HTMLElement): boolean {
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
      // A run of white space alone shows nothing, and is no line.
      const text = node.data.slice(run.start, run.end);
      const words = text.trimEnd().length;
      if (text.trim() === "") {
        continue;
      }
      range.setStart(node, run.start);
      range.setEnd(node, run.start + words);
      const rect = range.getBoundingClientRect();
      runs.push({ top: rect.top, bottom: rect.bottom, right: rect.right, text, level });
    }
  }
  for (const formula of area.querySelectorAll(".formula")) {
    for (const rect of formula.getClientRects()) {
      runs.push({ top: rect.top, bottom: rect.bottom, right: rect.right, text: "", level: false });
    }
  }
  // The lines are those of the text set level, or of all the text where none is; a raised or
  // lowered run, or a formula, belongs to the line whose middle is nearest its own, and makes
  // no line of its own.
  const lines: Run[] = [];
  const byTop = (one: Run, other: Run) => one.top - other.top;
  const leveled = runs.some((run) => run.level);
  for (const run of runs.filter((each) => each.level || !leveled)) {
    const line = lines.find((each) => Math.abs(each.top - run.top) < SAME_LINE * size);
    if (line === undefined) {
      lines.push({ ...run });
    } else {
      line.right = Math.max(line.right, run.right);
      line.bottom = Math.max(line.bottom, run.bottom);
      line.text += run.text;
    }
  }
  const middleOf = (run: Run) => (run.top + run.bottom) / 2;
  for (const run of runs.filter((each) => leveled && !each.level)) {
    const off = (line: Run) => Math.abs(middleOf(line) - middleOf(run));
    const line = [...lines].sort((one, other) => off(one) - off(other))[0];
    if (line !== undefined) {
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
