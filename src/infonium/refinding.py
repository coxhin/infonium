import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter, OrderedDict
from functools import cache, partial
from itertools import accumulate, pairwise
from threading import Lock

from pydantic import BaseModel

from infonium.anchor import REFERENCE_MARK, Anchor, is_invisible, plain_character
from infonium.document import Block, Document, Table, TextRange, is_unspaced
from infonium.edit_distance import Stretch, nearest_stretches, partings, prefix_distances
from infonium.passage import Found, Lost, Passage, Standing

# A passage is looked for in another revision by its letters: the characters that
# anchor.comparable keeps, white space aside, so that neither spaces nor the places where
# blocks are cut decide what is found. What parts a table's cells and rows is a letter of its
# own, which no other character is, so that nothing is found across cells that was not marked
# across them.
SEPARATOR_LETTER = "\N{INFORMATION SEPARATOR ONE}"
# The letters right before and right after a passage that must stand right before and right
# after a place, on each side the passage has, for the place to be the passage's, and how many
# of them may be read otherwise.
NEIGHBOURS = 10
NEIGHBOUR_CHANGES = 1
# The letters on each side of a passage that is not distinct that it is looked for with, where
# its own letters differ a little.
SURROUNDINGS = 32
# Places that end within this many letters of each other are one place, as they are where a
# letter could go with the passage or beside it.
ONE_PLACE = 4
# The wider surroundings that tell apart places that are as good, how much more like, as the
# sum of both sides, the place they tell must be, and the most places they are compared at.
WIDE_SURROUNDINGS = 256
WIDE_MARGIN = 0.5
WIDE_FOUND = 1.0
WIDE_RIVALS = 8
# A passage of fewer letters than this could stand in many places for what it says. A
# character of a script written without spaces says as much as several letters.
DISTINCT_LETTERS = 40
UNSPACED_LETTERS = 3
# Words that differ a little: the share of a passage's letters that the fewest changes of a
# letter (one put in, left out, or read otherwise) must leave as they are.
NEAR_LIKENESS = 0.8
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
# The places of the very letters that are weighed, at most.
OCCURRENCES_WEIGHED = 2000
# The documents whose letters are kept for the next search, those searched last.
SPELLINGS_KEPT = 16
# A passage of several blocks is found block by block across at most this many new blocks
# between two of its blocks, which hold this many letters at most, as a heading's link to edit
# it does.
INSERTED_BLOCKS = 2
INSERTED_LETTERS = 16
# The first letter of the Unicode categories of marks set on a character.
MARK = "M"


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


class MarkedLetters(BaseModel):
    # A passage as letters: the letters from its first anchor's beginning to its last one's
    # end, those between its anchors included, which of them stand after white space and which
    # begin a block, the stretches of them that its anchors hold, the letters around it in the
    # revision it was marked in, where it begins there as a share of that revision's letters,
    # how many times that revision holds its letters, up to as many as are weighed, and whether
    # it begins and ends where a word does.
    letters: str
    spaced: list[bool]
    breaks: list[bool]
    anchored: list[TextRange]
    before: str
    after: str
    place: float
    copies: int
    whole_start: bool
    whole_end: bool


@cache
def letter_form(character: str) -> str:
    if character.isspace() or is_invisible(character):
        return ""
    return "".join(plain_character(character).split())


def separators(block: Block) -> set[str]:
    # The characters that part a table's cells and rows; other blocks have none.
    return {block.cell_separator, block.row_separator} if isinstance(block, Table) else set()


def form_in(block: Block, character: str) -> str:
    return SEPARATOR_LETTER if character in separators(block) else letter_form(character)


def letters_of(block: Block) -> str:
    return block.text.translate(
        {ord(character): form_in(block, character) for character in set(block.text)}
    )


def spelling_of(document: Document) -> Spelling:
    blocks = [letters_of(block) for block in document.blocks]
    return Spelling(letters="".join(blocks), starts=list(accumulate(map(len, blocks), initial=0)))


def block_letters(block: Block) -> BlockLetters:
    origins: list[int] = []
    spaced: list[bool] = []
    gap = False
    for offset, character in enumerate(block.text):
        form = form_in(block, character)
        if not form:
            gap = gap or character.isspace()
            continue
        origins.extend([offset] * len(form))
        spaced.extend([gap] + [False] * (len(form) - 1))
        gap = False
    return BlockLetters(origins=origins, spaced=spaced)


