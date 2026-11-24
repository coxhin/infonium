from bisect import bisect_left, bisect_right
from collections import OrderedDict
from enum import StrEnum
from functools import cache
from itertools import accumulate, pairwise
from threading import Lock
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from infonium.anchor import Anchor, is_invisible, plain_character
from infonium.document import Document, Tagged, is_unspaced

SEVERAL_DOCUMENTS = "the anchors of a passage lie in one document"
OUT_OF_ORDER = "the anchors of a passage lie in different blocks, in the order of the document"
EMPTY_ANCHOR = "an anchor of a passage holds at least one character"
NOT_HELD = "block {block} of the document does not hold these words at {start} to {end}"
# A passage is looked for in another revision by its letters: the characters that
# anchor.comparable keeps, white space aside, so that neither spaces nor the places where
# blocks are cut decide what is found.
#
# The letters on each side of a passage that tell one place of its words from another, and
# the pieces they are compared by: a side is found beside a place when this share of its
# pieces stands within as many letters, and a few more, on that side of the place.
SURROUNDINGS = 32
SURROUNDINGS_SLACK = 4
GRAM = 3
SURROUNDINGS_FOUND = 0.5
# A passage of fewer letters than this could stand in many places for what it says, and is
# found only where its surroundings are found with it, or where both revisions hold it once. A
# character of a script written without spaces says as much as several letters.
DISTINCT_LETTERS = 40
UNSPACED_LETTERS = 3
# Words that differ a little: the share of a passage's letters that the fewest changes of a
# letter (one put in, left out, or read otherwise) must leave as they are.
NEAR_LIKENESS = 0.8
# Where words that differ a little may lie is told by pieces of them that the other revision
# holds as they are: up to this many pieces of this many letters, each of which tells nothing
# when it stands in more places than this. The places that the most pieces agree on are
# compared whole, this many of them.
SEED_LETTERS = 12
SEEDS = 24
SEED_PLACES = 64
PLACES_COMPARED = 4
# The places of the very letters that are weighed against each other, at most.
OCCURRENCES_WEIGHED = 2000
# The documents whose letters are kept for the next search, those searched last.
SPELLINGS_KEPT = 16
# What each letter of a text before a stretch costs, in end_distances: nothing where the
# stretch may begin anywhere, one where it begins with the text.
ANYWHERE = 0
AT_THE_BEGINNING = 1


class Passage(BaseModel):
    # A stretch of one document's text, as a reader marks it or a note links to it: an anchor
    # for each block it runs through, in the order of the document. A passage inside one block
    # is one anchor.
    anchors: Annotated[list[Anchor], Field(min_length=1)]

    @model_validator(mode="after")
    def one_stretch(self) -> Self:
        if len({anchor.document_id for anchor in self.anchors}) > 1:
            raise ValueError(SEVERAL_DOCUMENTS)
        numbers = [anchor.block_number for anchor in self.anchors]
        if any(later <= earlier for earlier, later in pairwise(numbers)):
            raise ValueError(OUT_OF_ORDER)
        if any(anchor.end <= anchor.start for anchor in self.anchors):
            raise ValueError(EMPTY_ANCHOR)
        return self

    @property
    def document_id(self) -> str:
        return self.anchors[0].document_id


class StandingKind(StrEnum):
    FOUND = "found"
    LOST = "lost"


class Found(Tagged):
    # The document holds the passage, at these anchors, which are the document's own. Exact
    # says whether it holds the passage's very words, letter case, spacing and the shapes of
    # quotation marks and dashes aside, or words that differ a little from them, as another
    # recognition of a scanned page gives.
    kind: Literal[StandingKind.FOUND] = StandingKind.FOUND
    passage: Passage
    exact: bool


class Lost(Tagged):
    # The document does not hold the passage's words.
    kind: Literal[StandingKind.LOST] = StandingKind.LOST


# Where a passage of one revision of a source stands in another revision of it.
Standing = Annotated[Found | Lost, Field(discriminator="kind")]


class PassageNotHeld(Exception):
    # A passage was given that its own document does not hold as it is given.
    pass


def held_passage(passage: Passage, document: Document) -> Passage:
    # The passage, once its document is seen to hold it: every anchor names a block of the
    # document, and its quote is the text of that block at its range.
    for anchor in passage.anchors:
        held = (
            document.blocks[anchor.block_number].text[anchor.start : anchor.end]
            if anchor.document_id == document.id and anchor.block_number < len(document.blocks)
            else None
        )
        if held != anchor.quote:
            raise PassageNotHeld(
                NOT_HELD.format(block=anchor.block_number, start=anchor.start, end=anchor.end)
            )
    return passage


