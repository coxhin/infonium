import unicodedata
from bisect import bisect_left
from enum import StrEnum
from itertools import pairwise

from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.anchor import Anchor, comparable
from infonium.document import (
    CELL_SEPARATOR,
    HTML_MEDIA_TYPE,
    ROW_SEPARATOR,
    WORD_GAP,
    Block,
    Document,
    Paragraph,
    Table,
    document_of,
    is_unspaced,
)
from infonium.passage import Found, Passage, Standing

# A source read twice, the second document made from the first by known operations, so that a
# judgment knows where every passage of the first went, or that its words are gone; and the
# rule that judges where a search says a passage stands.
#
# A document is a run of units: sentences, lines of a table, formulas, sentences of a script
# written without spaces. A unit is words with the gaps between them; a word is never cut, so
# a passage of whole words maps word for word onto the second document.
ITEM = "d" * 64
SNAPSHOT = "0" * 64
FIRST_EXTRACTOR = "first-reading"
SECOND_EXTRACTOR = "second-reading"
UNIT_GAP = WORD_GAP
UNSPACED_GAP = ""
# What a second reading may put between two words instead of one space, all white space that
# anchor.comparable reads as one.
SPACINGS = (WORD_GAP, "  ", "\n", "\N{NO-BREAK SPACE}", " \t ")
INVISIBLES = ("\N{SOFT HYPHEN}", "\N{ZERO WIDTH SPACE}")
STRAIGHT_TO_TYPESET = (
    ('"', "\N{LEFT DOUBLE QUOTATION MARK}"),
    ('"', "\N{RIGHT DOUBLE QUOTATION MARK}"),
    ("'", "\N{RIGHT SINGLE QUOTATION MARK}"),
    ("-", "\N{EN DASH}"),
)
# How a second recognition of a scanned page reads a word otherwise: letters for letters, never
# a digit for a letter, since words that differ in a digit are other words.
MISREADINGS = (
    ("rn", "m"),
    ("m", "rn"),
    ("e", "c"),
    ("cl", "d"),
    ("l", "i"),
    ("h", "b"),
    ("a", "o"),
)
LIGATURES = (("fi", "\N{LATIN SMALL LIGATURE FI}"), ("fl", "\N{LATIN SMALL LIGATURE FL}"))
LINE_END_HYPHEN = "-\n"
PUNCTUATION_MORE = ","
SENTENCE_END = "."
UNSPACED_SENTENCE_END = "\N{IDEOGRAPHIC FULL STOP}"
UNSPACED_PUNCTUATION_MORE = "\N{IDEOGRAPHIC COMMA}"
# Words that every sentence draws on, in several scripts, with quotation marks, dashes,
# accents composed and not, and characters outside the Basic Multilingual Plane.
COMMON_WORDS = (
    "the", "of", "and", "a", "in", "to", "was", "ferry", "harbour", "ledger", "night", "winter",
    "keeper", "town", "late", "fog", '"crossing"', "keeper's", "north-east", "1850-1860",
    "café", "naïve", "Zürich", "straße", "λόγος", "ἀρχῇ", "город", "и", "שלום", "مرحبا",
    "\N{MATHEMATICAL FRAKTUR CAPITAL U}\N{MATHEMATICAL FRAKTUR SMALL N}",
    "\N{GRINNING FACE}", "\U0002000b\U0002000c", "e\N{COMBINING ACUTE ACCENT}te",
    "fifty", "flood",
)  # fmt: skip
UNSPACED_WORDS = ("港", "の", "帳簿", "は", "町", "図書館", "に", "今も", "保管", "されている")
# Words that make each unit of a document its own: no two are alike, and each differs from any
# other in at least three of its four consonants.
RARE_CONSONANTS = "bcdfghjklmnpqrstv"
RARE_VOWELS = "aeiou"
RARE_HAN = "鷗鶯鸞鴻鵬鷺鶴鳩鵜鷲鴨鷹燕雀鴉鳳鵲"
RARE_WORDS = len(RARE_CONSONANTS) ** 2
SENTENCE_LETTERS = 40
ROW_VALUES = ("4.5", "7", "12", "0.3", "3.25")
# The values of a line put in, like none of a table's own.
FRESH_ROW_VALUES = ("88.1", "905", "61.75", "0.004")
PLAIN_ROW_VALUES = ("1", "2", "3")
# Lists of references built on one template: entries differ in names and numbers.
SURNAMES = ("Brenn", "Okafor", "Lindqvist", "Moreau", "Tanaka", "Haddad")
JOURNALS = ("Harbour Studies", "Journal of Tides", "Annals of Ferries")
REFERENCE_TITLE = ("On", "the", "night", "ferry", "of", "the", "harbour")
FIRST_LABEL = 10
FRESH_LABEL = 900
# Sentences that a document says again and again, long and short.
BOILERPLATE = (
    "See the notes at the end of this chapter for the sources of these figures.",
    "Continued on the next page.",
    "Figure 3.",
    "Table 2 sums the rows above it.",
)
FORMULA = ("$\\frac{{{rare}}}{{3}}", "=", "\\int_0^1", "x^2\\,dx$")
# The rule's bounds, well inside the search's own (a passage is distinct from 40 letters, and
# words that differ a little keep four in five of its letters). A passage held once in each
# revision is found by its letters from DISTINCT_LETTERS, and held to its surroundings below
# SHORT_LETTERS; one of words that differ is held to be found when they keep NEAR_ALIKE of its
# letters. A passage is told by its surroundings when SURROUNDINGS letters on each side stand
# again as they stood, and the surroundings are changed when no more than CHANGED_SIDE of the
# TELLING_SIDE letters next to it on either side stand at their distance again. A passage
# found in words that differ begins and ends within NEAR_SLACK letters of where its words
# stand, and covers NEAR_COVER of them when it must be found.
SHORT_LETTERS = 30
DISTINCT_LETTERS = 60
NEAR_ALIKE = 0.9
LETTERS_A_CHANGE = 25
SURROUNDINGS = 64
TELLING_SIDE = 16
CHANGED_SIDE = 0.25
# A copy of a passage's words whose RIVAL_SIDE letters on each side stand at their distance as
# they stood around the passage, more than RIVAL_LIKENESS of them in sum over both sides, may
# leave the passage's place in doubt.
RIVAL_SIDE = 32
RIVAL_LIKENESS = 1.0
RIVAL_SIDE_LIKENESS = 0.5
# A copy is clearly another place when no more than this share of the RIVAL_SIDE letters, and
# of the SURROUNDINGS letters, on either of its sides stands as around the passage.
CLEARLY_OTHER = 0.25
# Words found for a passage that keep fewer of its letters than this are other words; between
# this and NEAR_ALIKE lies the edge of the search's bound, where either answer is right.
EDGE_LIKENESS = 0.7
# Another stretch this like a passage leaves a place of words that differ a little in doubt; a
# letter no text holds stands in for the place itself.
RIVAL_NEAR = 0.6
MASKED = "\x00"
NEAR_SLACK = 3
NEAR_COVER = 0.8
UNSPACED_WEIGHT = 3
# How often a unit of a document is one said before in it.
SAID_AGAIN_ONE_IN = 10
SOMETIMES_SAID_AGAIN = st.integers(0, SAID_AGAIN_ONE_IN - 1).map(lambda chance: chance == 0)
NEVER_SAID_AGAIN = st.just(False)


