import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter, OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from functools import cache, lru_cache
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
RIVAL_MARGIN = 2
QUICK_SIDE = 4
# A table's row tells a cell's place when it holds this many letters beside the cell.
ROW_LETTERS = 4
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
# How many places of a passage held many times are weighed against all its copies at most: a
# place not turned down by the copies that stood among its own letters is one of them.
TOLD_PLACES = 2 * WIDE_RIVALS
# How many blocks' letters are kept, by their text, and how many searches for the copies of
# a passage's letters.
LAYOUTS_KEPT = 8192
OCCURRENCES_KEPT = 64
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

# What stands for a document's beginning or end beside a copy of a passage, which no letter is.
EDGE = "\x00"


class Spelling(BaseModel):
    # A document's letters, block after block, the letter each block begins at, the last
    # number being the count of letters, and the letters at which a table's cell or row begins,
    # a table's block counting as a row; and the numbers of the blocks that are tables.
    letters: str
    starts: list[int]
    cell_edges: list[int]
    row_edges: list[int]
    tables: list[int]


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
    rows: int
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
    tables: list[int] = []
    for number, block in enumerate(document.blocks):
        if not isinstance(block, Table):
            continue
        tables.append(number)
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
        tables=tables,
    )


def block_letters(block: Block) -> BlockLetters:
    return text_letters(block.text)


@lru_cache(maxsize=LAYOUTS_KEPT)
def text_letters(text: str) -> BlockLetters:
    # Kept for the texts read last, since the search asks for a block's again and again.
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


@lru_cache(maxsize=OCCURRENCES_KEPT)
def occurrences(letters: str, needle: str, most: int) -> tuple[int, ...]:
    # Where the letters hold the needle, up to the most that are wanted; kept for the last
    # asked, since the rules ask for a passage's copies once a place.
    found: list[int] = []
    at = letters.find(needle)
    while at >= 0 and len(found) < most:
        found.append(at)
        at = letters.find(needle, at + 1)
    return tuple(found)


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
        rows=len(edges_within(spelling.row_edges, begin, finish)),
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
        # What parts a table's cells is a letter, but white space at an anchor's start. An
        # anchor's end is a letter's character, never white space, which has no letter. A
        # mark goes with the character it is set on; a mark that stands on nothing, after
        # white space or at a block's beginning, goes with neither.
        while start < end and (
            text[start].isspace()
            or (is_mark(text[start]) and (start == 0 or text[start - 1].isspace()))
        ):
            start += 1
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


def digits_of(letters: str) -> list[str]:
    return [letter for letter in letters if letter.isdigit()]


def is_side_found(marked: str, held: str) -> bool:
    # Whether the letters next to a place on one side are those next to the passage, read away
    # from it, with one letter changed at most, but no digit: a changed number next to the
    # place, as the next entry of a numbered list has, makes it another place, while a comma
    # put in before a date does not. Where the passage has no letters on a side, nothing is
    # asked of it; where it has some and the place has none, the side is not found.
    return not marked or any(
        distance <= NEIGHBOUR_CHANGES and digits_of(held[:length]) == digits_of(marked)
        for length, distance in enumerate(prefix_distances(marked, held))
        if length
    )


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
    # heading. A table, however short, is no such block: its rows are the passage's
    # surroundings or another table's.
    low, high = max(0, low), min(len(held.letters), high)
    first = bisect_right(held.starts, low) - 1
    kept: list[str] = []
    for number, (begin, finish) in enumerate(pairwise(held.starts[first:]), start=first):
        if begin >= high:
            break
        letters = held.letters[begin:finish]
        if finish - begin > INSERTED_LETTERS or letters in kept_letters or number in held.tables:
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


def is_told_by_row(passage: MarkedLetters, marked: Spelling) -> bool:
    # Whether the passage is a table's cell, or cells of one row, whose row holds ROW_LETTERS
    # beside it at least.
    if passage.rows or bisect_right(marked.starts, passage.begin) - 1 not in marked.tables:
        return False
    in_row = side_before(marked, passage.begin, NEIGHBOURS, marked.row_edges) + side_after(
        marked, passage.finish, NEIGHBOURS, marked.row_edges
    )
    return len(in_row) >= ROW_LETTERS and in_row != side_before(
        marked, passage.begin, NEIGHBOURS, []
    ) + side_after(marked, passage.finish, NEIGHBOURS, [])


