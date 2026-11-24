import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter, OrderedDict
from functools import cache
from itertools import accumulate, pairwise
from threading import Lock

from pydantic import BaseModel

from infonium.anchor import REFERENCE_MARK, Anchor, is_invisible, plain_character
from infonium.document import Block, Document, Table, TextRange, is_unspaced
from infonium.edit_distance import Stretch, nearest_stretches, partings, prefix_distances
from infonium.passage import Found, Lost, Passage, Standing

# A passage is looked for in another revision by its letters: the characters that
# anchor.comparable keeps, white space aside, so that neither spaces nor the places where
# blocks are cut decide what is found. Where a table's cells and rows part is kept beside the
# letters, so that nothing is found across more cells than it was marked across, and the
# letters next to a cell that count are those of its own row.
# The letters right before and right after a passage that must stand right before and right
# after a place, on each side the passage has, for the place to be the passage's, and how many
# of them may be read otherwise.
NEIGHBOURS = 10
NEIGHBOUR_CHANGES = 1
# Where the revision a passage was marked in holds other copies of it, a place is the
# passage's only where the RIVAL_SIDE letters on each side of it are liker those that stood
# around the marked copy than those around any other copy, by RIVAL_MARGIN changes at least.
# A copy whose QUICK_SIDE letters right next to it are not those next to the place on either
# side is no rival.
RIVAL_SIDE = 32
# A table's row tells a cell's place when it holds this many letters beside the cell.
ROW_LETTERS = 4
RIVAL_MARGIN = 2
QUICK_SIDE = 4
# Another copy of the passage's very letters in the other revision whose RIVAL_SIDE letters on
# each side stand at their distance as they stood around the passage, more than this share of
# them over both sides together, leaves the place in doubt.
TOLD_APART = 1.5
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
WIDE_FOUND = 0.5
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
# A place of words a little unlike a passage's is widened by this many letters at most to the
# edges of the words it begins and ends in.
WORD_SLACK = 3
# The first letter of the Unicode categories of marks set on a character.
MARK = "M"


class Spelling(BaseModel):
    # A document's letters, block after block, the letter each block begins at, the last
    # number being the count of letters, and the letters at which a table's cell or row begins,
    # a table's block counting as a row.
    letters: str
    starts: list[int]
    cell_edges: list[int]
    row_edges: list[int]


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
    begin: int
    finish: int
    parts: int
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


def letters_of(block: Block) -> str:
    return block.text.translate(
        {ord(character): letter_form(character) for character in set(block.text)}
    )


def spelling_of(document: Document) -> Spelling:
    blocks = [letters_of(block) for block in document.blocks]
    starts = list(accumulate(map(len, blocks), initial=0))
    cells: set[int] = set()
    rows: set[int] = set()
    for number, block in enumerate(document.blocks):
        if not isinstance(block, Table):
            continue
        base, origins = starts[number], block_letters(block).origins
        rows.update({base, starts[number + 1]})
        for offset, character in enumerate(block.text):
            if character in (block.cell_separator, block.row_separator):
                edge = base + bisect_left(origins, offset)
                cells.add(edge)
                if character == block.row_separator:
                    rows.add(edge)
    return Spelling(
        letters="".join(blocks),
        starts=starts,
        cell_edges=sorted(cells | rows),
        row_edges=sorted(rows),
    )


def block_letters(block: Block) -> BlockLetters:
    origins: list[int] = []
    spaced: list[bool] = []
    gap = False
    for offset, character in enumerate(block.text):
        form = letter_form(character)
        if not form:
            gap = gap or character.isspace()
            continue
        origins.extend([offset] * len(form))
        spaced.extend([gap] + [False] * (len(form) - 1))
        gap = False
    return BlockLetters(origins=origins, spaced=spaced)