class UnitKind(StrEnum):
    SENTENCE = "sentence"
    UNSPACED = "unspaced"
    FORMULA = "formula"
    BOILERPLATE = "boilerplate"
    REFERENCE = "reference"
    ROW = "row"
    PLAIN_ROW = "plain_row"


ROW_KINDS = frozenset({UnitKind.ROW, UnitKind.PLAIN_ROW})
# Units whose words a document holds once, so that one changed a little is like no other.
OWN_KINDS = frozenset({UnitKind.SENTENCE, UnitKind.UNSPACED, UnitKind.FORMULA})


class Change(StrEnum):
    KEPT = "kept"
    RETYPED = "retyped"
    VARIED = "varied"
    RENUMBERED = "renumbered"
    REMOVED = "removed"
    REWRITTEN = "rewritten"


SURVIVING = frozenset({Change.KEPT, Change.RETYPED, Change.VARIED, Change.RENUMBERED})


class Unit(BaseModel):
    kind: UnitKind
    words: list[str]
    # The white space between each word and the next.
    gaps: list[str]


class Cut(BaseModel):
    # Where a document's blocks end: before a unit, or inside one before a word of it; and
    # what joins a unit to the one before it in a block: a space, or nothing where two blocks
    # were joined with nothing between them.
    before_unit: bool
    before_word: int | None
    joiner: str = UNIT_GAP


class WordPlace(BaseModel):
    block: int
    start: int
    end: int


class Laid(BaseModel):
    document: Document
    # Where each word of each unit stands.
    places: list[list[WordPlace]]


class Revised(BaseModel):
    first_units: list[Unit]
    second_units: list[Unit]
    # The unit of the first document each unit of the second was made from, word for word.
    origins: list[int | None]
    changes: list[Change]
    first: Laid
    second: Laid


class WordSpan(BaseModel):
    # A passage of whole words, from a word of one unit to a word of the same unit or a later
    # one, both included.
    first_unit: int
    first_word: int
    last_unit: int
    last_word: int


class Letters(BaseModel):
    # A document's text as a passage is looked for in it: the characters anchor.comparable
    # keeps, white space aside, each with the block and offset it came from.
    text: str
    places: list[tuple[int, int]]

    def span(self, block: int, start: int, end: int) -> tuple[int, int]:
        return bisect_left(self.places, (block, start)), bisect_left(self.places, (block, end))


class Coiner:
    # Coins the words that make units their own, none twice in a document.
    def __init__(self, offset: int, stride: int) -> None:
        self.offset = offset
        self.stride = stride
        self.coined = 0
        self.label = FIRST_LABEL
        self.fresh_label = FRESH_LABEL

    def number(self) -> int:
        self.coined += 1
        return (self.offset + self.coined * self.stride) % RARE_WORDS

    def rare(self) -> str:
        # Consonants as the four places of a code of distance three: two numbers that differ
        # give words that differ in at least three of them.
        a, b = divmod(self.number(), len(RARE_CONSONANTS))
        consonant, vowel = RARE_CONSONANTS, RARE_VOWELS
        return (
            consonant[a]
            + vowel[(a + b) % len(vowel)]
            + consonant[b]
            + vowel[(a + 3 * b) % len(vowel)]
            + consonant[(a + b) % len(consonant)]
            + vowel[b % len(vowel)]
            + consonant[(a + 2 * b) % len(consonant)]
        )

    def han(self) -> str:
        a, b = divmod(self.number(), len(RARE_CONSONANTS))
        return RARE_HAN[a] + RARE_HAN[b] + RARE_HAN[(a + b) % len(RARE_HAN)]

    def row_label(self, fresh: bool) -> str:
        if fresh:
            self.fresh_label += 7
            return f"r{self.fresh_label}"
        self.label += 1
        return f"r{self.label}"


def letters_of(text: str) -> str:
    return comparable(text, ignored=set()).text.replace(WORD_GAP, "")


def weight(letters: str) -> int:
    return sum(UNSPACED_WEIGHT if is_unspaced(letter) else 1 for letter in letters)


def unit_letters(unit: Unit) -> str:
    return letters_of("".join(unit.words))