class KeptSpellings:
    # The letters of the documents searched last, kept by document id: what the search
    # prepares of a document's blocks, not the document. A document never changes under its
    # id, so its letters are dropped only for room. The documents themselves, parsed from the
    # library, are kept by owner and document id in standings.ParsedDocuments.
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


def is_word_character(character: str) -> bool:
    # A character a word of a script written with spaces is made of: a letter, a digit, or a
    # mark set on one.
    return (character.isalnum() or unicodedata.category(character).startswith(MARK)) and not (
        is_unspaced(character)
    )


def is_word_edge(text: str, offset: int) -> bool:
    # Whether the offset lies between two words, or between a word and what is no word, so
    # that a stretch beginning or ending there takes no word in part.
    inside = 0 < offset < len(text)
    return not (inside and is_word_character(text[offset - 1]) and is_word_character(text[offset]))


def is_mark(character: str) -> bool:
    return unicodedata.category(character).startswith(MARK)


def marked_letters(passage: Passage, document: Document, spelling: Spelling) -> MarkedLetters:
    # The letters between the anchors, as the end of a block that an anchor leaves out, are
    # looked for with the passage, so that its letters stand together as they do in the
    # document.
    ends: list[int] = []
    for anchor in passage.anchors:
        origins = block_letters(document.blocks[anchor.block_number]).origins
        base = spelling.starts[anchor.block_number]
        ends.extend(
            [base + bisect_left(origins, anchor.start), base + bisect_left(origins, anchor.end)]
        )
    begin, finish = ends[0], ends[-1]
    spaced: list[bool] = []
    breaks: list[bool] = []
    for piece in pieces_of(spelling, begin, finish):
        layout = block_letters(document.blocks[piece.block_number])
        spaced.extend(layout.spaced[piece.start : piece.end])
        breaks.extend([True] + [False] * (piece.end - piece.start - 1))
    letters = spelling.letters[begin:finish]
    first, last = passage.anchors[0], passage.anchors[-1]
    return MarkedLetters(
        letters=letters,
        spaced=spaced,
        breaks=breaks,
        anchored=[
            TextRange(start=start - begin, end=end - begin)
            for start, end in zip(ends[::2], ends[1::2], strict=True)
            if end > start
        ],
        before=spelling.letters[max(0, begin - WIDE_SURROUNDINGS) : begin],
        after=spelling.letters[finish : finish + WIDE_SURROUNDINGS],
        place=begin / max(1, len(spelling.letters)),
        copies=len(occurrences(spelling.letters, letters, OCCURRENCES_WEIGHED)) if letters else 0,
        whole_start=is_word_edge(document.blocks[first.block_number].text, first.start),
        whole_end=is_word_edge(document.blocks[last.block_number].text, last.end),
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
    # An anchor for each piece, widened to whole characters with their marks.
    anchors: list[Anchor] = []
    for piece in pieces:
        text = document.blocks[piece.block_number].text
        origins = block_letters(document.blocks[piece.block_number]).origins
        start, end = origins[piece.start], origins[piece.end - 1] + 1
        # What parts a table's cells is a letter, but white space at an anchor's ends. A mark
        # goes with the character it is set on; a mark that stands on nothing, after white
        # space or at a block's beginning, goes with neither.
        while start < end and (
            text[start].isspace()
            or (is_mark(text[start]) and (start == 0 or text[start - 1].isspace()))
        ):
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        while 0 < start < end and is_mark(text[start]) and not text[start - 1].isspace():
            start -= 1
        while start < end < len(text) and is_mark(text[end]):
            end += 1
        if end == start:
            continue
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
    # it. Where a block of the document begins, a space may stand or not, as where a paragraph
    # is cut in two; where a block of the passage's revision began, the document must have
    # white space unless one block continues the other in a script written without spaces.
    spaced: list[bool] = []
    breaks: list[bool] = []
    for piece in pieces:
        layout = block_letters(document.blocks[piece.block_number])
        spaced.extend(layout.spaced[piece.start : piece.end])
        breaks.extend([True] + [False] * (piece.end - piece.start - 1))
    unspaced = [is_unspaced(letter) for letter in passage.letters]
    return all(
        held_break
        or (held_space if marked_break else marked_space == held_space)
        or (marked_break and (unspaced[at] or unspaced[at - 1]))
        for at, (marked_space, marked_break, held_space, held_break) in enumerate(
            zip(passage.spaced, passage.breaks, spaced, breaks, strict=True)
        )
        if at
    )


def is_side_found(marked: str, held: str) -> bool:
    # Whether the letters next to a place on one side are those next to the passage, read away
    # from it, with one letter changed at most; but none where they hold a digit, since a
    # changed number next to the place, as the next entry of a numbered list has, makes it
    # another place.
    changes = 0 if any(letter.isdigit() for letter in marked) else NEIGHBOUR_CHANGES
    return not marked or min(prefix_distances(marked, held)) <= changes


def without_new_blocks(held: Spelling, low: int, high: int, kept_letters: str) -> str:
    # The letters from low to high, but those of short blocks that the passage's surroundings
    # do not hold, such as a heading's link to edit it that a later reading puts after every
    # heading.
    low, high = max(0, low), min(len(held.letters), high)
    first = bisect_right(held.starts, low) - 1
    kept: list[str] = []
    for begin, finish in pairwise(held.starts[first:]):
        if begin >= high:
            break
        letters = held.letters[begin:finish]
        if finish - begin > INSERTED_LETTERS or letters in kept_letters:
            kept.append(held.letters[max(begin, low) : min(finish, high)])
    return "".join(kept)


def has_neighbours(passage: MarkedLetters, held: Spelling, stretch: Stretch) -> bool:
    # Whether the letters that stood right before and right after the passage stand right
    # before and right after the place, on each side the passage has, as they are or with new
    # short blocks between. Footnote marks are left out on both sides: a new revision numbers
    # its references anew.
    before = REFERENCE_MARK.sub("", passage.before)[-NEIGHBOURS:]
    after = REFERENCE_MARK.sub("", passage.after)[:NEIGHBOURS]
    reach = NEIGHBOURS + NEIGHBOUR_CHANGES
    wider = reach + INSERTED_LETTERS * INSERTED_BLOCKS
    befores = [
        REFERENCE_MARK.sub("", side)[-reach:]
        for side in (
            held.letters[max(0, stretch.start - wider) : stretch.start],
            without_new_blocks(held, stretch.start - 2 * wider, stretch.start, passage.before),
        )
    ]
    afters = [
        REFERENCE_MARK.sub("", side)[:reach]
        for side in (
            held.letters[stretch.end : stretch.end + wider],
            without_new_blocks(held, stretch.end, stretch.end + 2 * wider, passage.after),
        )
    ]
    return any(is_side_found(before[::-1], side[::-1]) for side in befores) and any(
        is_side_found(after, side) for side in afters
    )


def is_placed(passage: MarkedLetters, held: Spelling, place: Stretch) -> bool:
    # Whether a place is the passage's by what stands around it: right next to it, the letters
    # that stood next to the passage, and further off, letters as like those that stood
    # around it as WIDE_FOUND asks, so that the same words beside the same words in another
    # entry of a list of references are not taken for it.
    return (
        has_neighbours(passage, held, place)
        and wide_likeness(passage, held.letters, place) >= WIDE_FOUND
    )


def wide_likeness(passage: MarkedLetters, letters: str, stretch: Stretch) -> float:
    # How like the wide surroundings of a passage are those of a place, side by side: one less
    # the changes that make one side the other, as a share of the side's letters.
    sides = [
        (
            passage.before[::-1],
            letters[max(0, stretch.start - WIDE_SURROUNDINGS) : stretch.start][::-1],
        ),
        (passage.after, letters[stretch.end : stretch.end + WIDE_SURROUNDINGS]),
    ]
    return sum(
        1 - prefix_distances(marked, held)[-1] / len(marked) if marked else float(not held)
        for marked, held in sides
    )


def is_whole(passage: MarkedLetters, document: Document, held: Spelling, stretch: Stretch) -> bool:
    # Whether a place begins and ends where a word does, where the passage did.
    anchors = anchors_at(document, pieces_of(held, stretch.start, stretch.end))
    return bool(anchors) and (
        (
            not passage.whole_start
            or is_word_edge(document.blocks[anchors[0].block_number].text, anchors[0].start)
        )
        and (
            not passage.whole_end
            or is_word_edge(document.blocks[anchors[-1].block_number].text, anchors[-1].end)
        )
    )


def is_near(stretch: Stretch, needle: str) -> bool:
    return stretch.distance <= len(needle) * (1 - NEAR_LIKENESS)


def says_the_same(passage: MarkedLetters, held: str) -> bool:
    # Words a little unlike a passage's never differ in a digit, which would make them another
    # statement, nor in what parts a table's cells. The number of a footnote mark is no
    # statement: a new revision numbers its references anew.
    def kept(letters: str) -> str:
        return "".join(
            letter
            for letter in REFERENCE_MARK.sub("", letters)
            if letter.isdigit() or letter == SEPARATOR_LETTER
        )

    return kept(passage.letters) == kept(held)


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
                needle, letters[low : group[-1][0] + len(needle) + slack], ONE_PLACE
            )
            if is_near(nearest, needle)
        )
    return stretches