class Spelling(BaseModel):
    # A document's letters, block after block, and the letter each block begins at; the last
    # number is the count of letters.
    letters: str
    starts: list[int]


class BlockLetters(BaseModel):
    # For each letter of a block's text: the offset of the character it comes from, and
    # whether white space stands between it and the letter before.
    origins: list[int]
    spaced: list[bool]


class Piece(BaseModel):
    # Letters of one block, counted from the block's first.
    block_number: int
    start: int
    end: int


class Stretch(BaseModel):
    # Letters of a spelling, and how many letters must change to make them a passage's.
    start: int
    end: int
    distance: int


class MarkedLetters(BaseModel):
    # A passage as letters: its letters, which of them stand after white space and which begin
    # an anchor, the letters around it in the revision it was marked in, where it begins there
    # as a share of that revision's letters, and whether that revision holds it once.
    letters: str
    spaced: list[bool]
    breaks: list[bool]
    before: str
    after: str
    place: float
    once: bool


@cache
def letter_form(character: str) -> str:
    if character.isspace() or is_invisible(character):
        return ""
    return "".join(plain_character(character).split())


def letters_of(text: str) -> str:
    return text.translate({ord(character): letter_form(character) for character in set(text)})


def spelling_of(document: Document) -> Spelling:
    blocks = [letters_of(block.text) for block in document.blocks]
    return Spelling(letters="".join(blocks), starts=list(accumulate(map(len, blocks), initial=0)))


def block_letters(text: str) -> BlockLetters:
    origins: list[int] = []
    spaced: list[bool] = []
    gap = False
    for offset, character in enumerate(text):
        form = letter_form(character)
        if not form:
            gap = gap or character.isspace()
            continue
        origins.extend([offset] * len(form))
        spaced.extend([gap] + [False] * (len(form) - 1))
        gap = False
    return BlockLetters(origins=origins, spaced=spaced)


class KeptSpellings:
    # The letters of the documents searched last. A document never changes under its id, so
    # its letters are dropped only for room.
    def __init__(self, room: int) -> None:
        self.room = room
        self.spellings: OrderedDict[str, Spelling] = OrderedDict()
        self.lock = Lock()

    def spelling(self, document: Document) -> Spelling:
        with self.lock:
            kept = self.spellings.get(document.id)
            if kept is not None:
                self.spellings.move_to_end(document.id)
                return kept
        spelling = spelling_of(document)
        with self.lock:
            self.spellings[document.id] = spelling
            while len(self.spellings) > self.room:
                self.spellings.popitem(last=False)
        return spelling


@cache
def kept_spellings() -> KeptSpellings:
    return KeptSpellings(room=SPELLINGS_KEPT)


def occurrences(letters: str, needle: str, most: int) -> list[int]:
    # Where the letters hold the needle, up to the most that are wanted.
    found: list[int] = []
    at = letters.find(needle)
    while at >= 0 and len(found) < most:
        found.append(at)
        at = letters.find(needle, at + 1)
    return found


def is_distinct(letters: str) -> bool:
    weight = sum(UNSPACED_LETTERS if is_unspaced(letter) else 1 for letter in letters)
    return weight >= DISTINCT_LETTERS


def marked_letters(passage: Passage, document: Document, spelling: Spelling) -> MarkedLetters:
    letters: list[str] = []
    spaced: list[bool] = []
    breaks: list[bool] = []
    ends: list[int] = []
    for anchor in passage.anchors:
        layout = block_letters(document.blocks[anchor.block_number].text)
        first = bisect_left(layout.origins, anchor.start)
        last = bisect_left(layout.origins, anchor.end)
        base = spelling.starts[anchor.block_number]
        letters.append(spelling.letters[base + first : base + last])
        spaced.extend(layout.spaced[first:last])
        breaks.extend([True] + [False] * (last - first - 1) if last > first else [])
        ends.extend([base + first, base + last])
    joined = "".join(letters)
    begin, finish = ends[0], ends[-1]
    return MarkedLetters(
        letters=joined,
        spaced=spaced,
        breaks=breaks,
        before=spelling.letters[max(0, begin - SURROUNDINGS) : begin],
        after=spelling.letters[finish : finish + SURROUNDINGS],
        place=begin / max(1, len(spelling.letters)),
        once=bool(joined) and len(occurrences(spelling.letters, joined, 2)) == 1,
    )


