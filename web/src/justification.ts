// Whether justifying an area of a page's translation would stretch its lines: the measure of
// how much justifying would widen the gaps of each line, taken on the area as it is set.

// An area is justified unless that would widen the gaps of a line other than its last by more
// than this part of its type size each.
const MOST_WIDENING = 0.15;
// Two parts of text whose tops lie closer than this part of the type size stand on one line.
const SAME_LINE = 0.5;
// The places a browser widens to justify a line: characters of Han, kana and hangul and of
// their punctuation, and spaces.
const WIDENED =
  /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}\u3000-\u303f\uff00-\uffef]|\s/gu;

// A part of an area's text that stands on one line, or a formula, with where it stands, and
// whether it stands on the line itself rather than raised or lowered from it.
type LinePart = { top: number; bottom: number; right: number; text: string; level: boolean };

// The parts of a text node, each on one line: where the node's fragments begin a new line, the
// first character there is found by halving.
function linePartsOf(
  node: Text,
  range: Range,
  tolerance: number,
): { start: number; end: number }[] {
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
// by the parts of text set on them, and a raised or lowered part, or a formula, belongs to the
// line it stands across.
export function stretches(area: HTMLElement): boolean {
  const content = area.getBoundingClientRect();
  const style = getComputedStyle(area);
  const size = parseFloat(style.fontSize);
  const start = content.left + parseFloat(style.paddingInlineStart);
  const room = content.width - parseFloat(style.paddingInlineStart) * 2;
  const range = document.createRange();
  const parts: LinePart[] = [];
  const walker = document.createTreeWalker(area, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    if (!(node instanceof Text) || node.parentElement?.closest(".formula") !== null) {
      continue;
    }
    const level = node.parentElement?.closest("sub, sup") === null;
    for (const part of linePartsOf(node, range, SAME_LINE * size)) {
      // A part of white space alone shows nothing, and is no line.
      const text = node.data.slice(part.start, part.end);
      const words = text.trimEnd().length;
      if (text.trim() === "") {
        continue;
      }
      range.setStart(node, part.start);
      range.setEnd(node, part.start + words);
      const rect = range.getBoundingClientRect();
      parts.push({ top: rect.top, bottom: rect.bottom, right: rect.right, text, level });
    }
  }
  for (const formula of area.querySelectorAll(".formula")) {
    for (const rect of formula.getClientRects()) {
      parts.push({ top: rect.top, bottom: rect.bottom, right: rect.right, text: "", level: false });
    }
  }
  // The lines are those of the text set level, or of all the text where none is; a raised or
  // lowered part, or a formula, belongs to the line whose middle is nearest its own, and makes
  // no line of its own.
  const lines: LinePart[] = [];
  const byTop = (one: LinePart, other: LinePart) => one.top - other.top;
  const leveled = parts.some((part) => part.level);
  for (const part of parts.filter((each) => each.level || !leveled)) {
    const line = lines.find((each) => Math.abs(each.top - part.top) < SAME_LINE * size);
    if (line === undefined) {
      lines.push({ ...part });
    } else {
      line.right = Math.max(line.right, part.right);
      line.bottom = Math.max(line.bottom, part.bottom);
      line.text += part.text;
    }
  }
  const middleOf = (part: LinePart) => (part.top + part.bottom) / 2;
  for (const part of parts.filter((each) => leveled && !each.level)) {
    const off = (line: LinePart) => Math.abs(middleOf(line) - middleOf(part));
    const line = [...lines].sort((one, other) => off(one) - off(other))[0];
    if (line !== undefined) {
      line.right = Math.max(line.right, part.right);
      line.text += part.text;
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