def edges_within(edges: list[int], start: int, end: int) -> list[int]:
    # The edges that lie inside a stretch, between two of its letters.
    return edges[bisect_right(edges, start) : bisect_left(edges, end)]


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
    seams = set(edges_within(spelling.cell_edges, begin, finish))
    seams |= set(edges_within(spelling.starts, begin, finish))
    return MarkedLetters(
        letters=letters,
        spaced=spaced,
        breaks=breaks,
        anchored=[
            TextRange(start=start - begin, end=end - begin)
            for start, end in zip(ends[::2], ends[1::2], strict=True)
            if end > start
        ],
        begin=begin,
        finish=finish,
        parts=len(seams),
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
    # white space unless one block continues the other, both in a script written without
    # spaces.
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
        or (marked_break and unspaced[at] and unspaced[at - 1])
        for at, (marked_space, marked_break, held_space, held_break) in enumerate(
            zip(passage.spaced, passage.breaks, spaced, breaks, strict=True)
        )
        if at
    )


def is_side_found(marked: str, held: str) -> bool:
    # Whether the letters next to a place on one side are those next to the passage, read away
    # from it, with one letter changed at most; but none where they hold a digit, since a
    # changed number next to the place, as the next entry of a numbered list has, makes it
    # another place. Where the passage has no letters on a side, nothing is asked of it; where
    # it has some and the place has none, the side is not found.
    changes = 0 if any(letter.isdigit() for letter in marked) else NEIGHBOUR_CHANGES
    return not marked or (bool(held) and min(prefix_distances(marked, held)) <= changes)


def side_before(spelling: Spelling, at: int, reach: int, rows: list[int]) -> str:
    # The letters right before a letter, back to the beginning of its table's row where the
    # rows are given.
    low = max(0, at - reach)
    return spelling.letters[max([low, *edges_within(rows, low - 1, at + 1)]) : at]


def side_after(spelling: Spelling, at: int, reach: int, rows: list[int]) -> str:
    # The letters from a letter on, up to the end of its table's row where the rows are given.
    high = min(len(spelling.letters), at + reach)
    return spelling.letters[at : min([high, *edges_within(rows, at - 1, high + 1)])]


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


def sides_found(
    passage: MarkedLetters,
    marked: Spelling,
    held: Spelling,
    stretch: Stretch,
    rows: tuple[list[int], list[int]],
) -> tuple[bool, bool]:
    # Whether the letters that stood right before, and those right after, the passage stand so
    # beside the place, on each side the passage has, as they are or with new short blocks
    # between, within a table's row where the rows of each revision are given. Footnote marks
    # are left out on both sides: a new revision numbers its references anew.
    reach = NEIGHBOURS + NEIGHBOUR_CHANGES
    wider = reach + INSERTED_LETTERS * INSERTED_BLOCKS
    before = REFERENCE_MARK.sub("", side_before(marked, passage.begin, wider, rows[0]))
    after = REFERENCE_MARK.sub("", side_after(marked, passage.finish, wider, rows[0]))
    row_before = side_before(held, stretch.start, wider, rows[1])
    row_after = side_after(held, stretch.end, wider, rows[1])
    befores = [
        REFERENCE_MARK.sub("", side)[-reach:]
        for side in (
            row_before,
            without_new_blocks(held, stretch.start - 2 * wider, stretch.start, passage.before)
            if row_before
            else "",
        )
    ]
    afters = [
        REFERENCE_MARK.sub("", side)[:reach]
        for side in (
            row_after,
            without_new_blocks(held, stretch.end, stretch.end + 2 * wider, passage.after)
            if row_after
            else "",
        )
    ]
    return (
        any(is_side_found(before[-NEIGHBOURS:][::-1], side[::-1]) for side in befores),
        any(is_side_found(after[:NEIGHBOURS], side) for side in afters),
    )


def has_neighbours(
    passage: MarkedLetters, marked: Spelling, held: Spelling, stretch: Stretch
) -> bool:
    # Whether the letters that stood right before and right after the passage stand right
    # before and right after the place. In a table, the letters that count are first those of
    # the passage's own row, where its row holds ROW_LETTERS beside it at least, and then
    # those of the rows on one side of it, so that a row put in above or below a cell leaves
    # it its place and a row moved elsewhere does not take it along; a cell of a shorter row
    # is told by the rows around it.
    across: tuple[list[int], list[int]] = ([], [])
    in_row = side_before(marked, passage.begin, NEIGHBOURS, marked.row_edges) + side_after(
        marked, passage.finish, NEIGHBOURS, marked.row_edges
    )
    if len(in_row) < ROW_LETTERS or in_row == side_before(
        marked, passage.begin, NEIGHBOURS, []
    ) + side_after(marked, passage.finish, NEIGHBOURS, []):
        return all(sides_found(passage, marked, held, stretch, across))
    return all(
        sides_found(passage, marked, held, stretch, (marked.row_edges, held.row_edges))
    ) and any(sides_found(passage, marked, held, stretch, across))