def has_neighbours(
    passage: MarkedLetters, marked: Spelling, held: Spelling, stretch: Stretch
) -> bool:
    # Whether the letters that stood right before and right after the passage stand right
    # before and right after the place. In a table, the letters that count are first those of
    # the passage's own row, where its row holds ROW_LETTERS beside it at least, and then
    # those right before or right after its row, outside it, so that a row put in above or
    # below a cell leaves it its place and a row moved elsewhere, or said again, does not
    # take it along; a cell of a shorter row is told by the rows around it.
    across: tuple[list[int], list[int]] = ([], [])
    if passage.begin == 0 and passage.finish == len(marked.letters):
        # A passage that was its whole revision has no letters around it: the place must
        # keep one of its document's edges at least.
        return stretch.start == 0 or stretch.end == len(held.letters)
    if not is_told_by_row(passage, marked):
        # A side is borne out by its letters, or by its block's edge; one side at least by
        # NEIGHBOURS letters. A side the passage lacks, at its revision's edge, asks nothing,
        # and bears nothing out, and a side cut short by that edge bears out only itself,
        # unless the place stands as near the other revision's edge, among the same letters.
        by_letters = sides_found(passage, marked, held, stretch, across)
        by_edge = edge_sides(passage, marked, held, stretch)
        has = (passage.begin > 0, passage.finish < len(marked.letters))
        full = (
            passage.begin >= NEIGHBOURS
            or held.letters[: stretch.start] == marked.letters[: passage.begin],
            len(marked.letters) - passage.finish >= NEIGHBOURS
            or held.letters[stretch.end :] == marked.letters[passage.finish :],
        )
        return all(
            letters or edge or not side
            for letters, edge, side in zip(by_letters, by_edge, has, strict=True)
        ) and any(
            letters and side and whole
            for letters, side, whole in zip(by_letters, has, full, strict=True)
        )
    return all(
        sides_found(passage, marked, held, stretch, (marked.row_edges, held.row_edges))
    ) and is_row_placed(passage, marked, held, stretch)


def edge_sides(
    passage: MarkedLetters, marked: Spelling, held: Spelling, stretch: Stretch
) -> tuple[bool, bool]:
    # On each side, whether the passage's block's edge bears the place out: the passage began
    # (or ended) fewer than NEIGHBOURS letters from its block's start (or end), and the place
    # lies as far from its own block's start (or end), the letters between standing as they
    # were. A paragraph left word for word as it was keeps a passage marked at its start
    # though the paragraph before it was rewritten.
    def block_of(spelling: Spelling, at: int) -> tuple[int, int]:
        number = min(bisect_right(spelling.starts, at), len(spelling.starts) - 1) - 1
        return spelling.starts[number], spelling.starts[number + 1]

    marked_start, _ = block_of(marked, passage.begin)
    _, marked_end = block_of(marked, passage.finish - 1)
    held_start, _ = block_of(held, stretch.start)
    _, held_end = block_of(held, stretch.end - 1)
    before = marked.letters[marked_start : passage.begin]
    after = marked.letters[passage.finish : marked_end]
    return (
        len(before) < NEIGHBOURS and held.letters[held_start : stretch.start] == before,
        len(after) < NEIGHBOURS and held.letters[stretch.end : held_end] == after,
    )


def keeps_its_ends(passage: MarkedLetters, letters: str) -> bool:
    # Whether letters a little unlike a passage's begin and end as it did: its first and its
    # last NEIGHBOURS letters, one read otherwise at most and no digit changed, as its
    # neighbours are asked; where they differ, its words at that end are another's. In a
    # script written without spaces, where one letter is a word, none may be read otherwise.
    reach = NEIGHBOURS + NEIGHBOUR_CHANGES
    ends = [
        (passage.letters[:NEIGHBOURS], letters[:reach]),
        (passage.letters[-NEIGHBOURS:][::-1], letters[-reach:][::-1]),
    ]
    return all(
        held.startswith(marked)
        if any(is_unspaced(letter) for letter in marked)
        else is_side_found(marked, held)
        for marked, held in ends
    )


def has_a_side(passage: MarkedLetters, marked: Spelling, held: Spelling, stretch: Stretch) -> bool:
    # Whether one side of the passage at least is borne out at a place: by its letters, or by
    # its document's edge, which the place keeps too. A block's edge alone bears out nothing
    # here: a new paragraph of the same form has one as the passage's own has.
    by_letters = sides_found(passage, marked, held, stretch, ([], []))
    at_edges = (
        passage.begin == 0 and stretch.start == 0,
        passage.finish == len(marked.letters) and stretch.end == len(held.letters),
    )
    has = (passage.begin > 0, passage.finish < len(marked.letters))
    return any(
        (side and letters) or kept
        for side, letters, kept in zip(has, by_letters, at_edges, strict=True)
    )