def pieces_of(spelling: Spelling, start: int, end: int) -> list[Piece]:
    # The letters of a stretch, block by block; a block it holds no letter of has no piece.
    first = bisect_right(spelling.starts, start) - 1
    pieces: list[Piece] = []
    for number, (begin, finish) in enumerate(pairwise(spelling.starts[first:]), start=first):
        low, high = max(start, begin), min(end, finish)
        if high > low:
            pieces.append(Piece(block_number=number, start=low - begin, end=high - begin))
        if finish >= end:
            break
    return pieces


def anchors_at(document: Document, pieces: list[Piece]) -> list[Anchor]:
    anchors: list[Anchor] = []
    for piece in pieces:
        text = document.blocks[piece.block_number].text
        origins = block_letters(text).origins
        start, end = origins[piece.start], origins[piece.end - 1] + 1
        anchors.append(
            Anchor(
                document_id=document.id,
                block_number=piece.block_number,
                start=start,
                end=end,
                quote=text[start:end],
            )
        )
    return anchors


def is_spaced_alike(passage: MarkedLetters, document: Document, pieces: list[Piece]) -> bool:
    # Whether the same letters stand in the document with white space where the passage has
    # it. Where a block begins, in either, a space may stand or not.
    spaced: list[bool] = []
    breaks: list[bool] = []
    for piece in pieces:
        layout = block_letters(document.blocks[piece.block_number].text)
        spaced.extend(layout.spaced[piece.start : piece.end])
        breaks.extend([True] + [False] * (piece.end - piece.start - 1))
    return all(
        marked_break or held_break or marked_space == held_space
        for marked_space, marked_break, held_space, held_break in zip(
            passage.spaced, passage.breaks, spaced, breaks, strict=True
        )
    )


def grams(letters: str) -> set[str]:
    # The pieces a side is compared by; a side shorter than a piece is one piece.
    return {letters[at : at + GRAM] for at in range(len(letters) - GRAM + 1)} or {letters}


def side_likeness(marked_side: str, held_side: str) -> float:
    # The share of the letters on one side of a passage that stand on that side of a place. A
    # passage at an end of its revision is found at that end of another.
    if not marked_side:
        return float(not held_side)
    expected = grams(marked_side)
    return len(expected & grams(held_side)) / len(expected)


def surroundings(passage: MarkedLetters, letters: str, start: int, end: int) -> tuple[float, float]:
    reach = SURROUNDINGS + SURROUNDINGS_SLACK
    return (
        side_likeness(passage.before, letters[max(0, start - reach) : start]),
        side_likeness(passage.after, letters[end : end + reach]),
    )


def end_distances(needle: str, text: str, entry: int) -> list[int]:
    # For each end in the text, the fewest letters to put in, leave out or change that make the
    # needle the stretch of the text that ends there, a stretch that begins anywhere or at the
    # beginning of the text, as entry says. This is Myers' bit-parallel count, as Hyyro writes
    # it: a bit for each letter of the needle, so that a letter of the text takes a few
    # operations on whole numbers.
    size = len(needle)
    full = (1 << size) - 1
    top = 1 << (size - 1)
    matches: dict[str, int] = {}
    for position, letter in enumerate(needle):
        matches[letter] = matches.get(letter, 0) | 1 << position
    rising, falling, distance = full, 0, size
    distances: list[int] = []
    for letter in text:
        equal = matches.get(letter, 0)
        vertical = equal | falling
        horizontal = (((equal & rising) + rising) ^ rising) | equal
        up = falling | (full & ~(horizontal | rising))
        down = rising & horizontal
        distance += 1 if up & top else -1 if down & top else 0
        up = ((up << 1) | entry) & full
        down = (down << 1) & full
        rising = down | (full & ~(vertical | up))
        falling = up & vertical
        distances.append(distance)
    return distances


def nearest_stretch(needle: str, text: str) -> Stretch:
    # The stretch of the text that the fewest changes make the needle.
    ends = end_distances(needle, text, ANYWHERE)
    distance = min(ends)
    end = ends.index(distance) + 1
    starts = end_distances(needle[::-1], text[:end][::-1], AT_THE_BEGINNING)
    return Stretch(start=end - 1 - starts.index(min(starts)), end=end, distance=distance)


def is_near(stretch: Stretch, needle: str) -> bool:
    return stretch.distance <= len(needle) * (1 - NEAR_LIKENESS)