def side_changes(
    passage: MarkedLetters, marked: Spelling, at: tuple[int, int], held: Spelling, place: Stretch
) -> tuple[int, int]:
    # The changes that make the letters before and after a copy in the passage's revision those
    # before and after a place: on each side, as many as both the marked copy and the place
    # have, RIVAL_SIDE at most, so that a document's edge beside the marked copy or the place
    # tells nothing; a copy with fewer letters on a side lacks the rest.
    reach_before = min(RIVAL_SIDE, passage.begin, place.start)
    reach_after = min(
        RIVAL_SIDE, len(marked.letters) - passage.finish, len(held.letters) - place.end
    )
    before, after = (
        prefix_distances(one, other)[-1]
        for one, other in (
            (
                marked.letters[max(0, at[0] - reach_before) : at[0]][::-1],
                held.letters[place.start - reach_before : place.start][::-1],
            ),
            (
                marked.letters[at[1] : at[1] + reach_after],
                held.letters[place.end : place.end + reach_after],
            ),
        )
    )
    return before, after


def is_copy_s_place(
    passage: MarkedLetters, marked: Spelling, held: Spelling, place: Stretch
) -> bool:
    # Whether what stands around a place is liker what stood around the marked copy than what
    # stood around any other copy of the passage in its revision: in all by RIVAL_MARGIN
    # changes, and on neither side liker another copy's, by as many, where that copy's side
    # stands there nearly as it was (no more than a quarter of RIVAL_SIDE changed). A place
    # that could as well be another copy's is doubtful.
    size = passage.finish - passage.begin
    own = side_changes(passage, marked, (passage.begin, passage.finish), held, place)
    held_before = held.letters[max(0, place.start - QUICK_SIDE) : place.start]
    held_after = held.letters[place.end : place.end + QUICK_SIDE]
    return all(
        not any(
            other_side + RIVAL_MARGIN <= own_side and other_side <= RIVAL_SIDE // 4
            for other_side, own_side in zip(other, own, strict=True)
        )
        and sum(other) >= sum(own) + RIVAL_MARGIN
        for copy in occurrences(marked.letters, passage.letters, OCCURRENCES_WEIGHED)
        if copy != passage.begin
        and (
            marked.letters[max(0, copy - QUICK_SIDE) : copy] == held_before
            or marked.letters[copy + size : copy + size + QUICK_SIDE] == held_after
        )
        for other in [side_changes(passage, marked, (copy, copy + size), held, place)]
    )


def positional_likeness(marked: str, held: str) -> float:
    # The share of a side's letters, read away from the passage, that stand at the same
    # distance from a place.
    return sum(one == other for one, other in zip(marked, held, strict=False)) / max(1, len(marked))


def is_told_apart(
    passage: MarkedLetters,
    marked: Spelling,
    held: Spelling,
    place: Stretch,
    copies: list[Stretch],
) -> bool:
    # Whether no other copy of the passage's very letters in the other revision has the
    # passage's neighbours too, and surroundings that stand as those of the passage stood,
    # letter for letter, more than TOLD_APART of them over both sides of RIVAL_SIDE letters:
    # such a copy leaves the place in doubt.
    marked_sides = (passage.before[-RIVAL_SIDE:][::-1], passage.after[:RIVAL_SIDE])
    return not any(
        positional_likeness(
            marked_sides[0], held.letters[max(0, copy.start - RIVAL_SIDE) : copy.start][::-1]
        )
        + positional_likeness(marked_sides[1], held.letters[copy.end : copy.end + RIVAL_SIDE])
        > TOLD_APART
        for copy in copies
        if copy.start != place.start and has_neighbours(passage, marked, held, copy)
    )