def own_place(passage: MarkedLetters, marked: Spelling, held: Spelling) -> tuple[int, int] | None:
    # Where the passage's own blocks still stand in the other revision, as letters: between
    # the blocks right before and right after them, where both stand there, their very
    # letters, each said once in both revisions, with letters between them (a document's
    # edge stands for a neighbour the passage's blocks do not have). None where they do not,
    # or stand next to each other: the passage's own blocks are gone from there.
    def blocks_of(spelling: Spelling) -> list[str]:
        return [spelling.letters[begin:end] for begin, end in pairwise(spelling.starts)]

    marked_blocks = blocks_of(marked)
    first = bisect_right(marked.starts, passage.begin) - 1
    last = bisect_right(marked.starts, passage.finish - 1) - 1
    before = next((n for n in range(first - 1, -1, -1) if marked_blocks[n]), None)
    after = next((n for n in range(last + 1, len(marked_blocks)) if marked_blocks[n]), None)
    # Each neighbour's letters, said once in each revision, stand where they stand in the
    # other, as a block of their own or as part of one that took other words in; where the
    # passage's blocks begin or end the document, its edge stands for that neighbour.
    if before is None and after is None:
        return None

    def placed(number: int | None) -> int | None:
        if number is None:
            return None
        letters = marked_blocks[number]
        if len(occurrences(marked.letters, letters, 2)) != 1:
            return None
        found = occurrences(held.letters, letters, 2)
        return found[0] if len(found) == 1 else None

    at_before, at_after = placed(before), placed(after)
    if (before is not None and at_before is None) or (after is not None and at_after is None):
        return None
    start = 0 if before is None or at_before is None else at_before + len(marked_blocks[before])
    end = len(held.letters) if at_after is None else at_after
    if end <= start:
        return None
    return start, end


def row_bounds(spelling: Spelling, start: int, end: int) -> tuple[int, int]:
    # Where the table's row that holds a stretch begins and ends.
    before = bisect_right(spelling.row_edges, start) - 1
    after = bisect_left(spelling.row_edges, end)
    return (
        spelling.row_edges[before] if before >= 0 else 0,
        spelling.row_edges[after] if after < len(spelling.row_edges) else len(spelling.letters),
    )


def is_row_placed(
    passage: MarkedLetters, marked: Spelling, held: Spelling, stretch: Stretch
) -> bool:
    # Whether the letters right before the passage's row, or those right after it, stand
    # right before or right after the place's row, on a side the passage's row has: at a
    # document's edge, the edge confirms nothing.
    reach = NEIGHBOURS + NEIGHBOUR_CHANGES
    marked_start, marked_end = row_bounds(marked, passage.begin, passage.finish)
    held_start, held_end = row_bounds(held, stretch.start, stretch.end)
    return (
        marked_start > 0
        and is_side_found(
            marked.letters[max(0, marked_start - NEIGHBOURS) : marked_start][::-1],
            held.letters[max(0, held_start - reach) : held_start][::-1],
        )
    ) or (
        marked_end < len(marked.letters)
        and is_side_found(
            marked.letters[marked_end : marked_end + NEIGHBOURS],
            held.letters[held_end : held_end + reach],
        )
    )


def changes_between(one: str, other: str) -> int:
    # The changes that make one string the other; none between the same letters, as text
    # said again and again has them.
    return 0 if one == other else prefix_distances(one, other)[-1]


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
        changes_between(one, other)
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
    # stands there nearly as it was (no more than a quarter of RIVAL_SIDE changed); nor, over
    # the wide surroundings, liker on a side by WIDE_MARGIN, as where copies stand within a
    # sentence said again and the rest of what stood around another copy stands beside the
    # place. A place that could as well be another copy's is doubtful. (is_placed asks
    # is_told_from_copies first, which turns another copy's place down soonest.)
    size = passage.finish - passage.begin
    own = side_changes(passage, marked, (passage.begin, passage.finish), held, place)
    held_before = held.letters[max(0, place.start - QUICK_SIDE) : place.start]
    held_after = held.letters[place.end : place.end + QUICK_SIDE]
    held_wide = wide_sides(held.letters, place.start, place.end)
    own_wide = [
        side_likeness(side, held_side)
        for side, held_side in zip(
            wide_sides(marked.letters, passage.begin, passage.finish), held_wide, strict=True
        )
    ]
    return all(
        not any(
            other_side + RIVAL_MARGIN <= own_side and other_side <= RIVAL_SIDE // 4
            for other_side, own_side in zip(other, own, strict=True)
        )
        and sum(other) >= sum(own) + RIVAL_MARGIN
        and not any(
            # A likeness is one at most: where the marked copy's is within WIDE_MARGIN of
            # one, no other copy's can pass it by as much.
            own_side + WIDE_MARGIN <= 1
            and bool(side)
            and side_likeness(side, held_side) >= own_side + WIDE_MARGIN
            for side, held_side, own_side in zip(
                wide_sides(marked.letters, copy, copy + size), held_wide, own_wide, strict=True
            )
        )
        for copy in copies_for(passage, marked, held, place)
        if copy != passage.begin
        and (
            marked.letters[max(0, copy - QUICK_SIDE) : copy] == held_before
            or marked.letters[copy + size : copy + size + QUICK_SIDE] == held_after
        )
        and is_row_rival(passage, marked, held, place, copy)
        for other in [side_changes(passage, marked, (copy, copy + size), held, place)]
    )


def edged_sides(letters: str, start: int, end: int, reach: int = RIVAL_SIDE) -> tuple[str, str]:
    # The letters on each side of a stretch, as many as reach, read away from it, each ending
    # in EDGE where the document ends before as many: a document's beginning or end beside a
    # copy is what stood around it as much as letters are.
    before = letters[max(0, start - reach) : start][::-1]
    after = letters[end : end + reach]
    return (
        before + EDGE if start < reach else before,
        after + EDGE if end + reach > len(letters) else after,
    )


