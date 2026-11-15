import re
from itertools import pairwise
from pathlib import Path
from tempfile import TemporaryDirectory

from lxml import html
from pydantic import BaseModel
from pypdfium2 import PdfDocument

from infonium.document import (
    CELL_SEPARATOR,
    ROW_SEPARATOR,
    SOFT_HYPHEN,
    WORD_GAP,
    Block,
    Code,
    Extracted,
    Heading,
    ListItem,
    Mark,
    PageFraction,
    PageNumber,
    PagePlace,
    Paragraph,
    Place,
    Style,
    StyleMark,
    Table,
    UnreadableSource,
    joined,
)
from infonium.mineru import (
    Box,
    CodeBody,
    FormulaSpan,
    Group,
    Line,
    Member,
    ParsedPage,
    Plain,
    TableBody,
    Text,
    TextSpan,
    Title,
    lettered_pages,
    picture_pages,
    word_lines,
)

EXTRACTOR = "pdf/1"

TITLE_ENTRY = "Title"
# What a program writes as the title when it is given none: the name of the file it printed.
FILE_SUFFIXES = (".doc", ".docx", ".dvi", ".indd", ".odt", ".pdf", ".ppt", ".pptx", ".ps")
LINE_BREAK = "\n"
HYPHEN = "-"
HYPHENS = f"{HYPHEN}\N{HYPHEN}"
WORD = re.compile(rf"[^\W_]+(?:{HYPHEN}[^\W_]+)*")
BROKEN_END = re.compile(rf"([^\W_]+)[{HYPHENS}]$")
WORD_START = re.compile(r"[^\W_]+")
SCRIPT = re.compile(r"<(sub|sup)>(.*?)</\1>", re.DOTALL)
SCRIPT_STYLES = {"sub": Style.SUBSCRIPT, "sup": Style.SUPERSCRIPT}
BULLETS = (
    "\N{BULLET}\N{WHITE BULLET}\N{TRIANGULAR BULLET}\N{HYPHEN BULLET}\N{BULLET OPERATOR}"
    "\N{MIDDLE DOT}\N{BLACK SMALL SQUARE}\N{WHITE SMALL SQUARE}\N{BLACK SQUARE}\N{WHITE SQUARE}"
    "\N{BLACK CIRCLE}\N{WHITE CIRCLE}\N{BLACK DIAMOND}\N{WHITE DIAMOND}"
)
OPENING_BRACKETS = "(\N{FULLWIDTH LEFT PARENTHESIS}"
# The marks that close a number where the script is written without spaces.
UNSPACED_CLOSERS = "\N{FULLWIDTH RIGHT PARENTHESIS}\N{IDEOGRAPHIC COMMA}\N{FULLWIDTH FULL STOP}"
# The first word of a list item: a bullet or a hyphen, or a number from 1 that a full stop or
# a bracket closes. It stands alone, except before a closing mark that needs no space.
ITEM_MARKER = re.compile(
    rf"(?:[{BULLETS}{HYPHEN}]$"
    rf"|[{OPENING_BRACKETS}]?(?P<number>[1-9]\d{{0,2}})(?:[.)]$|[{UNSPACED_CLOSERS}]))"
)
TABLE_ROWS = ".//tr[not(ancestor::td or ancestor::th)]"
ROW_CELLS = "./td | ./th"
COLUMN_SPAN = "colspan"
ROW_SPAN = "rowspan"
ONE_CELL = "1"


class Piece(BaseModel):
    # Words of one line that are set alike. A word that ends in a soft hyphen goes on in the
    # word after it, with no gap between the two.
    words: list[str]
    styles: list[Style]


class Area(BaseModel):
    page: PageNumber
    left: PageFraction
    top: PageFraction
    width: PageFraction
    height: PageFraction


class ShownLine(BaseModel):
    # One line of a block's words, and the box on its page that shows them.
    pieces: list[Piece]
    area: Area


class Flow(BaseModel):
    text: str
    marks: list[Mark]
    places: list[Place]


class Evidence(BaseModel):
    # What the whole file tells about the reading of any one line of it: the words it shows
    # whole, and the pages it stores turned.
    whole_words: frozenset[str]
    turned_pages: frozenset[PageNumber]