def crosses_no_more_cells(passage: MarkedLetters, held: Spelling, place: Stretch) -> bool:
    # Whether a place runs across no more of a table's cells than the passage was marked
    # across, the ends of the blocks it ran through counted as cells.
    return len(edges_within(held.cell_edges, place.start, place.end)) <= passage.parts


def is_placed(passage: MarkedLetters, marked: Spelling, held: Spelling, place: Stretch) -> bool:
    # Whether a place is the passage's by what stands around it: right next to it, the letters
    # that stood next to the passage; further off, letters as like those that stood around it
    # as WIDE_FOUND asks, so that the same words beside the same words in another entry of a
    # list of references are not taken for it; of the RIVAL_SIDE letters on each side, no
    # more than RIVAL_SIDE changed in all, half of them; and none of its other copies told as
    # well.
    return (
        has_neighbours(passage, marked, held, place)
        and wide_likeness(passage, held.letters, place) >= WIDE_FOUND
        and sum(side_changes(passage, marked, (passage.begin, passage.finish), held, place))
        <= RIVAL_SIDE
        and (passage.copies <= 1 or is_copy_s_place(passage, marked, held, place))
    )


def wide_likeness(passage: MarkedLetters, letters: str, stretch: Stretch) -> float:
    # How like the wide surroundings of a passage are those of a place, side by side: one less
    # the changes that make one side the other, as a share of the side's letters. A side the
    # passage lacks, at its revision's edge, tells nothing, and counts as alike.
    sides = [
        (
            passage.before[::-1],
            letters[max(0, stretch.start - WIDE_SURROUNDINGS) : stretch.start][::-1],
        ),
        (passage.after, letters[stretch.end : stretch.end + WIDE_SURROUNDINGS]),
    ]
    return sum(
        1 - min(prefix_distances(marked, held)) / len(marked) if marked else 1.0
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
    # statement. The number of a footnote mark is no statement: a new revision numbers its
    # references anew.
    def kept(letters: str) -> str:
        return "".join(letter for letter in REFERENCE_MARK.sub("", letters) if letter.isdigit())

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


def one_place(
    passage: MarkedLetters, marked: Spelling, held: Spelling, places: list[Stretch]
) -> list[Stretch]:
    # The one place of several that are as good: the one whose surroundings are liker the
    # passage's than any other's by RIVAL_MARGIN changes, or failing that the one whose wider
    # surroundings stand out; none where none does, or where there are too many to compare.
    # Near places that end within ONE_PLACE letters of each other are one place.
    distinct: list[Stretch] = []
    for place in sorted(places, key=lambda place: (place.distance, place.start)):
        if all(
            (place.start, place.end) != (other.start, other.end)
            and (place.distance == 0 or abs(place.start - other.start) > ONE_PLACE)
            for other in distinct
        ):
            distinct.append(place)
    if len(distinct) <= 1:
        return distinct
    if len(distinct) > WIDE_RIVALS:
        return []
    own = (passage.begin, passage.finish)
    changes = sorted(
        (sum(side_changes(passage, marked, own, held, place)), number)
        for number, place in enumerate(distinct)
    )
    if changes[1][0] - changes[0][0] >= RIVAL_MARGIN:
        return [distinct[changes[0][1]]]
    weighed = sorted(
        (
            (wide_likeness(passage, held.letters, place), number)
            for number, place in enumerate(distinct)
        ),
        reverse=True,
    )
    (likest, number), (next_likest, _) = weighed[:2]
    return [distinct[number]] if likest - next_likest >= WIDE_MARGIN else []


def found_places(
    passage: MarkedLetters, document: Document, marked: Spelling, held: Spelling
) -> list[Stretch]:
    # A distinct passage that each revision holds once is found by its letters. Any other is
    # found only where the letters that stood right before and right after it stand right
    # before and right after it again, and where its other copies, if any, would not stand as
    # well; a passage whose own place is gone is lost, though its words stand elsewhere. Where
    # the other revision does not hold its very letters so, it is found where it holds letters
    # a little unlike them that say the same, so placed. Where it holds fewer copies of the
    # very letters than the passage's revision held, the copy that changed may be the
    # passage's, and places a little unlike are weighed with the very ones. A passage that
    # began and ended with a word begins and ends with one where it is found, and runs across
    # no more of a table's cells.
    def is_shaped(place: Stretch) -> bool:
        return is_whole(passage, document, held, place) and crosses_no_more_cells(
            passage, held, place
        )

    exact = [
        Stretch(start=at, end=at + len(passage.letters), distance=0)
        for at in occurrences(held.letters, passage.letters, OCCURRENCES_WEIGHED)
    ]
    if is_distinct(passage.letters) and passage.copies == 1 and len(exact) == 1:
        return [place for place in exact if is_shaped(place)]
    # A passage that each revision holds once, which no other copy can be taken for, is found
    # where the letters on one side of it stand as they stood, the other side having changed.
    if passage.copies == 1 and len(exact) == 1:
        return [
            place
            for place in exact
            if is_shaped(place) and any(sides_found(passage, marked, held, place, ([], [])))
        ]
    neighboured = [
        place
        for place in exact
        if is_shaped(place)
        and is_placed(passage, marked, held, place)
        and is_told_apart(passage, marked, held, place, exact)
    ]
    if neighboured and len(exact) >= passage.copies:
        return one_place(passage, marked, held, neighboured)
    near = [
        place
        for place in (
            to_word_edges(passage, document, held, place) for place in near_places(passage, held)
        )
        if place.end > place.start
        and is_near(place, passage.letters)
        and says_the_same(passage, held.letters[place.start : place.end])
        and is_shaped(place)
        and is_placed(passage, marked, held, place)
    ]
    return one_place(passage, marked, held, neighboured + near)


def to_word_edges(
    passage: MarkedLetters, document: Document, held: Spelling, place: Stretch
) -> Stretch:
    # A place of letters a little unlike a passage that begins or ends inside a word, as one
    # whose first letter was read as two may, widened by WORD_SLACK letters at most to the
    # word's edges, where the passage began or ended at a word's edge.
    def edge_offset(at: int) -> tuple[str, int]:
        block = bisect_right(held.starts, at) - 1
        layout = block_letters(document.blocks[block])
        letter = at - held.starts[block]
        text = document.blocks[block].text
        return text, layout.origins[letter] if letter < len(layout.origins) else len(text)

    start, end = place.start, place.end
    for _ in range(WORD_SLACK):
        text, offset = edge_offset(start)
        if not passage.whole_start or is_word_edge(text, offset) or start == 0:
            break
        start -= 1
    for _ in range(WORD_SLACK):
        text, offset = edge_offset(end - 1)
        if not passage.whole_end or is_word_edge(text, offset + 1) or end == len(held.letters):
            break
        end += 1
    return Stretch(
        start=start,
        end=end,
        distance=prefix_distances(passage.letters, held.letters[start:end])[-1],
    )


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


def parts_apart(
    passage: MarkedLetters, document: Document, marked: Spelling, held: Spelling
) -> list[Piece]:
    # A passage of several blocks that the other revision does not hold as one stretch, since
    # new blocks came to stand between its blocks, as a heading's link to edit it does: found
    # block by block, each part as it is, in order, with nothing but new blocks of few letters
    # between them, and with what stood right before and right after the passage, and around
    # it, standing so again; and only at one place. A table's rows are not looked for so.
    ranges = passage.anchored
    breaks_at_seams = (
        len(ranges) > 1
        and passage.parts == len(ranges) - 1
        and all(
            earlier.end == later.start and passage.breaks[later.start]
            for earlier, later in pairwise(ranges)
        )
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
            and not edges_within(held.cell_edges, whole.start - 1, whole.end + 1)
            and is_placed(passage, marked, held, whole)
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
    marked = spellings.spelling(marked_in)
    letters = marked_letters(passage, marked_in, marked)
    chosen = found_places(letters, document, marked, held) if letters.letters else []
    if chosen:
        anchors, exact = placed_anchors(letters, document, held, chosen[0])
    else:
        apart = parts_apart(letters, document, marked, held) if letters.letters else []
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
