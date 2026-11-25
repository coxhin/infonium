// Where the sentences of a text begin and end, and which sentences of a translation stand for
// a stretch of its source. A block's translation is given whole, as one segment, so the part
// of it that says what a cited stretch of the source says is found by the sentences of the
// two: a translation keeps its source's sentences, one for one as a rule, and in their order.

// A stretch of a text, in code points from start to end.
export type Stretch = { start: number; end: number };

// Marks that end a sentence: in scripts written without spaces, wherever they stand; in
// others, where a space and the start of another sentence follow.
const UNSPACED_ENDS = new Set([..."。！？"]);
const SPACED_ENDS = new Set([...".!?"]);
// What may close a sentence after the mark that ends it: quotation marks and brackets.
const CLOSERS = new Set([..."\"'”’」』）)]】》"]);
// What a sentence may begin with after a space: a capital, a digit, an opening quotation
// mark or bracket, or a letter of a script without capitals.
const SENTENCE_START =
  /[\p{Lu}\p{Lt}\p{N}"'“‘(（[「『【《\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
const SPACE = /\s/u;
// Words a full stop follows inside a sentence: abbreviations a paper uses before a name, a
// number or a reference. Compared without case.
const ABBREVIATIONS = new Set(
  (
    "al e.g i.e etc vs cf fig figs eq eqs sec secs tab no nos ref refs ch dr mr ms prof st " +
    "approx resp incl appx def thm lem prop alg ed eds vol pp"
  ).split(" "),
);
const WORD_BEFORE = /([\p{L}.]+)$/u;

// Whether the full stop at this place ends a word that is never the last of a sentence: an
// abbreviation, or an initial.
function abbreviated(characters: string[], at: number): boolean {
  const before = characters.slice(Math.max(0, at - 12), at).join("");
  const word = WORD_BEFORE.exec(before)?.[1];
  if (word === undefined) {
    return false;
  }
  const bare = word.replace(/^\.+/, "").toLowerCase();
  return ABBREVIATIONS.has(bare) || /^\p{Lu}$/u.test(word);
}

// What a sentence holds: a letter or a digit. Marks alone, as an ellipsis in brackets that
// stands for words left out, go with the sentence after them.
const WORDED = /[\p{L}\p{N}]/u;

// The sentences of a text, each from its first character that is not a space to the end of
// its closing marks, in their order. A text with no mark that ends a sentence is one.
export function sentencesOf(text: string): Stretch[] {
  const characters = [...text];
  const sentences: Stretch[] = [];
  let start = 0;
  const close = (end: number) => {
    while (start < end && SPACE.test(characters[start] ?? "")) {
      start += 1;
    }
    if (start < end && WORDED.test(characters.slice(start, end).join(""))) {
      sentences.push({ start, end });
      start = end;
    }
  };
  for (let at = 0; at < characters.length; at += 1) {
    const character = characters[at] ?? "";
    // A line of its own, as a row of a table is, is a sentence of its own.
    if (character === "\n") {
      close(at);
      continue;
    }
    const unspaced = UNSPACED_ENDS.has(character);
    if (!unspaced && !SPACED_ENDS.has(character)) {
      continue;
    }
    let end = at + 1;
    while (
      end < characters.length &&
      (CLOSERS.has(characters[end] ?? "") || characters[end] === character)
    ) {
      end += 1;
    }
    if (unspaced) {
      close(end);
      at = end - 1;
      continue;
    }
    // A spaced mark ends a sentence only before a space and what may start another, and a full
    // stop not after an abbreviation or an initial.
    let next = end;
    while (next < characters.length && SPACE.test(characters[next] ?? "")) {
      next += 1;
    }
    const followed = next > end && SENTENCE_START.test(characters[next] ?? "");
    if (followed && !(character === "." && abbreviated(characters, at))) {
      close(end);
      at = end - 1;
    }
  }
  close(characters.length);
  // Marks after the last sentence go with it.
  const last = sentences.at(-1);
  if (last !== undefined && start < characters.length) {
    last.end = characters.length;
  }
  return sentences;
}

// The indices of the sentences a stretch of a text runs through, first and last.
function sentencesThrough(sentences: Stretch[], stretch: Stretch): [number, number] | undefined {
  const through = sentences
    .map((sentence, index) => ({ sentence, index }))
    .filter(({ sentence }) => sentence.start < stretch.end && stretch.start < sentence.end);
  const first = through[0];
  const last = through.at(-1);
  return first === undefined || last === undefined ? undefined : [first.index, last.index];
}

function lengthOf(stretch: Stretch): number {
  return stretch.end - stretch.start;
}

// Ways the sentences of a source and its translation stand for one another, as so many of the
// one for so many of the other, each with what it costs to take it: one for one costs nothing
// of itself, the others more as they are rarer.
const BEADS: { source: number; translation: number; cost: number }[] = [
  { source: 1, translation: 1, cost: 0 },
  { source: 1, translation: 2, cost: 2.3 },
  { source: 2, translation: 1, cost: 2.3 },
  { source: 2, translation: 2, cost: 4.4 },
  { source: 1, translation: 0, cost: 4.5 },
  { source: 0, translation: 1, cost: 4.5 },
];
// How much a difference in share weighs against a rarer way: the shares of a sentence and of
// its translation in their texts differ, as a rule, by a few hundredths.
const SHARE_WEIGHT = 400;

// Which sentences of a translation stand for which of its source: the cheapest way of pairing
// them in their order (Gale and Church's), where a pairing costs as much as its sentences' shares
// of their texts differ, and more as it is rarer. The result gives, for each source sentence,
// the translation sentences that stand for it with the others of its pairing.
function paired(source: Stretch[], translation: Stretch[]): [number, number][] {
  const sourceTotal = source.reduce((sum, sentence) => sum + lengthOf(sentence), 0) || 1;
  const translationTotal = translation.reduce((sum, sentence) => sum + lengthOf(sentence), 0) || 1;
  const rows = source.length + 1;
  const columns = translation.length + 1;
  const costs: number[] = new Array<number>(rows * columns).fill(Infinity);
  const ways: number[] = new Array<number>(rows * columns).fill(-1);
  costs[0] = 0;
  for (let i = 0; i < rows; i += 1) {
    for (let j = 0; j < columns; j += 1) {
      const here = costs[i * columns + j] ?? Infinity;
      if (here === Infinity) {
        continue;
      }
      BEADS.forEach((bead, way) => {
        const toI = i + bead.source;
        const toJ = j + bead.translation;
        if (toI >= rows || toJ >= columns) {
          return;
        }
        const sourceShare =
          source.slice(i, toI).reduce((sum, sentence) => sum + lengthOf(sentence), 0) / sourceTotal;
        const translationShare =
          translation.slice(j, toJ).reduce((sum, sentence) => sum + lengthOf(sentence), 0) /
          translationTotal;
        const cost = here + bead.cost + SHARE_WEIGHT * (sourceShare - translationShare) ** 2;
        if (cost < (costs[toI * columns + toJ] ?? Infinity)) {
          costs[toI * columns + toJ] = cost;
          ways[toI * columns + toJ] = way;
        }
      });
    }
  }
  // Each source sentence gets the translation sentences of its pairing, as first and last; a
  // sentence paired with none gets the place between its neighbours' as an empty pair.
  const pairs: [number, number][] = new Array<[number, number]>(source.length);
  let i = source.length;
  let j = translation.length;
  while (i > 0 || j > 0) {
    const way = ways[i * columns + j] ?? -1;
    const bead = BEADS[way];
    if (bead === undefined) {
      break;
    }
    for (let k = i - bead.source; k < i; k += 1) {
      pairs[k] = [j - bead.translation, j - 1];
    }
    i -= bead.source;
    j -= bead.translation;
  }
  return pairs;
}

// The stretch of a translation that stands for a stretch of its source: the sentences that
// stand for the source sentences the stretch runs through. Where those stand for none, the
// whole translation does.
export function translatedStretch(source: string, translation: string, stretch: Stretch): Stretch {
  const whole = { start: 0, end: [...translation].length };
  const sourceSentences = sentencesOf(source);
  const translationSentences = sentencesOf(translation);
  const through = sentencesThrough(sourceSentences, stretch);
  if (through === undefined || translationSentences.length === 0) {
    return whole;
  }
  // A translation that keeps its source's count of sentences keeps them one for one.
  const pairs =
    sourceSentences.length === translationSentences.length
      ? sourceSentences.map((_, index): [number, number] => [index, index])
      : paired(sourceSentences, translationSentences);
  const covered = pairs
    .slice(through[0], through[1] + 1)
    .filter((pair): pair is [number, number] => pair !== undefined && pair[0] <= pair[1]);
  const first = translationSentences[Math.min(...covered.map((pair) => pair[0]))];
  const last = translationSentences[Math.max(...covered.map((pair) => pair[1]))];
  return first === undefined || last === undefined ? whole : { start: first.start, end: last.end };
}