def spaced(kind: UnitKind, words: list[str]) -> Unit:
    gap = {UnitKind.UNSPACED: UNSPACED_GAP}.get(
        kind, CELL_SEPARATOR if kind in ROW_KINDS else WORD_GAP
    )
    return Unit(kind=kind, words=words, gaps=[gap] * (len(words) - 1))


@st.composite
def coiners(draw: st.DrawFn) -> Coiner:
    stride = draw(st.integers(1, RARE_WORDS - 1).filter(lambda step: step % len(RARE_CONSONANTS)))
    return Coiner(offset=draw(st.integers(0, RARE_WORDS - 1)), stride=stride)


@st.composite
def sentences(draw: st.DrawFn, coiner: Coiner) -> Unit:
    words = draw(st.lists(st.sampled_from(COMMON_WORDS), min_size=4, max_size=10))
    for rare in (coiner.rare(), coiner.rare()):
        words.insert(draw(st.integers(0, len(words))), rare)
    filler = iter(COMMON_WORDS)
    while weight(letters_of("".join(words))) < SENTENCE_LETTERS:
        words.append(next(filler))
    words[-1] += SENTENCE_END
    return spaced(UnitKind.SENTENCE, words)


@st.composite
def unspaced_sentences(draw: st.DrawFn, coiner: Coiner) -> Unit:
    words = draw(st.lists(st.sampled_from(UNSPACED_WORDS), min_size=4, max_size=10))
    words.insert(draw(st.integers(0, len(words))), coiner.han())
    words[-1] += UNSPACED_SENTENCE_END
    return spaced(UnitKind.UNSPACED, words)


@st.composite
def rows(draw: st.DrawFn, coiner: Coiner, fresh: bool = False) -> Unit:
    values = draw(
        st.lists(st.sampled_from(FRESH_ROW_VALUES if fresh else ROW_VALUES), min_size=3, max_size=3)
    )
    return spaced(UnitKind.ROW, [coiner.row_label(fresh), *values])


def reference(coiner: Coiner) -> Unit:
    # An entry of a list of references, its numbers its own.
    number = coiner.number()
    first_page = 10 + number
    return spaced(
        UnitKind.REFERENCE,
        [
            f"{SURNAMES[number % len(SURNAMES)]},",
            f"{RARE_CONSONANTS[number % len(RARE_CONSONANTS)].upper()}.",
            f"({1900 + number % 120}).",
            *REFERENCE_TITLE,
            *JOURNALS[number % len(JOURNALS)].split(WORD_GAP),
            f"{1 + number % 40}({1 + number % 4}):",
            f"{first_page}-{first_page + 5 + number % 9}.",
        ],
    )


def renumbered_word(word: str) -> str:
    # The word with its first digit one more: another number.
    place = next(place for place, character in enumerate(word) if character.isdigit())
    return word[:place] + str((int(word[place]) + 1) % 10) + word[place + 1 :]


def unit_of_kind(kind: UnitKind, coiner: Coiner) -> st.SearchStrategy[Unit]:
    match kind:
        case UnitKind.SENTENCE:
            return sentences(coiner)
        case UnitKind.UNSPACED:
            return unspaced_sentences(coiner)
        case UnitKind.FORMULA:
            return st.builds(
                lambda: spaced(
                    UnitKind.FORMULA, [FORMULA[0].format(rare=coiner.rare()), *FORMULA[1:]]
                )
            )
        case UnitKind.BOILERPLATE:
            return st.sampled_from(BOILERPLATE).map(
                lambda said: spaced(UnitKind.BOILERPLATE, said.split(WORD_GAP))
            )
        case UnitKind.REFERENCE:
            return st.builds(lambda: reference(coiner))
        case UnitKind.ROW:
            return rows(coiner)
        case _:
            return st.lists(st.sampled_from(PLAIN_ROW_VALUES), min_size=3, max_size=3).map(
                lambda values: spaced(UnitKind.PLAIN_ROW, values)
            )


@st.composite
def first_units(
    draw: st.DrawFn,
    coiner: Coiner,
    kinds: tuple[UnitKind, ...],
    most_runs: int = 5,
    said_again: st.SearchStrategy[bool] = SOMETIMES_SAID_AGAIN,
) -> list[Unit]:
    # Runs of a paragraph's units and of a table's lines; a unit may be said again later.
    paragraph_kinds = [kind for kind in kinds if kind not in ROW_KINDS]
    row_kinds = [kind for kind in kinds if kind in ROW_KINDS]
    units: list[Unit] = []
    for _ in range(draw(st.integers(1, most_runs))):
        table = bool(row_kinds) and (not paragraph_kinds or draw(st.booleans()))
        run_kinds = row_kinds if table else paragraph_kinds
        for _ in range(draw(st.integers(2 if table else 1, 8 if table else 6))):
            earlier = [unit for unit in units if (unit.kind in ROW_KINDS) == table]
            if len(earlier) > 0 and draw(said_again):
                units.append(draw(st.sampled_from(earlier)))
            else:
                units.append(draw(unit_of_kind(draw(st.sampled_from(run_kinds)), coiner)))
    return units


def retyped_word(word: str, style: int) -> str:
    match style:
        case 0:
            return word.upper()
        case 1:
            for straight, typeset in STRAIGHT_TO_TYPESET:
                word = word.replace(straight, typeset, 1)
            return word
        case 2:
            return unicodedata.normalize("NFD", word)
        case 3 | 4 if len(word) > 1:
            return word[:1] + INVISIBLES[style - 3] + word[1:]
        case _:
            return word