def words_of(text: str) -> list[str]:
    return [word for word in text.split() if word.strip(SOFT_HYPHEN)]


def span_pieces(span: TextSpan | FormulaSpan) -> list[Piece]:
    if isinstance(span, FormulaSpan):
        return [Piece(words=words, styles=[])] if (words := words_of(span.content)) else []
    runs: list[tuple[str, list[Style]]] = []
    read = 0
    for script in SCRIPT.finditer(span.content):
        runs.append((span.content[read : script.start()], []))
        runs.append((script.group(2), [SCRIPT_STYLES[script.group(1)]]))
        read = script.end()
    runs.append((span.content[read:], []))
    pieces: list[Piece] = []
    # A run that touches the next one, such as a word and the mark set above it, goes on in
    # it; after the last run of a span comes a gap.
    for (run, styles), (after, _) in pairwise([*(run for run in runs if run[0]), (WORD_GAP, [])]):
        if words := words_of(run):
            touching = not (run[-1].isspace() or after[0].isspace())
            last = f"{words[-1]}{SOFT_HYPHEN if touching else ''}"
            pieces.append(Piece(words=[*words[:-1], last], styles=styles))
    return pieces


def line_pieces(line: Line) -> list[Piece]:
    return [piece for span in line.spans for piece in span_pieces(span)]


def line_code(line: Line) -> str:
    return WORD_GAP.join(SCRIPT.sub(r"\2", span.content) for span in line.spans)


def fraction(distance: float, size: float) -> float:
    return min(1.0, max(0.0, distance / size))


def area(page: ParsedPage, box: Box) -> Area:
    # The far edges are cut to the page as fractions of it, and the size is taken from them.
    width, height = page.page_size
    left_edge, top_edge, right_edge, bottom_edge = box
    left, top = fraction(left_edge, width), fraction(top_edge, height)
    return Area(
        page=page.page_idx,
        left=left,
        top=top,
        width=fraction(right_edge, width) - left,
        height=fraction(bottom_edge, height) - top,
    )


def placed(where: Area, start: int, end: int) -> PagePlace:
    return PagePlace(
        start=start,
        end=end,
        page=where.page,
        left=where.left,
        top=where.top,
        width=where.width,
        height=where.height,
    )


def shown_lines(page: ParsedPage, lines: list[Line]) -> list[ShownLine]:
    return [
        ShownLine(pieces=pieces, area=area(page, line.bbox))
        for line in lines
        if (pieces := line_pieces(line))
    ]


def whole_words(lines: list[Line]) -> frozenset[str]:
    # The first and the last word of a line are left out: either may be half of a word that
    # a line break has cut.
    whole: set[str] = set()
    for line in lines:
        words = [word for piece in line_pieces(line) for word in piece.words]
        for word in WORD.findall(WORD_GAP.join(words).casefold())[1:-1]:
            whole.update([word, *word.split(HYPHEN)])
    return frozenset(whole)


def breaks_word(head: str, tail: str, known: frozenset[str]) -> bool:
    # A hyphen at a line end either breaks one word or belongs to a compound, and the
    # document itself is asked which: the word without the hyphen found whole elsewhere is
    # a broken word; the word with it, or both halves as words of their own, is a compound.
    # A word found in neither form is taken as broken when it goes on in lower case.
    whole, compound = f"{head}{tail}".casefold(), f"{head}{HYPHEN}{tail}".casefold()
    if whole in known:
        return True
    if compound in known or {head.casefold(), tail.casefold()} <= known:
        return False
    return tail[0].islower()


def line_end(last: str, first: str, known: frozenset[str]) -> str:
    # The last word of a line, marked with a soft hyphen when it goes on in the next line:
    # in place of the hyphen that breaks a word, after the hyphen of a compound.
    head, tail = BROKEN_END.search(last), WORD_START.match(first)
    if not head or not tail:
        return last
    if breaks_word(head.group(1), tail.group(), known):
        return f"{last[:-1]}{SOFT_HYPHEN}"
    return f"{last}{SOFT_HYPHEN}"