def near_places(passage: MarkedLetters, held: Spelling) -> list[Stretch]:
    # Where the other revision holds letters a little unlike the passage's: a distinct
    # passage by its letters, any other with the letters around it, among which it is then
    # found.
    if is_distinct(passage.letters):
        return near_stretches(held.letters, passage.letters)
    before, after = passage.before[-SURROUNDINGS:], passage.after[:SURROUNDINGS]
    places: list[Stretch] = []
    for around in near_stretches(held.letters, before + passage.letters + after):
        text = held.letters[around.start : around.end]
        start = partings(before, passage.letters + after, text)[-1]
        end = max(start, partings(before + passage.letters, after, text)[0])
        distance = prefix_distances(passage.letters, text[start:end])[-1]
        places.append(
            Stretch(start=around.start + start, end=around.start + end, distance=distance)
        )
    return places


def one_place(passage: MarkedLetters, held: Spelling, places: list[Stretch]) -> list[Stretch]:
    # The one place of several that are as good, or of several, the one whose wider
    # surroundings stand out, as those of one of two copies of a text do; none where none
    # does, or where there are too many to compare.
    distinct: list[Stretch] = []
    for place in sorted(places, key=lambda place: (place.distance, place.start)):
        if all(abs(place.start - other.start) > ONE_PLACE for other in distinct):
            distinct.append(place)
    if len(distinct) <= 1:
        return distinct
    if len(distinct) > WIDE_RIVALS:
        return []
    weighed = sorted(
        (
            (wide_likeness(passage, held.letters, place), number)
            for number, place in enumerate(distinct)
        ),
        reverse=True,
    )
    (likest, number), (next_likest, _) = weighed[:2]
    return [distinct[number]] if likest - next_likest >= WIDE_MARGIN else []


