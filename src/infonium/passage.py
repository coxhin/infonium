from bisect import bisect_left, bisect_right
from collections import Counter, OrderedDict
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
# pieces stands as far from the place on that side, a piece that stands a letter or a few
# nearer or farther, up to the slack, counting for less.
SURROUNDINGS = 32
SURROUNDINGS_SLACK = 4
GRAM = 3
SURROUNDINGS_FOUND = 0.5
# A passage that is not distinct is looked for with one side of its surroundings when that side
# holds this many letters at least, and the other side is not found with it. A passage looked
# for or found by one side only needs this share of that side.
ONE_SIDE_LETTERS = SURROUNDINGS // 2
ONE_SIDE_FOUND = 0.7
BEFORE_FIELD = "before"
AFTER_FIELD = "after"
# Places whose surroundings are less like the passage's than the likest by this much, as the
# sum of the shares of both sides, are not its rivals.
SURROUNDINGS_MARGIN = 0.5
# The wider surroundings that tell apart places with surroundings as like, how much more like,
# as the sum of both sides, the place they tell must be, and the most places they are compared
# at.
WIDE_SURROUNDINGS = 256
WIDE_MARGIN = 0.5
WIDE_RIVALS = 8
# A passage of fewer letters than this could stand in many places for what it says, and is
# found only where its surroundings are found with it, or where both revisions hold it once. A
# character of a script written without spaces says as much as several letters.
DISTINCT_LETTERS = 40
UNSPACED_LETTERS = 3
# Words that differ a little: the share of a passage's letters that the fewest changes of a
# letter (one put in, left out, or read otherwise) must leave as they are.
NEAR_LIKENESS = 0.8
# How many fewer changes than at any other place the surroundings of a passage that is not
# distinct must take at the place it is found at by them.
NEAR_MARGIN = 2
# Where words that differ a little may lie is told by pieces of them that the other revision
# holds as they are: up to this many pieces of this many letters, each of which tells nothing
# when it stands in more places than this. The places that the most pieces agree on are
# compared whole, this many of them. Few letters are cut into shorter pieces, at least this
# many of them, and no shorter than this. Pieces stop being looked for once this many agree on
# a place.
SEED_LETTERS = 12
SEEDS_IN_SHORT_NEEDLE = 4
SHORTEST_SEED = 4
SEEDS = 24
SEED_PLACES = 64
AGREEING_SEEDS = 4
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
    # an anchor, the letters around it in the revision it was marked in, near and wide, where
    # it begins there as a share of that revision's letters, and how many times that revision
    # holds its letters, up to as many as are weighed.
    letters: str
    spaced: list[bool]
    breaks: list[bool]
    before: str
    after: str
    wide_before: str
    wide_after: str
    place: float
    copies: int


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
        wide_before=spelling.letters[max(0, begin - WIDE_SURROUNDINGS) : begin],
        wide_after=spelling.letters[finish : finish + WIDE_SURROUNDINGS],
        place=begin / max(1, len(spelling.letters)),
        copies=len(occurrences(spelling.letters, joined, OCCURRENCES_WEIGHED)) if joined else 0,
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


def grams(letters: str) -> dict[str, list[int]]:
    # The pieces a side is compared by, each with where it stands; a side shorter than a piece
    # is one piece.
    places: dict[str, list[int]] = {}
    for at in range(max(1, len(letters) - GRAM + 1)):
        places.setdefault(letters[at : at + GRAM], []).append(at)
    return places


def side_likeness(marked_side: str, held_side: str) -> float:
    # The share of the pieces of the letters on one side of a passage that stand as far from
    # a place on that side of it: a piece that stands nearer or farther by a letter or a few
    # counts for less, by the slack, so that surroundings that only repeat what stands around
    # them, as in a list, tell the place they stand beside. Both sides are read away from the
    # passage. A passage at an end of its revision is found at that end of another.
    if not marked_side:
        return float(not held_side)
    expected = grams(marked_side)
    held = grams(held_side)
    found = sum(
        max(
            [
                max(0.0, 1 - abs(other - at) / (SURROUNDINGS_SLACK + 1))
                for other in held.get(gram, [])
            ],
            default=0.0,
        )
        for gram, places in expected.items()
        for at in places
    )
    return found / sum(map(len, expected.values()))