def near_stretches(letters: str, needle: str) -> list[Stretch]:
    # The stretches of the letters that are like the needle, looked for where the most of its
    # pieces stand as they are.
    count = min(SEEDS, len(needle) // SEED_LETTERS)
    step = (len(needle) - SEED_LETTERS) / max(1, count - 1)
    diagonals: list[tuple[int, int]] = []
    for offset in sorted({round(seed * step) for seed in range(count)}):
        found = occurrences(letters, needle[offset : offset + SEED_LETTERS], SEED_PLACES + 1)
        if len(found) <= SEED_PLACES:
            diagonals.extend((at - offset, offset) for at in found)
    slack = int(len(needle) * (1 - NEAR_LIKENESS)) + 1
    groups: list[list[tuple[int, int]]] = []
    for diagonal in sorted(diagonals):
        if groups and diagonal[0] - groups[-1][-1][0] <= slack + SEED_LETTERS:
            groups[-1].append(diagonal)
        else:
            groups.append([diagonal])
    likeliest = sorted(groups, key=lambda group: -len({offset for _, offset in group}))
    stretches: list[Stretch] = []
    for group in likeliest[:PLACES_COMPARED]:
        low = max(0, group[0][0] - slack)
        nearest = nearest_stretch(needle, letters[low : group[-1][0] + len(needle) + slack])
        if is_near(nearest, needle):
            stretches.append(
                Stretch(start=low + nearest.start, end=low + nearest.end, distance=nearest.distance)
            )
    return stretches


def exact_stretches(passage: MarkedLetters, held: Spelling) -> list[Stretch]:
    # Where the other revision holds the passage's very letters: of several places, the one
    # its surroundings are found beside, then the nearest to where it stood. A passage that is
    # not distinct is found only by its surroundings, or where both revisions hold it once.
    size = len(passage.letters)
    found = occurrences(held.letters, passage.letters, OCCURRENCES_WEIGHED)
    length = max(1, len(held.letters))
    weighed = [
        (sum(likeness), -abs(at / length - passage.place), at, likeness)
        for at in found
        for likeness in [surroundings(passage, held.letters, at, at + size)]
    ]
    return [
        Stretch(start=at, end=at + size, distance=0)
        for _, _, at, likeness in sorted(weighed)[-1:]
        if is_distinct(passage.letters)
        or max(likeness) >= SURROUNDINGS_FOUND
        or (len(found) == 1 and passage.once)
    ]


def near_stretch(passage: MarkedLetters, held: Spelling) -> list[Stretch]:
    # Where the other revision holds letters a little unlike the passage's. A passage that is
    # not distinct is looked for with its surroundings, and then found among them.
    if is_distinct(passage.letters):
        candidates = near_stretches(held.letters, passage.letters)
    else:
        candidates = [
            Stretch(
                start=around.start + within.start,
                end=around.start + within.end,
                distance=within.distance,
            )
            for around in near_stretches(
                held.letters, passage.before + passage.letters + passage.after
            )
            for within in [
                nearest_stretch(passage.letters, held.letters[around.start : around.end])
            ]
            if is_near(within, passage.letters)
        ]
    length = max(1, len(held.letters))
    weighed = [
        (
            -stretch.distance,
            sum(surroundings(passage, held.letters, stretch.start, stretch.end)),
            -abs(stretch.start / length - passage.place),
            stretch.start,
            stretch.end,
        )
        for stretch in candidates
    ]
    return [
        Stretch(start=start, end=end, distance=-distance)
        for distance, _, _, start, end in sorted(weighed)[-1:]
    ]


def standing_in(passage: Passage, marked_in: Document, document: Document) -> Standing:
    # Where a passage that was marked in one revision of a source stands in another: in the
    # revision it was marked in, where it is; in another, where that holds the passage's
    # letters, the very ones or ones a little unlike them; and lost where it holds neither, or
    # where it is doubtful which of their places is the passage's.
    if document.id == marked_in.id:
        return Found(passage=passage, exact=True)
    spellings = kept_spellings()
    held = spellings.spelling(document)
    letters = marked_letters(passage, marked_in, spellings.spelling(marked_in))
    if not letters.letters:
        return Lost()
    exact = exact_stretches(letters, held)
    chosen = exact or near_stretch(letters, held)
    if not chosen:
        return Lost()
    pieces = pieces_of(held, chosen[0].start, chosen[0].end)
    return Found(
        passage=Passage(anchors=anchors_at(document, pieces)),
        exact=bool(exact) and is_spaced_alike(letters, document, pieces),
    )