def varied_word(word: str, style: int, unspaced: bool) -> str:
    # The word as a second recognition may read it: never the same letters.
    if unspaced:
        return word.removesuffix(UNSPACED_SENTENCE_END) + UNSPACED_PUNCTUATION_MORE
    misread = next(
        (word.replace(seen, read, 1) for seen, read in MISREADINGS if seen in word), None
    )
    middle = len(word) // 2
    match style:
        case 0 if misread is not None:
            changed = misread
        case 1 if "-" in word:
            changed = word.replace("-", "", 1)
        case 1 if middle > 1:
            changed = word[:middle] + LINE_END_HYPHEN + word[middle:]
        case 2 if misread is not None:
            changed = misread
            for plain, ligature in LIGATURES:
                changed = changed.replace(plain, ligature)
        case 3 if misread is not None and middle > 1:
            changed = misread[:middle] + WORD_GAP + misread[middle:]
        case _:
            changed = word.removesuffix(SENTENCE_END) + PUNCTUATION_MORE
    return changed if letters_of(changed) != letters_of(word) else word + PUNCTUATION_MORE


@st.composite
def retyped_units(draw: st.DrawFn, unit: Unit) -> Unit:
    if unit.kind in ROW_KINDS:
        return unit
    styles = draw(st.lists(st.integers(0, 6), min_size=len(unit.words), max_size=len(unit.words)))
    words = [retyped_word(word, style) for word, style in zip(unit.words, styles, strict=True)]
    gaps = (
        unit.gaps
        if unit.kind == UnitKind.UNSPACED
        else [draw(st.sampled_from(SPACINGS)) for _ in unit.gaps]
    )
    if [letters_of(word) for word in words] != [letters_of(word) for word in unit.words]:
        raise ValueError(f"a test's retypesetting changed the letters of {unit.words}")
    return Unit(kind=unit.kind, words=words, gaps=gaps)