def flow(lines: list[ShownLine], evidence: Evidence) -> Flow:
    # Every word is a part of its own, so that the words of one line are joined by the same
    # rule as two lines are.
    spoken = [[word for piece in line.pieces for word in piece.words] for line in lines]
    known = evidence.whole_words
    ends = [line_end(words[-1], after[0], known) for words, after in pairwise(spoken)]
    said = joined(
        [
            word
            for words, end in zip(spoken, [*ends, spoken[-1][-1]], strict=True)
            for word in [*words[:-1], end]
        ]
    )
    marks: list[Mark] = []
    places: list[Place] = []
    read = 0
    for line in lines:
        first = read
        # The engine misjudges what is set above or below the line on a page that is stored
        # turned, so there its word for it is not taken.
        believed = line.area.page not in evidence.turned_pages
        for piece in line.pieces:
            start, end = said.ranges[read].start, said.ranges[read + len(piece.words) - 1].end
            marks.extend(
                StyleMark(style=style, start=start, end=end) for style in piece.styles if believed
            )
            read += len(piece.words)
        places.append(placed(line.area, said.ranges[first].start, said.ranges[read - 1].end))
    return Flow(text=said.text, marks=marks, places=places)


def paragraphs(lines: list[ShownLine], evidence: Evidence) -> list[Block]:
    if not lines:
        return []
    words = flow(lines, evidence)
    return [Paragraph(text=words.text, marks=words.marks, places=words.places)]


def headings(title: Title, page: ParsedPage, evidence: Evidence) -> list[Block]:
    lines = shown_lines(page, title.lines)
    if not lines:
        return []
    words = flow(lines, evidence)
    return [Heading(level=title.level, text=words.text, marks=words.marks, places=words.places)]


def bullet_position(earlier: list[Block]) -> int:
    match earlier[-1:]:
        case [ListItem(ordered=False, position=position)]:
            return position + 1
        case _:
            return 1


def passages(lines: list[ShownLine], evidence: Evidence, earlier: list[Block]) -> list[Block]:
    # The engine does not tell lists apart: it finds each item as a text of its own. A text
    # that starts with a bullet or a number is taken as an item, and loses that marker, which
    # a reader draws again from what the item says of itself.
    if not lines:
        return []
    first = lines[0].pieces[0]
    marker = ITEM_MARKER.match(first.words[0])
    if not marker:
        return paragraphs(lines, evidence)
    rest = [*words_of(first.words[0][marker.end() :]), *first.words[1:]]
    pieces = [*([Piece(words=rest, styles=first.styles)] if rest else []), *lines[0].pieces[1:]]
    item = [*([ShownLine(pieces=pieces, area=lines[0].area)] if pieces else []), *lines[1:]]
    if not item:
        return []
    words = flow(item, evidence)
    number = marker.group("number")
    return [
        ListItem(
            ordered=bool(number),
            depth=0,
            position=int(number) if number else bullet_position(earlier),
            continues=False,
            text=words.text,
            marks=words.marks,
            places=words.places,
        )
    ]


def table_text(markup: str) -> str:
    # A cell that spans rows or columns leaves empty cells where it reaches, so that every
    # cell stays under its own column.
    grid: dict[tuple[int, int], str] = {}
    for row_number, row in enumerate(html.fromstring(markup).xpath(TABLE_ROWS)):
        column = 0
        for cell in row.xpath(ROW_CELLS):
            while (row_number, column) in grid:
                column += 1
            reach = range(column, column + int(cell.get(COLUMN_SPAN, ONE_CELL)))
            for below in range(row_number, row_number + int(cell.get(ROW_SPAN, ONE_CELL))):
                grid.update({(below, across): "" for across in reach})
            grid[row_number, column] = WORD_GAP.join(cell.text_content().split())
            column = reach.stop
    columns = range(1 + max((across for _, across in grid), default=-1))
    table = [
        [grid.get((row_number, column), "") for column in columns]
        for row_number in range(1 + max((below for below, _ in grid), default=-1))
    ]
    return ROW_SEPARATOR.join(CELL_SEPARATOR.join(cells) for cells in table if any(cells))


def tables(body: TableBody, page: ParsedPage) -> list[Block]:
    text = ROW_SEPARATOR.join(
        table
        for line in body.lines
        for span in line.spans
        if span.html and (table := table_text(span.html))
    )
    return [Table(text=text, places=[placed(area(page, body.bbox), 0, len(text))])] if text else []