def found_places(passage: MarkedLetters, document: Document, held: Spelling) -> list[Stretch]:
    # A distinct passage that each revision holds once is found by its letters. Any other is
    # found only where the letters that stood right before and right after it stand right
    # before and right after it again; a passage whose own place is gone is lost, though its
    # words stand elsewhere. Where the other revision does not hold its very letters so, it is
    # found where it holds letters a little unlike them that say the same, so placed. Where it
    # holds fewer copies of the very letters than the passage's revision held, the copy that
    # changed may be the passage's, and places a little unlike are weighed with the very ones.
    # Of several places, the one whose wide surroundings stand out; none where none does. A
    # passage that began and ended with a word begins and ends with one where it is found.
    exact = [
        Stretch(start=at, end=at + len(passage.letters), distance=0)
        for at in occurrences(held.letters, passage.letters, OCCURRENCES_WEIGHED)
    ]
    if is_distinct(passage.letters) and passage.copies == 1 and len(exact) == 1:
        return [place for place in exact if is_whole(passage, document, held, place)]
    neighboured = [
        place
        for place in exact
        if is_placed(passage, held, place) and is_whole(passage, document, held, place)
    ]
    if neighboured and len(exact) >= passage.copies:
        return one_place(passage, held, neighboured)
    near = [
        place
        for place in map(partial(within_cells, held.letters), near_places(passage, held))
        if place.end > place.start
        and is_near(place, passage.letters)
        and says_the_same(passage, held.letters[place.start : place.end])
        and is_placed(passage, held, place)
        and is_whole(passage, document, held, place)
    ]
    return one_place(passage, held, neighboured + near)


def within_cells(letters: str, place: Stretch) -> Stretch:
    # A place a little unlike a passage, without what parts a table's cells at either end.
    start, end = place.start, place.end
    while start < end and letters[start] == SEPARATOR_LETTER:
        start += 1
    while end > start and letters[end - 1] == SEPARATOR_LETTER:
        end -= 1
    return place.model_copy(update={"start": start, "end": end})