@st.composite
def varied_units(draw: st.DrawFn, unit: Unit) -> Unit:
    # A few words read otherwise: one in every LETTERS_A_CHANGE letters at most.
    most = max(1, weight(unit_letters(unit)) // LETTERS_A_CHANGE)
    changed = draw(
        st.lists(st.integers(0, len(unit.words) - 1), min_size=1, max_size=most, unique=True)
    )
    words = list(unit.words)
    for number in changed:
        style = draw(st.integers(0, 4))
        words[number] = varied_word(words[number], style, unit.kind == UnitKind.UNSPACED)
    return Unit(kind=unit.kind, words=words, gaps=unit.gaps)


def fresh_unit(kind: UnitKind, coiner: Coiner) -> st.SearchStrategy[Unit]:
    # A unit in the place of one rewritten, or put in: of the same sort, in other words.
    match kind:
        case UnitKind.ROW | UnitKind.PLAIN_ROW:
            return rows(coiner, fresh=True)
        case UnitKind.UNSPACED:
            return unspaced_sentences(coiner)
        case UnitKind.REFERENCE:
            return st.builds(lambda: reference(coiner))
        case _:
            return sentences(coiner)


@st.composite
def cuts(draw: st.DrawFn, units: list[Unit]) -> list[Cut]:
    return [
        Cut(
            before_unit=draw(st.integers(0, 2)) == 0,
            before_word=(
                draw(st.integers(1, len(unit.words) - 1))
                if unit.kind not in ROW_KINDS
                and len(unit.words) > 1
                and draw(st.integers(0, 5)) == 0
                else None
            ),
        )
        for unit in units
    ]


def laid_out(units: list[Unit], unit_cuts: list[Cut], extractor: str) -> Laid:
    # The document whose blocks hold the units: a table's lines in a table of their own, a
    # paragraph's units joined by a space; a block ends where a cut says.
    texts: list[str] = []
    tables: list[bool] = []
    places: list[list[WordPlace]] = []
    for number, (unit, cut) in enumerate(zip(units, unit_cuts, strict=True)):
        table = unit.kind in ROW_KINDS
        if number == 0 or cut.before_unit or tables[-1] != table:
            texts.append("")
            tables.append(table)
        elif texts[-1]:
            texts[-1] += ROW_SEPARATOR if table else cut.joiner
        unit_places: list[WordPlace] = []
        for word_number, word in enumerate(unit.words):
            if word_number and word_number == cut.before_word:
                texts.append("")
                tables.append(table)
            elif word_number:
                texts[-1] += unit.gaps[word_number - 1]
            start = len(texts[-1])
            texts[-1] += word
            unit_places.append(WordPlace(block=len(texts) - 1, start=start, end=len(texts[-1])))
        places.append(unit_places)
    blocks: list[Block] = [
        Table(text=text) if table else Paragraph(text=text, marks=[])
        for text, table in zip(texts, tables, strict=True)
    ]
    document = document_of(
        item_id=ITEM,
        extractor=extractor,
        snapshot=SNAPSHOT,
        media_type=HTML_MEDIA_TYPE,
        blocks=blocks,
    )
    return Laid(document=document, places=places)


@st.composite
def revisions(
    draw: st.DrawFn,
    coiner: Coiner,
    units: list[Unit],
    changes: tuple[Change, ...],
    put_in: bool,
    fixed: frozenset[tuple[int, Change]] = frozenset(),
) -> Revised:
    # The second reading: each unit kept, typeset otherwise, read a little otherwise, removed
    # or rewritten, units put in between (new ones, or one said again), and the blocks cut
    # anew. The units fixed are changed as given, and nothing is put in among them.
    fixed_changes = dict(fixed)
    second: list[Unit] = []
    origins: list[int | None] = []
    made: list[Change] = []
    for number, unit in enumerate([*units, None]):
        guarded = number in fixed_changes and number - 1 in fixed_changes
        if put_in and not guarded and draw(st.integers(0, 4)) == 0:
            said_again = units[draw(st.integers(0, len(units) - 1))]
            near = units[min(number, len(units) - 1)]
            if (said_again.kind in ROW_KINDS) == (near.kind in ROW_KINDS) and draw(st.booleans()):
                second.append(said_again)
            else:
                second.append(draw(fresh_unit(near.kind, coiner)))
            origins.append(None)
        if unit is None:
            continue
        change = fixed_changes.get(number) or draw(st.sampled_from(changes))
        numbered = [place for place, word in enumerate(unit.words) if any(map(str.isdigit, word))]
        if (change == Change.VARIED and unit.kind in ROW_KINDS) or (
            change == Change.RENUMBERED and not numbered
        ):
            change = Change.RETYPED
        made.append(change)
        match change:
            case Change.KEPT:
                second.append(unit)
            case Change.RETYPED:
                second.append(draw(retyped_units(unit)))
            case Change.VARIED:
                second.append(draw(varied_units(unit)))
            case Change.RENUMBERED:
                renumbered = draw(st.sampled_from(numbered))
                words = list(unit.words)
                words[renumbered] = renumbered_word(words[renumbered])
                second.append(Unit(kind=unit.kind, words=words, gaps=unit.gaps))
            case Change.REWRITTEN:
                second.append(draw(fresh_unit(unit.kind, coiner)))
            case _:
                continue
        origins.append(number if change in SURVIVING else None)
    if not second:
        second.append(draw(fresh_unit(units[0].kind, coiner)))
        origins.append(None)
    return Revised(
        first_units=units,
        second_units=second,
        origins=origins,
        changes=made,
        first=laid_out(units, draw(cuts(units)), FIRST_EXTRACTOR),
        second=laid_out(second, draw(cuts(second)), SECOND_EXTRACTOR),
    )


@st.composite
def word_spans(
    draw: st.DrawFn, units: list[Unit], first_unit: int | None = None, most_units: int = 3
) -> WordSpan:
    first = draw(st.integers(0, len(units) - 1)) if first_unit is None else first_unit
    last = draw(st.integers(first, min(len(units), first + most_units) - 1))
    first_word = draw(st.integers(0, len(units[first].words) - 1))
    last_word = draw(st.integers(first_word if first == last else 0, len(units[last].words) - 1))
    return WordSpan(first_unit=first, first_word=first_word, last_unit=last, last_word=last_word)


def span_words(span: WordSpan, units: list[Unit]) -> list[tuple[int, int]]:
    return [
        (unit, word)
        for unit in range(span.first_unit, span.last_unit + 1)
        for word in range(len(units[unit].words))
        if (span.first_unit, span.first_word) <= (unit, word) <= (span.last_unit, span.last_word)
    ]


def anchors_over(laid: Laid, words: list[tuple[int, int]]) -> list[Anchor]:
    # The anchors of the stretch from the first word given to the last: in each block, from
    # the first of its words to the end of the last.
    by_block: dict[int, list[WordPlace]] = {}
    for unit, word in words:
        place = laid.places[unit][word]
        by_block.setdefault(place.block, []).append(place)
    return [
        Anchor(
            document_id=laid.document.id,
            block_number=block,
            start=held[0].start,
            end=held[-1].end,
            quote=laid.document.blocks[block].text[held[0].start : held[-1].end],
        )
        for block, held in by_block.items()
    ]


def marked_passage(revised: Revised, span: WordSpan) -> Passage:
    return Passage(anchors=anchors_over(revised.first, span_words(span, revised.first_units)))


def document_letters(document: Document) -> Letters:
    text: list[str] = []
    places: list[tuple[int, int]] = []
    for number, block in enumerate(document.blocks):
        form = comparable(block.text, ignored=set())
        for letter, origin in zip(form.text, form.origins, strict=True):
            if letter != WORD_GAP:
                text.append(letter)
                places.append((number, origin))
    return Letters(text="".join(text), places=places)


def occurrences(text: str, sought: str) -> list[int]:
    found: list[int] = []
    at = text.find(sought)
    while at >= 0 and sought:
        found.append(at)
        at = text.find(sought, at + 1)
    return found


def best_likeness_elsewhere(sought: str, text: str, place: tuple[int, int]) -> float:
    # The likeness to the passage of the likest stretch of the text that leaves out the place:
    # the fewest letters put in, left out or read otherwise that make the passage any stretch.
    masked = text[: place[0]] + MASKED * (place[1] - place[0]) + text[place[1] :]
    row = [0] * (len(masked) + 1)
    for number, letter in enumerate(sought, start=1):
        previous, row[0] = row[0], number
        for at, other_letter in enumerate(masked, start=1):
            previous, row[at] = (
                row[at],
                min(row[at] + 1, row[at - 1] + 1, previous + (letter != other_letter)),
            )
    return 1 - min(row) / max(1, len(sought))


def edits(one: str, other: str) -> int:
    # The fewest letters put in, left out or read otherwise that make one text the other.
    row = list(range(len(other) + 1))
    for number, letter in enumerate(one, start=1):
        previous, row[0] = row[0], number
        for place, other_letter in enumerate(other, start=1):
            previous, row[place] = (
                row[place],
                min(row[place] + 1, row[place - 1] + 1, previous + (letter != other_letter)),
            )
    return row[-1]


def described(document: Document, anchors: list[Anchor]) -> str:
    return (
        " | ".join(
            f"block {anchor.block_number} [{anchor.start}:{anchor.end}] {anchor.quote!r}"
            for anchor in anchors
        )
        + f" (of {len(document.blocks)} blocks)"
    )


def digits_of(letters: str) -> str:
    return "".join(letter for letter in letters if letter.isdigit())


def is_inside_a_word(before: str, after: str) -> bool:
    # Two characters of spaced text that stand in one word, or in one number.
    return (
        before.isalnum() and after.isalnum() and not is_unspaced(before) and not is_unspaced(after)
    )


def well_formed(found: Found, document: Document, letters: Letters) -> list[tuple[int, int]]:
    # The stretches of the document's letters a found passage covers, one for each anchor,
    # once its anchors are seen to be the document's own (R6b, R6f), in the order of the
    # document, of whole words and whole characters.
    anchors = found.passage.anchors
    assert anchors, "R6b: a passage was found with no anchor"
    for anchor in anchors:
        assert anchor.document_id == document.id, (
            f"R6b: a passage found names another document than the one it was looked for in: "
            f"{anchor.document_id}"
        )
        assert anchor.block_number < len(document.blocks), (
            f"R6b: a passage was found in a block the document does not have: {anchor}"
        )
        text = document.blocks[anchor.block_number].text
        assert 0 <= anchor.start < anchor.end <= len(text), (
            f"R6b: a found anchor is empty or out of its block, counted in code points: {anchor}"
        )
        assert anchor.quote == text[anchor.start : anchor.end], (
            f"R6b, R6f: a found anchor's quote is not the text of its block at its range, counted "
            f"in code points: {anchor} where the block holds "
            f"{text[anchor.start : anchor.end]!r}"
        )
        assert not anchor.quote[0].isspace() and not anchor.quote[-1].isspace(), (
            f"R6b: a found anchor begins or ends with white space: {anchor.quote!r}"
        )
        assert not unicodedata.combining(anchor.quote[0]) and not (
            anchor.end < len(text) and unicodedata.combining(text[anchor.end])
        ), f"R6d, R6f: a found anchor parts a character from its marks: {anchor}"
    first, last = anchors[0], anchors[-1]
    first_text = document.blocks[first.block_number].text
    last_text = document.blocks[last.block_number].text
    assert not (
        first.start and is_inside_a_word(first_text[first.start - 1], first_text[first.start])
    ) and not (
        last.end < len(last_text) and is_inside_a_word(last_text[last.end - 1], last_text[last.end])
    ), (
        "R6 (whole words): a passage of whole words was found beginning or ending inside a word "
        f"or a number: {described(document, anchors)}"
    )
    assert all(earlier.block_number < later.block_number for earlier, later in pairwise(anchors)), (
        f"R6b: the anchors of a passage found are not in the document's order: {anchors}"
    )
    return [letters.span(anchor.block_number, anchor.start, anchor.end) for anchor in anchors]


def table_parts(document: Document, anchors: list[Anchor]) -> int:
    # How many separators of a table's cells and rows a passage runs across, a table's block
    # ending among them counted as one.
    in_tables = [
        anchor for anchor in anchors if isinstance(document.blocks[anchor.block_number], Table)
    ]
    return max(0, len(in_tables) - 1) + sum(
        anchor.quote.count(CELL_SEPARATOR) + anchor.quote.count(ROW_SEPARATOR)
        for anchor in in_tables
    )


def parts_marked(document: Document, anchors: list[Anchor]) -> int:
    # The separators a passage was marked across, and the ends of the blocks it runs through,
    # each of which may stand as a separator in another revision.
    return len(anchors) - 1 + table_parts(document, anchors)


class Outcome(StrEnum):
    # The three answers the rule holds the search to: found where the passage's words stand,
    # lost, or either of the two, and never found anywhere else.
    FOUND = "found where its words stand"
    LOST = "lost"
    EITHER = "lost, or found where its words stand"


class Expectation(BaseModel):
    # What a rule that knows how the second document was made expects of a passage: where its
    # words stand there, the very same or otherwise, and what the search must answer. A passage
    # held once in each revision may stand at the one copy of its words in the other wherever
    # the operations put that copy (a move).
    sought: str
    marked_at: tuple[int, int]
    region: tuple[int, int] | None
    # Every word of the passage stands unchanged, in its order, perhaps with units put in
    # between; and with nothing between.
    same_words: bool
    same: bool
    expected_anchors: list[Anchor]
    moved_to: tuple[int, int] | None
    # The letters between the units next to the passage that stand again, where its words
    # stood; and the copies of its very words there with the same letters right around them,
    # which no reading of the text tells from its own words.
    envelope: tuple[int, int]
    copies_in_place: list[tuple[int, int]]
    near: bool
    outcome: Outcome
    # Whether no other copy of the passage's words in the revision it was marked in had
    # surroundings alike to its own.
    told_in_first: bool


def has_rival(text: str, sought: str, place: int, marked_sides: tuple[str, str]) -> bool:
    # Whether a copy of the words other than the one at the place has surroundings that may
    # leave the place in doubt: either side half alike or more, or both together.
    for copy in occurrences(text, sought):
        if copy == place:
            continue
        held_sides = sides(text, (copy, copy + len(sought)), RIVAL_SIDE)
        before = side_likeness(marked_sides[0], held_sides[0], before=True)
        after = side_likeness(marked_sides[1], held_sides[1], before=False)
        if max(before, after) > RIVAL_SIDE_LIKENESS or before + after > RIVAL_LIKENESS:
            return True
    return False


def told_clearly(text: str, sought: str, place: int, marked_sides: tuple[str, str]) -> bool:
    # Whether every other copy of the words stands among letters far from the passage's own
    # on both sides, well past the search's count of letters that tell copies apart.
    for copy in occurrences(text, sought):
        if copy == place:
            continue
        for size in (RIVAL_SIDE, SURROUNDINGS):
            marked_before, marked_after = marked_sides[0][-size:], marked_sides[1][:size]
            held_before, held_after = sides(text, (copy, copy + len(sought)), size)
            if (
                side_likeness(marked_before, held_before, before=True) > CLEARLY_OTHER
                or side_likeness(marked_after, held_after, before=False) > CLEARLY_OTHER
            ):
                return False
    return True


def unit_letter_span(laid: Laid, letters: Letters, unit: int) -> tuple[int, int]:
    first, last = laid.places[unit][0], laid.places[unit][-1]
    return (
        letters.span(first.block, first.start, first.end)[0],
        letters.span(last.block, last.start, last.end)[1],
    )


def likeness(sought: str, held: str) -> float:
    return 1 - edits(sought, held) / max(1, len(sought))


def sides(text: str, place: tuple[int, int], size: int) -> tuple[str, str]:
    return text[max(0, place[0] - size) : place[0]], text[place[1] : place[1] + size]


def side_likeness(marked: str, held: str, before: bool) -> float:
    # The share of a side's letters that stand at the same distance from the passage again; a
    # side at a document's edge is alike only to the edge.
    if not marked or not held:
        return float(marked == held)
    pairs = (
        zip(reversed(marked), reversed(held), strict=False)
        if before
        else zip(marked, held, strict=False)
    )
    return sum(one == other for one, other in pairs) / len(marked)


def sides_changed(
    first: Letters, marked: tuple[int, int], second: Letters, place: tuple[int, int]
) -> bool:
    # Whether the letters right before and right after a passage have changed on both sides,
    # far past the search's count of neighbouring letters.
    before, after = sides(first.text, marked, TELLING_SIDE)
    held_before, held_after = sides(second.text, place, TELLING_SIDE)
    return (
        side_likeness(before, held_before, before=True) <= CHANGED_SIDE
        and side_likeness(after, held_after, before=False) <= CHANGED_SIDE
    )


def moved_outcome(
    sought: str, first: Letters, marked: tuple[int, int], second: Letters, place: tuple[int, int]
) -> Outcome:
    # A passage held once in each revision, whose one copy in the other stands at a place its
    # own words were not carried to (ruling 1): a distinct one is found by its letters; one
    # whose neighbours changed on both sides is lost; else either.
    if weight(sought) >= DISTINCT_LETTERS:
        return Outcome.FOUND
    if weight(sought) < SHORT_LETTERS and sides_changed(first, marked, second, place):
        return Outcome.LOST
    return Outcome.EITHER


def expectation(revised: Revised, span: WordSpan, first: Letters, second: Letters) -> Expectation:
    words = span_words(span, revised.first_units)
    head = revised.first.places[words[0][0]][words[0][1]]
    tail = revised.first.places[words[-1][0]][words[-1][1]]
    marked = (
        first.span(head.block, head.start, head.end)[0],
        first.span(tail.block, tail.start, tail.end)[1],
    )
    sought = first.text[marked[0] : marked[1]]
    copies_first = occurrences(first.text, sought)
    copies_second = occurrences(second.text, sought)
    held_once = len(copies_first) == len(copies_second) == 1
    told_in_first = not has_rival(
        first.text, sought, marked[0], sides(first.text, marked, RIVAL_SIDE)
    )
    second_unit = {
        origin: number for number, origin in enumerate(revised.origins) if origin is not None
    }
    surviving = [(second_unit[unit], word) for unit, word in words if unit in second_unit]
    units = range(span.first_unit, span.last_unit + 1)
    region: tuple[int, int] | None = None
    held_words = ""
    if surviving:
        head = revised.second.places[surviving[0][0]][surviving[0][1]]
        tail = revised.second.places[surviving[-1][0]][surviving[-1][1]]
        region = (
            second.span(head.block, head.start, head.end)[0],
            second.span(tail.block, tail.start, tail.end)[1],
        )
        held_words = "".join(
            second.text[slice(*second.span(place.block, place.start, place.end))]
            for place in (revised.second.places[unit][word] for unit, word in surviving)
        )
    same_words = len(surviving) == len(words) and held_words == sought
    same = same_words and region is not None and second.text[region[0] : region[1]] == sought
    moved_to = (
        (copies_second[0], copies_second[0] + len(sought)) if held_once and not same_words else None
    )
    whole = all(unit in second_unit for unit in units) and all(
        second_unit[later] == second_unit[earlier] + 1 for earlier, later in pairwise(units)
    )
    before_kept = [second_unit[unit] for unit in range(span.first_unit) if unit in second_unit]
    after_kept = [
        second_unit[unit]
        for unit in range(span.last_unit + 1, len(revised.first_units))
        if unit in second_unit
    ]
    envelope = (
        unit_letter_span(revised.second, second, before_kept[-1])[1] if before_kept else 0,
        unit_letter_span(revised.second, second, after_kept[0])[0]
        if after_kept
        else len(second.text),
    )
    # The very words at the passage's own place, whatever the unit around them became, with
    # the letters right around them not changed on both sides (ruling 7).
    copies_in_place = [
        (copy, copy + len(sought))
        for copy in copies_second
        if envelope[0] <= copy
        and copy + len(sought) <= envelope[1]
        and not sides_changed(first, marked, second, (copy, copy + len(sought)))
    ]
    # A passage of a table's lines takes its neighbours from its own line (the lead's decision
    # on cells), so lines changed around it do not make it lost.
    in_table = any(revised.first_units[unit].kind in ROW_KINDS for unit in units)
    near = False
    if moved_to is not None:
        outcome = moved_outcome(sought, first, marked, second, moved_to)
    elif region is None:
        outcome = Outcome.EITHER if copies_in_place else Outcome.LOST
    elif same:
        intact = sides(second.text, region, SURROUNDINGS) == sides(first.text, marked, SURROUNDINGS)
        marked_wide = sides(first.text, marked, SURROUNDINGS)
        if (held_once and weight(sought) >= DISTINCT_LETTERS) or (
            intact
            and told_clearly(first.text, sought, marked[0], marked_wide)
            and told_clearly(second.text, sought, region[0], marked_wide)
        ):
            outcome = Outcome.FOUND
        elif (
            told_in_first
            and not in_table
            and sides_changed(first, marked, second, region)
            and not (held_once and weight(sought) >= SHORT_LETTERS)
        ):
            outcome = Outcome.LOST
        else:
            outcome = Outcome.EITHER
    elif same_words or copies_in_place:
        # Units put in between the passage's own blocks (ruling 3), or its very words at its
        # place with the same letters around them.
        outcome = Outcome.EITHER
    else:
        held = second.text[region[0] : region[1]]
        near = True
        if (whole and digits_of(held) != digits_of(sought)) or (
            told_in_first and not in_table and sides_changed(first, marked, second, region)
        ):
            outcome = Outcome.LOST
        elif (
            whole
            and sides(second.text, region, SURROUNDINGS) == sides(first.text, marked, SURROUNDINGS)
            and likeness(sought, held) >= NEAR_ALIKE
            and best_likeness_elsewhere(sought, second.text, region) < RIVAL_NEAR
            and weight(sought) >= DISTINCT_LETTERS
            and len(copies_first) == 1
            and not copies_second
            and all(revised.first_units[unit].kind in OWN_KINDS for unit in units)
        ):
            outcome = Outcome.FOUND
        else:
            outcome = Outcome.EITHER
    return Expectation(
        sought=sought,
        marked_at=marked,
        region=region,
        same_words=same_words,
        same=same,
        expected_anchors=anchors_over(revised.second, surviving),
        moved_to=moved_to,
        envelope=envelope,
        copies_in_place=copies_in_place,
        near=near,
        outcome=outcome,
        told_in_first=told_in_first,
    )


def judge_standing(
    revised: Revised,
    span: WordSpan,
    standing: Standing,
    first: Letters,
    second: Letters,
) -> Expectation:
    # Judges where a search says a passage of the first document stands in the second (R6b to
    # R6f as the lead amended and ruled them), by where the operations that made the second
    # document put its words: a passage is found only where its own words stand, the very
    # same or a little otherwise, or at the one copy of a passage each revision holds once;
    # or it is lost.
    expected = expectation(revised, span, first, second)
    document = revised.second.document
    marked = marked_passage(revised, span)
    if expected.moved_to is not None:
        where = f"stand once in it at letters {expected.moved_to}"
    elif expected.region is None:
        where = "are gone"
    else:
        where = f"stand at {described(document, expected.expected_anchors)}" + (
            "" if expected.same else " otherwise, in part, or with units between"
        )
    told = (
        f"the passage {described(revised.first.document, marked.anchors)}, whose words "
        f"{where}, must be {expected.outcome.value}, and was said to stand: {standing}"
    )
    match expected.outcome:
        case Outcome.FOUND:
            # Something put in right after or before the passage's words leaves them its very
            # words (ruling 6): found exact is right there too.
            assert isinstance(standing, Found) and (
                standing.exact == (not expected.near) or standing.exact
            ), (
                "R6b, R6c, R6d: a passage whose words stand where its letters or its "
                "surroundings tell was not found there, "
                + ("not exact" if expected.near else "exact")
                + f": {told}"
            )
        case Outcome.LOST:
            # A passage whose words are gone may yet be found in words at their place that
            # keep nearly as many of its letters as the search's bound asks (ruling 5).
            assert not isinstance(standing, Found) or (
                expected.region is None and not standing.exact
            ), (
                "R6c, R6d, R6e: a passage whose own words are gone, changed in a number, or "
                f"whose surroundings changed on both sides was found: {told}"
            )
        case _:
            pass
    if not isinstance(standing, Found):
        return expected
    spans = well_formed(standing, document, second)
    assert table_parts(document, standing.passage.anchors) <= parts_marked(
        revised.first.document, marked.anchors
    ), f"R6 (table cells): a passage was found across the cells of a table: {told}"
    found_letters = "".join(second.text[start:end] for start, end in spans)
    whole_span = (spans[0][0], spans[-1][1])
    if standing.exact:
        assert found_letters == expected.sought, (
            f"R6b: a passage was found exact in words that are not its own: "
            f"{found_letters!r} for {expected.sought!r}: {told}"
        )
        assert (
            (expected.same_words and standing.passage.anchors == expected.expected_anchors)
            or whole_span == expected.moved_to
            or whole_span in expected.copies_in_place
            or (
                expected.region is not None
                and expected.region[0] <= whole_span[0]
                and whole_span[1] <= expected.region[1]
            )
        ), (
            f"R6c, R6e: a passage was found exact at another place than where its own words "
            f"stand, or without the anchors of its words there: {told}"
        )
        return expected
    assert not expected.same, (
        f"R6b: a passage was found in words that differ from it, though its very words stand "
        f"where it was marked: {told}"
    )
    low, high = (
        expected.region
        if expected.outcome == Outcome.FOUND and expected.region is not None
        else expected.envelope
    )
    assert low - NEAR_SLACK <= whole_span[0] and whole_span[1] <= high + NEAR_SLACK, (
        f"R6d, R6e: a passage was found in words that differ from it, away from where its "
        f"words stood: {told}"
    )
    assert digits_of(found_letters) == digits_of(expected.sought), (
        f"R6d: a passage was found in words that differ from it in a number: {told}"
    )
    assert likeness(expected.sought, found_letters) >= EDGE_LIKENESS, (
        f"R6d, R6e: a passage was found in words that keep far fewer of its letters than words "
        f"that differ a little: {told}"
    )
    if expected.outcome == Outcome.FOUND and expected.region is not None:
        covered = min(whole_span[1], high) - max(whole_span[0], low)
        assert covered >= NEAR_COVER * (high - low), (
            f"R6d: a passage found in words that differ a little covers too little of them: {told}"
        )
    return expected