def edged_changes(
    marked: Spelling,
    start: int,
    end: int,
    held: Spelling,
    place: Stretch,
    reach: int = RIVAL_SIDE,
) -> tuple[int, int]:
    # The changes that make the letters on each side of a stretch of the passage's revision,
    # a document's edge counted, those on each side of a place.
    sides = edged_sides(marked.letters, start, end, reach)
    held_sides = edged_sides(held.letters, place.start, place.end, reach)
    return (
        changes_between(sides[0], held_sides[0]),
        changes_between(sides[1], held_sides[1]),
    )


def positional_changes(
    marked: Spelling,
    start: int,
    end: int,
    held: Spelling,
    place: Stretch,
    reach: int = RIVAL_SIDE,
) -> tuple[int, int]:
    # On each side, the letters, a document's edge counted, that do not stand at the same
    # distance from a stretch of the passage's revision as from a place: a copy whose
    # surroundings are the place's shifted by a letter, as in a word said again and again,
    # is not told by them.
    sides = edged_sides(marked.letters, start, end, reach)
    held_sides = edged_sides(held.letters, place.start, place.end, reach)
    return (
        max(len(sides[0]), len(held_sides[0]))
        - sum(one == other for one, other in zip(sides[0], held_sides[0], strict=False)),
        max(len(sides[1]), len(held_sides[1]))
        - sum(one == other for one, other in zip(sides[1], held_sides[1], strict=False)),
    )


@lru_cache(maxsize=OCCURRENCES_KEPT)
def copies_by_sides(
    letters: str, needle: str
) -> tuple[tuple[int, ...], dict[str, list[int]], dict[str, list[int]]]:
    # The copies of the needle in the letters, and the copies by the RIVAL_SIDE letters
    # before them and those after them, a document's edge counted.
    copies = occurrences(letters, needle, OCCURRENCES_WEIGHED)
    befores: dict[str, list[int]] = {}
    afters: dict[str, list[int]] = {}
    for copy in copies:
        before, after = edged_sides(letters, copy, copy + len(needle))
        befores.setdefault(before, []).append(copy)
        afters.setdefault(after, []).append(copy)
    return copies, befores, afters


def outward(copies: Sequence[int], at: int) -> Iterator[int]:
    # The copies, in their order, from the one nearest a letter outwards.
    above = bisect_left(copies, at)
    below = above - 1
    while below >= 0 or above < len(copies):
        if above < len(copies):
            yield copies[above]
            above += 1
        if below >= 0:
            yield copies[below]
            below -= 1


def copies_among(
    passage: MarkedLetters, marked: Spelling, held: Spelling, place: Stretch
) -> Iterator[int]:
    # The copies of the passage in its revision that stood among the place's very letters on
    # a side, from where the place lies in its own revision outwards: those that turn down a
    # place that is another copy's.
    _, befores, afters = copies_by_sides(marked.letters, passage.letters)
    before, after = edged_sides(held.letters, place.start, place.end)
    at = place.start * len(marked.letters) // max(1, len(held.letters))
    seen: set[int] = set()
    for copies in (befores.get(before, []), afters.get(after, [])):
        for copy in outward(copies, at):
            if copy not in seen:
                seen.add(copy)
                yield copy


def copies_for(
    passage: MarkedLetters, marked: Spelling, held: Spelling, place: Stretch
) -> Iterator[int]:
    # The copies of the passage in its revision, those that stood among the place's letters on
    # a side first, then the others from where the place lies in its own revision outwards:
    # the order the rules that weigh every copy ask in, to end soonest.
    copies, _, _ = copies_by_sides(marked.letters, passage.letters)
    first: set[int] = set()
    for copy in copies_among(passage, marked, held, place):
        first.add(copy)
        yield copy
    at = place.start * len(marked.letters) // max(1, len(held.letters))
    yield from (copy for copy in outward(copies, at) if copy not in first)