def surroundings(passage: MarkedLetters, letters: str, start: int, end: int) -> tuple[float, float]:
    reach = SURROUNDINGS + SURROUNDINGS_SLACK
    return (
        side_likeness(passage.before[::-1], letters[max(0, start - reach) : start][::-1]),
        side_likeness(passage.after, letters[end : end + reach]),
    )


def wide_likeness(passage: MarkedLetters, letters: str, start: int, end: int) -> float:
    # How like the wide surroundings of a passage are those of a place, side by side: one less
    # the changes that make one side the other, as a share of the side's letters.
    sides = [
        (passage.wide_before[::-1], letters[max(0, start - WIDE_SURROUNDINGS) : start][::-1]),
        (passage.wide_after, letters[end : end + WIDE_SURROUNDINGS]),
    ]
    return sum(
        1 - prefix_distances(marked, held)[-1] / len(marked) if marked else float(not held)
        for marked, held in sides
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


def prefix_distances(needle: str, text: str) -> list[int]:
    # For each beginning of the text, from none of it to all of it, the fewest changes that
    # make the needle that beginning.
    if not needle:
        return list(range(len(text) + 1))
    return [len(needle), *end_distances(needle, text, AT_THE_BEGINNING)]


def partings(head: str, tail: str, text: str) -> list[int]:
    # The places the text may part at into what is made the head and what is made the tail by
    # the fewest changes.
    costs = [
        to_head + to_tail
        for to_head, to_tail in zip(
            prefix_distances(head, text),
            prefix_distances(tail[::-1], text[::-1])[::-1],
            strict=True,
        )
    ]
    return [at for at, cost in enumerate(costs) if cost == min(costs)]


def among_surroundings(passage: MarkedLetters, text: str) -> Stretch:
    # The passage within a text that is likest it with its surroundings: what stands between
    # the likest of the letters before it and the likest of those after it. Where a letter
    # could go with the passage or with what stands beside it, as a comma that one reading
    # puts in may, it goes with what stands beside it.
    start = partings(passage.before, passage.letters + passage.after, text)[-1]
    end = max(start, partings(passage.before + passage.letters, passage.after, text)[0])
    return Stretch(
        start=start, end=end, distance=prefix_distances(passage.letters, text[start:end])[-1]
    )


def nearest_stretches(needle: str, text: str) -> list[Stretch]:
    # The stretches of the text that the fewest changes make the needle: one for each place
    # they end at, ends within the slack of surroundings of each other being one place, as they are
    # where a letter could go with the stretch or beside it. A text that repeats itself, as
    # some recognitions write, holds the needle at several places.
    ends = end_distances(needle, text, ANYWHERE)
    distance = min(ends)
    places: list[int] = []
    for at, held in enumerate(ends):
        if held == distance and (not places or at - places[-1] > SURROUNDINGS_SLACK):
            places.append(at)
    return [
        Stretch(start=at - starts.index(min(starts)), end=at + 1, distance=distance)
        for at in places
        for starts in [end_distances(needle[::-1], text[: at + 1][::-1], AT_THE_BEGINNING)]
    ]


def is_near(stretch: Stretch, needle: str) -> bool:
    return stretch.distance <= len(needle) * (1 - NEAR_LIKENESS)


def near_stretches(letters: str, needle: str) -> list[Stretch]:
    # The stretches of the letters that are like the needle, looked for where the most of its
    # pieces stand as they are.
    seed_letters = max(SHORTEST_SEED, min(SEED_LETTERS, len(needle) // SEEDS_IN_SHORT_NEEDLE))
    count = min(SEEDS, len(needle) // seed_letters)
    step = (len(needle) - seed_letters) / max(1, count - 1)
    slack = int(len(needle) * (1 - NEAR_LIKENESS)) + 1
    # Pieces are looked for until enough of them agree on a place, since each search reads
    # all the letters.
    diagonals: list[tuple[int, int]] = []
    agreeing: Counter[int] = Counter()
    for offset in sorted({round(seed * step) for seed in range(count)}):
        found = occurrences(letters, needle[offset : offset + seed_letters], SEED_PLACES + 1)
        if len(found) <= SEED_PLACES:
            diagonals.extend((at - offset, offset) for at in found)
            agreeing.update({(at - offset) // (slack + seed_letters) for at in found})
        if max(agreeing.values(), default=0) >= AGREEING_SEEDS:
            break
    groups: list[list[tuple[int, int]]] = []
    for diagonal in sorted(diagonals):
        if groups and diagonal[0] - groups[-1][-1][0] <= slack + seed_letters:
            groups[-1].append(diagonal)
        else:
            groups.append([diagonal])
    likeliest = sorted(groups, key=lambda group: -len({offset for _, offset in group}))
    stretches: list[Stretch] = []
    for group in likeliest[:PLACES_COMPARED]:
        low = max(0, group[0][0] - slack)
        stretches.extend(
            Stretch(start=low + nearest.start, end=low + nearest.end, distance=nearest.distance)
            for nearest in nearest_stretches(
                needle, letters[low : group[-1][0] + len(needle) + slack]
            )
            if is_near(nearest, needle)
        )
    return stretches


def standing_out_widely(
    passage: MarkedLetters, held: Spelling, places: list[Stretch]
) -> list[Stretch]:
    # Of places that are as like the passage, the one whose wider surroundings stand out, as
    # those of one of two copies of a text do; none where none does, or where there are too
    # many to compare.
    if not 1 < len(places) <= WIDE_RIVALS:
        return []
    weighed = sorted(
        (
            (wide_likeness(passage, held.letters, place.start, place.end), number)
            for number, place in enumerate(places)
        ),
        reverse=True,
    )
    (likest, number), (next_likest, _) = weighed[:2]
    return [places[number]] if likest - next_likest >= WIDE_MARGIN else []


def exact_stretches(passage: MarkedLetters, held: Spelling, found: list[int]) -> list[Stretch]:
    # Where the other revision holds the passage's very letters. Of several places, those
    # with the likest surroundings are rivals; of them, the one whose wider surroundings stand
    # out, and failing that the nearest to where the passage stood. A passage that is not
    # distinct is found only where its surroundings are found and the place has no rival or
    # stands out by its wider surroundings, or where both revisions hold it once: surroundings
    # that stand beside several places, as the words around a date in a list of references
    # do, tell none of them.
    size = len(passage.letters)
    length = max(1, len(held.letters))
    weighed = [
        (sum(likeness), abs(at / length - passage.place), at, likeness)
        for at in found
        for likeness in [surroundings(passage, held.letters, at, at + size)]
    ]
    best = max(score for score, _, _, _ in weighed)
    rivals = sorted(
        (offset, at, likeness)
        for score, offset, at, likeness in weighed
        if score > best - SURROUNDINGS_MARGIN
    )
    places = [Stretch(start=at, end=at + size, distance=0) for _, at, _ in rivals]
    widely = standing_out_widely(passage, held, places)
    chosen = widely or places[:1]
    likeness = next(likeness for _, at, likeness in rivals if at == chosen[0].start)
    stands_out = max(likeness) >= SURROUNDINGS_FOUND and (bool(widely) or len(rivals) == 1)
    if is_distinct(passage.letters) or stands_out or len(found) == passage.copies == 1:
        return chosen
    return []


def is_surrounded(passage: MarkedLetters, held: Spelling, stretch: Stretch) -> bool:
    # Whether a side of the passage's surroundings that it has is found beside a place. Where
    # it has one side only, that side must be found more nearly whole.
    likeness = surroundings(passage, held.letters, stretch.start, stretch.end)
    present = [
        side_likeness
        for side, side_likeness in zip((passage.before, passage.after), likeness, strict=True)
        if side
    ]
    needed = SURROUNDINGS_FOUND if len(present) > 1 else ONE_SIDE_FOUND
    return any(side_likeness >= needed for side_likeness in present)


def surrounded_places(
    passage: MarkedLetters, held: Spelling, arounds: list[Stretch]
) -> list[Stretch]:
    # A passage that is not distinct, among the letters likest it with its surroundings, where
    # its surroundings are found beside it.
    likest = min((around.distance for around in arounds), default=0)
    return [
        stretch
        for around in arounds
        if around.distance - likest < NEAR_MARGIN
        for among in [among_surroundings(passage, held.letters[around.start : around.end])]
        if among.end > among.start and is_near(among, passage.letters)
        for stretch in [
            Stretch(
                start=around.start + among.start,
                end=around.start + among.end,
                distance=among.distance,
            )
        ]
        if is_surrounded(passage, held, stretch)
    ]


def distinct_stretches(passage: MarkedLetters, held: Spelling) -> list[Stretch]:
    # A distinct passage, where the other revision holds its very letters, or else letters a
    # little unlike them with each side of its surroundings found: a text a little unlike
    # with other surroundings, as the next of a list of references is, is another text. Of
    # several places of letters a little unlike, which are copies of one text, the one whose
    # wider surroundings stand out, and failing that the likest, then the one with the likest
    # surroundings, then the nearest to where it stood. Of copies of a text, one may have
    # changed: where the other revision holds fewer copies as they are than the passage's
    # revision holds, the passage may be a copy that changed.
    found = occurrences(held.letters, passage.letters, OCCURRENCES_WEIGHED)
    if found and len(found) >= passage.copies:
        return exact_stretches(passage, held, found)
    near = [
        stretch
        for stretch in near_stretches(held.letters, passage.letters)
        if is_surrounded(passage, held, stretch)
    ]
    if found:
        places = {at: Stretch(start=at, end=at + len(passage.letters), distance=0) for at in found}
        for stretch in near:
            places.setdefault(stretch.start, stretch)
        return standing_out_widely(passage, held, [*places.values()]) or exact_stretches(
            passage, held, found
        )
    length = max(1, len(held.letters))
    weighed = sorted(
        (
            -stretch.distance,
            sum(surroundings(passage, held.letters, stretch.start, stretch.end)),
            -abs(stretch.start / length - passage.place),
            number,
        )
        for number, stretch in enumerate(near)
    )
    return standing_out_widely(passage, held, near) or [near[number] for *_, number in weighed[-1:]]


def indistinct_stretches(passage: MarkedLetters, held: Spelling) -> list[Stretch]:
    # A passage that is not distinct is what stands with its surroundings: it is looked for
    # with them, and found among them, the very letters or letters a little unlike them; where
    # one side of them has changed, with the other side alone, when that side holds letters
    # enough. Only where its surroundings are not found so is it looked for by its letters
    # alone.
    sided = [
        passage,
        *(
            passage.model_copy(update={side: ""})
            for side, kept in ((AFTER_FIELD, passage.before), (BEFORE_FIELD, passage.after))
            if len(kept) >= ONE_SIDE_LETTERS
        ),
    ]
    # Where it is found so at several places, the place whose wider surroundings stand out;
    # where none does, it is doubtful which.
    for surrounded in sided:
        placed = surrounded_places(
            surrounded,
            held,
            near_stretches(held.letters, surrounded.before + surrounded.letters + surrounded.after),
        )
        if placed:
            return placed if len(placed) == 1 else standing_out_widely(passage, held, placed)
    found = occurrences(held.letters, passage.letters, OCCURRENCES_WEIGHED)
    return exact_stretches(passage, held, found) if found else []


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
    if is_distinct(letters.letters):
        chosen = distinct_stretches(letters, held)
    else:
        chosen = indistinct_stretches(letters, held)
    if not chosen:
        return Lost()
    pieces = pieces_of(held, chosen[0].start, chosen[0].end)
    return Found(
        passage=Passage(anchors=anchors_at(document, pieces)),
        exact=not chosen[0].distance and is_spaced_alike(letters, document, pieces),
    )