def listings(body: CodeBody, page: ParsedPage) -> list[Block]:
    lines = [(line, code) for line in body.lines if (code := line_code(line)).strip()]
    places: list[Place] = []
    read = 0
    for line, code in lines:
        places.append(placed(area(page, line.bbox), read, read + len(code)))
        read += len(code) + len(LINE_BREAK)
    return [Code(text=LINE_BREAK.join(code for _, code in lines), places=places)] if lines else []


def member_blocks(member: Member, page: ParsedPage, evidence: Evidence) -> list[Block]:
    match member:
        case Plain():
            return paragraphs(shown_lines(page, member.lines), evidence)
        case TableBody():
            return tables(member, page)
        case CodeBody():
            return listings(member, page)
        case _:
            return []


def text_chains(pages: list[ParsedPage]) -> dict[tuple[int, int], list[ShownLine]]:
    # Every text that starts a paragraph, with its own lines and those of the texts that go
    # on from it, each line on the page where it was found.
    chains: dict[tuple[int, int], list[ShownLine]] = {}
    starts: list[tuple[int, int]] = []
    for page in pages:
        continued = page.continued()
        for region in page.preproc_blocks:
            if isinstance(region, Text):
                if region.index not in continued:
                    starts.append((page.page_idx, region.index))
                chains.setdefault(starts[-1], []).extend(shown_lines(page, region.lines))
    return chains


def pdf_blocks(pages: list[ParsedPage], turned_pages: frozenset[int]) -> list[Block]:
    chains = text_chains(pages)
    evidence = Evidence(
        whole_words=whole_words(
            [
                line
                for page in pages
                for region in [*page.preproc_blocks, *page.discarded_blocks]
                for line in word_lines(region)
            ]
        ),
        turned_pages=turned_pages,
    )
    blocks: list[Block] = []
    for page in pages:
        continued = page.continued()
        for region in page.preproc_blocks:
            match region:
                case Text() if region.index not in continued:
                    lines = chains[page.page_idx, region.index]
                    blocks.extend(passages(lines, evidence, blocks))
                case Title():
                    blocks.extend(headings(region, page, evidence))
                case Plain():
                    blocks.extend(paragraphs(shown_lines(page, region.lines), evidence))
                case Group():
                    blocks.extend(
                        block
                        for member in region.blocks
                        for block in member_blocks(member, page, evidence)
                    )
        # A footnote says something the document says, so it is kept: as a paragraph after
        # the text of its page, where it interrupts no sentence of that page. The engine does
        # not always list the notes of a page in the order they stand, so they go top down.
        notes = [margin for margin in page.discarded_blocks if isinstance(margin, Plain)]
        for note in sorted(notes, key=lambda note: area(page, note.bbox).top):
            blocks.extend(paragraphs(shown_lines(page, note.lines), evidence))
    return blocks


def names_document(title: str) -> bool:
    return any(character.isalnum() for character in title) and not title.lower().endswith(
        FILE_SUFFIXES
    )


def document_title(own: str, blocks: list[Block], file_name: str) -> str:
    top_first = sorted(
        (block for block in blocks if isinstance(block, Heading)),
        key=lambda heading: heading.level,
    )
    if names_document(own):
        return own
    if top_first:
        return top_first[0].text
    return file_name


def extract_pdf(file: Path, engine: Path) -> Extracted:
    with PdfDocument(file) as document:
        own_title = WORD_GAP.join(str(document.get_metadata_value(TITLE_ENTRY)).split())
        numbers = range(len(document))
        turned_pages = frozenset(number for number in numbers if document[number].get_rotation())
        # A file without one character in its text layer can only be read as pictures.
        lettered = any(document[number].get_textpage().count_chars() for number in numbers)
    with TemporaryDirectory() as folder:
        read = lettered_pages if lettered else picture_pages
        pages = read(file, engine, Path(folder))
    blocks = pdf_blocks(pages, turned_pages)
    if not blocks:
        raise UnreadableSource(f"no readable text in {file.name}")
    return Extracted(title=document_title(own_title, blocks, file.name), blocks=blocks)