def is_block_start(held: Spelling, at: int) -> bool:
    found = bisect_left(held.starts, at)
    return found < len(held.starts) and held.starts[found] == at


def next_part(held: Spelling, part: str, after: int, last: bool) -> list[int]:
    # Where the next part of a passage found block by block begins: at the beginning of a
    # block that follows the part before it, or follows it past new blocks of few letters, and
    # ending at a block's end unless it is the last part.
    block = bisect_left(held.starts, after)
    for start in held.starts[block : block + INSERTED_BLOCKS + 1]:
        fits = held.letters.startswith(part, start) and (
            last or is_block_start(held, start + len(part))
        )
        if start - after > INSERTED_LETTERS:
            return []
        if fits:
            return [start]
    return []


def parts_apart(passage: MarkedLetters, document: Document, held: Spelling) -> list[Piece]:
    # A passage of several blocks that the other revision does not hold as one stretch, since
    # new blocks came to stand between its blocks, as a heading's link to edit it does: found
    # block by block, each part as it is, in order, with nothing but new blocks of few letters
    # between them, and with what stood right before and right after the passage, and around
    # it, standing so again; and only at one place.
    ranges = passage.anchored
    breaks_at_seams = len(ranges) > 1 and all(
        earlier.end == later.start and passage.breaks[later.start]
        for earlier, later in pairwise(ranges)
    )
    if not breaks_at_seams:
        return []
    parts = [passage.letters[part.start : part.end] for part in ranges]
    chains: list[list[Stretch]] = []
    for at in occurrences(held.letters, parts[0], OCCURRENCES_WEIGHED):
        chain = [Stretch(start=at, end=at + len(parts[0]), distance=0)]
        for number, part in enumerate(parts[1:], start=2):
            starts = (
                next_part(held, part, chain[-1].end, number == len(parts))
                if is_block_start(held, chain[-1].end)
                else []
            )
            chain.extend(
                Stretch(start=start, end=start + len(part), distance=0) for start in starts
            )
        whole = Stretch(start=chain[0].start, end=chain[-1].end, distance=0)
        if (
            len(chain) == len(parts)
            and whole.end - whole.start > len(passage.letters)
            and is_placed(passage, held, whole)
            and is_whole(passage, document, held, whole)
        ):
            chains.append(chain)
    if len(chains) != 1:
        return []
    return [piece for part in chains[0] for piece in pieces_of(held, part.start, part.end)]


def standing_in(passage: Passage, marked_in: Document, document: Document) -> Standing:
    # Where a passage that was marked in one revision of a source stands in another: in the
    # revision it was marked in, where it is; in another, where that holds the passage's
    # letters, the very ones or ones a little unlike them, so that the place is the passage's;
    # and lost where it holds neither, or where it is doubtful which of their places is the
    # passage's.
    if document.id == marked_in.id:
        return Found(passage=passage, exact=True)
    spellings = kept_spellings()
    held = spellings.spelling(document)
    letters = marked_letters(passage, marked_in, spellings.spelling(marked_in))
    chosen = found_places(letters, document, held) if letters.letters else []
    if chosen:
        anchors, exact = placed_anchors(letters, document, held, chosen[0])
    else:
        apart = parts_apart(letters, document, held) if letters.letters else []
        anchors, exact = anchors_at(document, apart), True
    return Found(passage=Passage(anchors=anchors), exact=exact) if anchors else Lost()


def placed_anchors(
    letters: MarkedLetters, document: Document, held: Spelling, stretch: Stretch
) -> tuple[list[Anchor], bool]:
    # The anchors of a passage at the place it is found at, and whether the document holds its
    # very words there. Where the very letters are found, each anchor's letters stand where
    # they stood among them, and the letters between the anchors are left out again, but where
    # two anchors' letters come to lie in one block, which holds one anchor of a passage.
    pieces = pieces_of(held, stretch.start, stretch.end)
    parts = (
        [
            piece
            for anchor in letters.anchored
            for piece in pieces_of(held, stretch.start + anchor.start, stretch.start + anchor.end)
        ]
        if not stretch.distance
        else pieces
    )
    anchored: list[Piece] = []
    for part in parts:
        if anchored and anchored[-1].block_number == part.block_number:
            anchored[-1] = anchored[-1].model_copy(update={"end": part.end})
        else:
            anchored.append(part)
    return (
        anchors_at(document, anchored),
        not stretch.distance and is_spaced_alike(letters, document, pieces),
    )