def is_told_from_copies(
    passage: MarkedLetters,
    marked: Spelling,
    held: Spelling,
    place: Stretch,
    copies: Iterable[int] | None = None,
) -> bool:
    # Whether a place is the marked copy's and not another copy's in the passage's revision,
    # by what stood around each: from each other copy, on a side where the place stands
    # nearly as the marked copy did (no more than a quarter of the side's letters changed),
    # the other copy's side needs twice the marked copy's changes and RIVAL_MARGIN more; and
    # on no side does another copy's stand liker (is_liker). Where a copy stood among the
    # very letters the marked copy stood among, as a sentence said at the end of every
    # section does, it is weighed again over twice as many letters, up to WIDE_SURROUNDINGS,
    # and the wider letters may tell against the place too. A cell told by its row is weighed
    # only against copies in rows like its own. A place that cannot be told is doubtful: a
    # passage shown at another copy's place is worse than one lost.
    # Another copy's changes are worked out only where they decide: it can stand liker
    # (is_liker) only on a side the marked copy's changes RIVAL_MARGIN letters at least, and
    # it is told without them where its letters, counted letter for letter, already tell it.
    size = passage.finish - passage.begin
    own_at: dict[int, tuple[tuple[str, str], tuple[str, str], tuple[int, int]]] = {}
    # The changes from a side's letters to the place's, by the side's letters: copies that
    # stood among the same letters, as in text said again and again, are worked out once.
    worked: dict[tuple[int, int, str], int] = {}

    def changes(reach: int, held_sides: tuple[str, str], sides: tuple[str, str]) -> tuple[int, int]:
        found = []
        for number in (0, 1):
            key = (reach, number, sides[number])
            if key not in worked:
                worked[key] = changes_between(sides[number], held_sides[number])
            found.append(worked[key])
        return found[0], found[1]

    unlikes: dict[tuple[int, tuple[str, str]], tuple[int, int]] = {}

    def unlike_at(
        reach: int, held_sides: tuple[str, str], sides: tuple[str, str]
    ) -> tuple[int, int]:
        if (reach, sides) not in unlikes:
            unlikes[reach, sides] = unlike_letters(sides, held_sides)
        return unlikes[reach, sides]

    def own_sides_at(reach: int) -> tuple[tuple[str, str], tuple[str, str], tuple[int, int]]:
        if reach not in own_at:
            sides = edged_sides(marked.letters, passage.begin, passage.finish, reach)
            held_sides = edged_sides(held.letters, place.start, place.end, reach)
            own_at[reach] = (sides, held_sides, changes(reach, held_sides, sides))
        return own_at[reach]

    # A copy that stood among the very letters, up to WIDE_SURROUNDINGS, of one already told
    # is told as that one was.
    passed: set[tuple[str, str]] = set()
    for copy in copies_for(passage, marked, held, place) if copies is None else copies:
        if copy == passage.begin or not is_row_rival(passage, marked, held, place, copy):
            continue
        widest = edged_sides(marked.letters, copy, copy + size, WIDE_SURROUNDINGS)
        if widest in passed:
            continue
        passed.add(widest)
        told = False
        reach = RIVAL_SIDE
        while reach <= WIDE_SURROUNDINGS:
            own_sides, held_sides, own = own_sides_at(reach)
            copy_sides = edged_sides(marked.letters, copy, copy + size, reach)
            unlike = unlike_at(reach, held_sides, copy_sides)
            other: tuple[int, int] | None = None
            if any(own_side >= RIVAL_MARGIN for own_side in own):
                other = changes(reach, held_sides, copy_sides)
                if any(
                    is_liker(other_side, own_side, reach)
                    for own_side, other_side in zip(own, other, strict=True)
                ):
                    return False
            bounds = [len(side) // 4 for side in own_sides]
            if not told and not any(
                own_side <= bound and 2 * own_side + RIVAL_MARGIN <= unlike_side
                for own_side, bound, unlike_side in zip(own, bounds, unlike, strict=True)
            ):
                if other is None:
                    other = changes(reach, held_sides, copy_sides)
                told = is_told(own, bounds, other, unlike)
            else:
                told = True
            if own_sides[0] != copy_sides[0] and own_sides[1] != copy_sides[1]:
                break
            reach *= 2
        if not told:
            return False
    return True


def unlike_letters(sides: tuple[str, str], held_sides: tuple[str, str]) -> tuple[int, int]:
    # On each side, the letters that do not stand at the same distance from the stretch, as
    # positional_changes counts them.
    return (
        max(len(sides[0]), len(held_sides[0]))
        - sum(one == other for one, other in zip(sides[0], held_sides[0], strict=False)),
        max(len(sides[1]), len(held_sides[1]))
        - sum(one == other for one, other in zip(sides[1], held_sides[1], strict=False)),
    )


def is_row_rival(
    passage: MarkedLetters, marked: Spelling, held: Spelling, place: Stretch, copy: int
) -> bool:
    # Whether another copy of a table's cell could be the one at a place by its own row: a
    # copy whose row's letters beside it do not stand beside the place on each side, as a
    # cell of another row numbered otherwise, is no rival of a cell told by its row.
    if not is_told_by_row(passage, marked):
        return True
    size = passage.finish - passage.begin
    other = passage.model_copy(
        update={
            "begin": copy,
            "finish": copy + size,
            "before": marked.letters[max(0, copy - WIDE_SURROUNDINGS) : copy],
            "after": marked.letters[copy + size : copy + size + WIDE_SURROUNDINGS],
        }
    )
    return all(sides_found(other, marked, held, place, (marked.row_edges, held.row_edges)))


def positional_likeness(marked: str, held: str) -> float:
    # The share of a side's letters, read away from the passage, that stand at the same
    # distance from a place.
    return sum(one == other for one, other in zip(marked, held, strict=False)) / max(1, len(marked))


def doubting(
    passage: MarkedLetters, marked: Spelling, held: Spelling, rivals: list[Stretch]
) -> set[int]:
    # Where the copies of the passage's very letters in the other revision stand (of rivals:
    # those that have the passage's neighbours too) whose surroundings stand as those of the
    # passage stood, letter for letter, more than TOLD_APART of them over both sides of
    # RIVAL_SIDE letters, and that are not the place of another copy in the passage's
    # revision: such a copy leaves any other place in doubt (is_told_apart).
    # Two are enough to leave every place in doubt, so the weighing ends at the second.
    marked_sides = (passage.before[-RIVAL_SIDE:][::-1], passage.after[:RIVAL_SIDE])
    doubts: set[int] = set()
    for copy in rivals:
        if positional_likeness(
            marked_sides[0], held.letters[max(0, copy.start - RIVAL_SIDE) : copy.start][::-1]
        ) + positional_likeness(
            marked_sides[1], held.letters[copy.end : copy.end + RIVAL_SIDE]
        ) > TOLD_APART and not is_another_copy_s(passage, marked, held, copy):
            doubts.add(copy.start)
            if len(doubts) > 1:
                break
    return doubts


def is_told_apart(place: Stretch, doubts: set[int]) -> bool:
    # Whether no copy but the place itself leaves it in doubt (doubting).
    return doubts <= {place.start}


def is_another_copy_s(
    passage: MarkedLetters, marked: Spelling, held: Spelling, place: Stretch
) -> bool:
    # Whether what stands around a place is liker what stood around another copy of the
    # passage in its revision than what stood around the passage, by RIVAL_MARGIN changes.
    size = passage.finish - passage.begin
    own = sum(side_changes(passage, marked, (passage.begin, passage.finish), held, place))
    if own < RIVAL_MARGIN:
        return False
    return any(
        sum(side_changes(passage, marked, (copy, copy + size), held, place)) + RIVAL_MARGIN <= own
        for copy in copies_for(passage, marked, held, place)
        if copy != passage.begin
    )


def crosses_no_more_cells(passage: MarkedLetters, held: Spelling, place: Stretch) -> bool:
    # Whether a place runs across no more of a table's cells, and no more of its rows' ends,
    # than the passage was marked across, the ends of the blocks it ran through counted as
    # cells, and those of the place not counted as rows' ends: a table read again in two
    # blocks parts no row.
    return len(edges_within(held.cell_edges, place.start, place.end)) <= passage.parts and (
        sum(
            not is_block_start(held, edge)
            for edge in edges_within(held.row_edges, place.start, place.end)
        )
        <= passage.rows
    )


def is_placed(passage: MarkedLetters, marked: Spelling, held: Spelling, place: Stretch) -> bool:
    # Whether a place is the passage's by what stands around it: right next to it, the letters
    # that stood next to the passage; further off, letters as like those that stood around it
    # as WIDE_FOUND asks, so that the same words beside the same words in another entry of a
    # list of references are not taken for it; of the RIVAL_SIDE letters on each side, no
    # more than RIVAL_SIDE changed in all, half of them; and none of its other copies told as
    # well.
    # The tests are asked cheapest and likeliest to turn a place down first.
    return (
        has_neighbours(passage, marked, held, place)
        and (passage.copies <= 1 or is_told_from_copies(passage, marked, held, place))
        and sum(side_changes(passage, marked, (passage.begin, passage.finish), held, place))
        <= RIVAL_SIDE
        and wide_likeness(passage, held.letters, place) >= WIDE_FOUND
        and (passage.copies <= 1 or is_copy_s_place(passage, marked, held, place))
    )


def wide_sides(letters: str, start: int, end: int) -> tuple[str, str]:
    # The WIDE_SURROUNDINGS letters on each side of a stretch, read away from it.
    return (
        letters[max(0, start - WIDE_SURROUNDINGS) : start][::-1],
        letters[end : end + WIDE_SURROUNDINGS],
    )


def side_likeness(marked: str, held: str) -> float:
    # One less the changes that make one side the other, as a share of the side's letters. A
    # side the passage lacks, at its revision's edge, tells nothing, and counts as alike.
    return 1 - min(prefix_distances(marked, held)) / len(marked) if marked else 1.0


def wide_likeness(passage: MarkedLetters, letters: str, stretch: Stretch) -> float:
    # How like the wide surroundings of a passage are those of a place, side by side.
    held = wide_sides(letters, stretch.start, stretch.end)
    return side_likeness(passage.before[::-1], held[0]) + side_likeness(passage.after, held[1])


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


def standing_sides(
    passage: MarkedLetters, marked: Spelling, held: Spelling, place: Stretch, reach: int
) -> list[bool]:
    # On each side, whether what stood around the passage, as many as reach letters, its
    # revision's beginning or end counted, stands around a place, a quarter of the side's
    # letters changed at most.
    sides = edged_sides(marked.letters, passage.begin, passage.finish, reach)
    return [
        changes <= len(side) // 4
        for changes, side in zip(
            edged_changes(marked, passage.begin, passage.finish, held, place, reach),
            sides,
            strict=True,
        )
    ]


def is_doubtless(
    passage: MarkedLetters,
    marked: Spelling,
    held: Spelling,
    place: Stretch,
    said_again: int,
    said_alike: int,
) -> bool:
    # Whether a place of a passage that may be another copy's (its revision held it more than
    # once, or the other says it more than once) stands among what stood around it further
    # off than its neighbours: on one side at least, over twice RIVAL_SIDE letters; and on
    # both, where the other revision says it more often than its own did (a cell, in rows
    # like its own), since a copy said again beside the same letters on one side is not told
    # from it by that side alone.
    if passage.copies <= 1 and said_again <= 1:
        return True
    wide = standing_sides(passage, marked, held, place, 2 * RIVAL_SIDE)
    return all(wide) if said_alike > passage.copies else any(wide)


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
    changes = [
        edged_changes(marked, passage.begin, passage.finish, held, place) for place in distinct
    ]
    best = min(range(len(distinct)), key=lambda number: sum(changes[number]))
    sides = edged_sides(marked.letters, passage.begin, passage.finish)
    told = all(
        is_told(
            changes[best],
            [len(side) // 4 for side in sides],
            changes[number],
            positional_changes(marked, passage.begin, passage.finish, held, place),
        )
        for number, place in enumerate(distinct)
        if number != best
    )
    return [distinct[best]] if told else []


def is_liker(other_side: int, own_side: int, reach: int) -> bool:
    # Whether another copy's side, of reach letters, stands beside a place liker than the
    # marked copy's: by RIVAL_MARGIN changes or a quarter of the marked copy's, whichever is
    # more, and with three quarters of its letters changed at most, so that a side the place
    # shares with neither does not count.
    return (
        other_side <= 3 * reach // 4 and other_side + max(RIVAL_MARGIN, own_side // 4) <= own_side
    )


def is_told(
    own: tuple[int, int],
    bounds: list[int],
    other: tuple[int, int],
    unlike: tuple[int, int],
) -> bool:
    # Whether one of two places, or of two copies, is told from the other by the letters
    # around the marked passage: on a side where the one stands nearly as the passage did
    # (within its bound), the other needs twice its changes and RIVAL_MARGIN more, its
    # changes counted letter for letter too; and on no side does the other stand liker
    # (is_liker).
    return any(
        own_side <= bound and 2 * own_side + RIVAL_MARGIN <= max(other_side, unlike_side)
        for own_side, bound, other_side, unlike_side in zip(own, bounds, other, unlike, strict=True)
    ) and not any(
        is_liker(other_side, own_side, RIVAL_SIDE)
        for own_side, other_side in zip(own, other, strict=True)
    )


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
    # The places in whole words, and of them those with the passage's neighbours, each
    # worked out once. Where more places have its neighbours than one_place can tell apart,
    # the passage is lost at once, before its copies are weighed place by place.
    shaped = [place for place in exact if is_shaped(place)]
    neighbouring = [place for place in exact if has_neighbours(passage, marked, held, place)]
    # The places with the passage's neighbours, in whole words, are weighed one by one,
    # against the copies of the passage in its revision. Each copy's surroundings are worked
    # out once a passage (copies_by_sides), and a place is weighed first against the copies
    # that stood among its own letters, which turn down a place that is another copy's at
    # once.
    shaped_starts = {place.start for place in shaped}
    rivals = [place for place in neighbouring if place.start in shaped_starts]
    # How many times the other revision says the passage, in whole words, counted as far as
    # the rules below ask; and how many of them, for a cell told by its row, in a row like its
    # own.
    enough = max(passage.copies, 1) + 1
    said_again = min(len(shaped), enough)
    said_alike = min(len(rivals), enough) if is_told_by_row(passage, marked) else said_again
    weighed: list[set[int]] = []

    def doubts() -> set[int]:
        # Worked out once, and only where a place is placed.
        if not weighed:
            weighed.append(doubting(passage, marked, held, neighbouring))
        return weighed[0]

    neighboured: list[Stretch] = []
    weighed_slowly = 0
    for place in rivals:
        if not is_doubtless(passage, marked, held, place, said_again, said_alike):
            continue
        if passage.copies > 1:
            # A place that is another copy's is turned down by the copies that stood among its
            # own letters. One that is not must be weighed against every copy; where more
            # places than TOLD_PLACES must, the passage cannot be told among them in time,
            # and is lost.
            if not is_told_from_copies(
                passage, marked, held, place, copies_among(passage, marked, held, place)
            ):
                continue
            weighed_slowly += 1
            if weighed_slowly > TOLD_PLACES:
                return []
        if is_placed(passage, marked, held, place) and is_told_apart(place, doubts()):
            neighboured.append(place)
            if len(neighboured) > WIDE_RIVALS:
                # More places than one_place can tell apart: lost, as one_place answers.
                return []
    if neighboured and len(exact) >= passage.copies:
        return one_place(passage, marked, held, neighboured)
    if said_again >= passage.copies:
        # The other revision holds the passage's very words, in whole words, as often as its
        # own did, and none of them where the passage stood: its words moved, and words a
        # little unlike them between its old neighbours may be a copy said again. Lost.
        return []
    candidates = [
        place
        for place in (
            to_word_edges(passage, document, held, place) for place in near_places(passage, held)
        )
        if place.end > place.start and is_near(place, passage.letters) and is_shaped(place)
    ]
    # Where the nearest of the places a little unlike the passage differs from it in a
    # number, wherever it stands, the passage was changed in a number there, and a place
    # further from its words is another passage's.
    nearest = min((place.distance for place in candidates), default=0)
    if not any(
        says_the_same(passage, held.letters[place.start : place.end])
        for place in candidates
        if place.distance == nearest
    ):
        return one_place(passage, marked, held, neighboured)
    if (
        is_distinct(passage.letters)
        and passage.copies == 1
        and not exact
        and not parts_apart(passage, document, marked, held)
    ):
        # A distinct passage that its revision held once, and that the other holds a little
        # unlike itself at one place only, saying the same and standing nowhere else in its
        # revision, is found there by its letters, as it is where the other holds its very
        # letters once: what stood around it may have changed with its words, on one side at
        # most, but its own first and last words stand there (keeps_its_ends), and one side is
        # borne out by letters or its document's edge. Not where it stands block by block with
        # new blocks between (parts_apart), found exact there. Where its own place still
        # stands (own_place), only a place there: a place elsewhere is another sentence's.
        region = own_place(passage, marked, held)
        alone = one_place(
            passage,
            marked,
            held,
            [
                place
                for place in candidates
                if says_the_same(passage, held.letters[place.start : place.end])
                and not stood_elsewhere(passage, marked, held, place)
                and has_a_side(passage, marked, held, place)
                and keeps_its_ends(passage, held.letters[place.start : place.end])
                and (region is None or (region[0] <= place.start and place.end <= region[1]))
            ],
        )
        if alone and len(candidates) == 1:
            return alone
    near = [
        place
        for place in candidates
        if says_the_same(passage, held.letters[place.start : place.end])
        and is_placed(passage, marked, held, place)
        and is_told_apart(place, doubts())
        and is_doubtless(passage, marked, held, place, said_again, said_alike)
        and not (place.distance and stood_elsewhere(passage, marked, held, place))
    ]
    return one_place(passage, marked, held, neighboured + near)


def stood_elsewhere(
    passage: MarkedLetters, marked: Spelling, held: Spelling, place: Stretch
) -> bool:
    # Whether the letters of a place a little unlike a passage's stood elsewhere in its
    # revision, the very same or liker than the passage's, as the entry before a removed one
    # that differs from it in an author's initial does, or a reference said again at the end
    # of a book in longer words: they are that passage's, not this one's.
    letters = held.letters[place.start : place.end]

    def is_elsewhere(start: int, end: int) -> bool:
        # Not where the passage begins, though it may overlap it.
        return abs(start - passage.begin) > ONE_PLACE

    # A place across blocks is asked block by block too: a block's part that the passage does
    # not hold, and that stood elsewhere in its revision, the very same, is another
    # sentence's, as a sibling sentence that differs from the passage's by one word is; a
    # short part, as a heading's new link to edit it, tells nothing so.
    parts = [
        held.letters[
            held.starts[piece.block_number] + piece.start : held.starts[piece.block_number]
            + piece.end
        ]
        for piece in pieces_of(held, place.start, place.end)
    ]
    return (
        any(
            is_elsewhere(at, at + len(letters))
            for at in occurrences(marked.letters, letters, OCCURRENCES_WEIGHED)
        )
        or any(
            is_elsewhere(stretch.start, stretch.end) and stretch.distance < place.distance
            for stretch in near_stretches(marked.letters, letters)
        )
        or (
            len(parts) > 1
            and any(
                len(part) > INSERTED_LETTERS
                and part not in passage.letters
                and any(
                    not passage.begin <= at < passage.finish
                    for at in occurrences(marked.letters, part, OCCURRENCES_WEIGHED)
                )
                for part in parts
            )
        )
    )


def to_word_edges(
    passage: MarkedLetters, document: Document, held: Spelling, place: Stretch
) -> Stretch:
    # A place of letters a little unlike a passage that begins or ends inside a word, as one
    # whose first letter was read as two may, widened by WORD_SLACK letters at most to the
    # word's edges, where the passage began or ended at a word's edge.
    def edge_offset(at: int) -> tuple[str, int]:
        # A place may reach the end of the document's letters, which is the last block's end.
        block = min(bisect_right(held.starts, at), len(document.blocks)) - 1
        layout = block_letters(document.blocks[block])
        letter = at - held.starts[block]
        text = document.blocks[block].text
        return text, layout.origins[letter] if letter < len(layout.origins) else len(text)

    start, end = place.start, place.end
    if end <= start:
        # An empty place, as the near search gives where nothing of the passage is left,
        # has no edges to widen.
        return place
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
