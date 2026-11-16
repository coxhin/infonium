from enum import StrEnum
from hashlib import sha256
from http import HTTPStatus
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationInfo,
    model_validator,
)

Offset = Annotated[
    int,
    Field(ge=0, description="A position in a block's text, counted in Unicode code points."),
]
BlockNumber = Annotated[
    int, Field(ge=0, description="The position of a block in its document, counted from 0.")
]
Depth = Annotated[
    int,
    Field(ge=0, description="How many lists enclose the list an item belongs to; 0 for none."),
]
PageNumber = Annotated[
    int, Field(ge=0, description="The position of a page in its file, counted from 0.")
]
PageFraction = Annotated[
    float,
    Field(ge=0, le=1, description="A distance across or down a page, as a part of its size."),
]
HeadingLevel = Annotated[int, Field(ge=1, le=6)]
Seconds = Annotated[
    float,
    Field(
        ge=0,
        allow_inf_nan=False,
        description="A length of time. A moment is the time from the start of its recording.",
    ),
]
# A media type as it is registered: a type and a subtype in lower case, with no parameters.
MEDIA_TYPE_PATTERN = r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$"
MediaType = Annotated[str, StringConstraints(pattern=MEDIA_TYPE_PATTERN)]

HTML_MEDIA_TYPE = "text/html"
PDF_MEDIA_TYPE = "application/pdf"

RowSeparator = Literal["\n"]
CellSeparator = Literal["\t"]
ROW_SEPARATOR: RowSeparator = "\n"
CELL_SEPARATOR: CellSeparator = "\t"

WORD_GAP = " "
LINE_BREAK = "\n"
SOFT_HYPHEN = "\N{SOFT HYPHEN}"
# Chinese characters, kana, and the punctuation and full-width forms written with them. Text
# in these takes no space between words.
UNSPACED_CODE_POINTS = (
    range(0x2E80, 0x3100),
    range(0x3400, 0xA000),
    range(0xF900, 0xFB00),
    range(0xFF00, 0xFFF0),
    range(0x20000, 0x40000),
)
# A page that holds fewer words than this may hold no article of its own: it may be an
# opening, a shell that its scripts fill, or a page that a site answers with in place of the
# one asked for. A short post holds as little, so such a page is looked at again, not refused.
LITTLE_TEXT_WORDS = 200
TEXT_FIELD = "text"
PLACES_FIELD = "places"
NO_WORDS = "a part of a text holds at least one word"


class Style(StrEnum):
    STRONG = "strong"
    EMPHASIS = "emphasis"
    UNDERLINE = "underline"
    CODE = "code"
    SUBSCRIPT = "subscript"
    SUPERSCRIPT = "superscript"


class MarkKind(StrEnum):
    STYLE = "style"
    LINK = "link"


class Tagged(BaseModel):
    # A tag has a default so that construction need not repeat it; this keeps the protocol
    # listing the tag as always present, which is what a reader of the union switches on.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class StyleMark(Tagged):
    kind: Literal[MarkKind.STYLE] = MarkKind.STYLE
    style: Style
    start: Offset
    end: Offset


class LinkMark(Tagged):
    kind: Literal[MarkKind.LINK] = MarkKind.LINK
    target: str
    start: Offset
    end: Offset


Mark = Annotated[StyleMark | LinkMark, Field(discriminator="kind")]


class PlaceKind(StrEnum):
    PAGE = "page"
    TIME = "time"


class PagePlace(Tagged):
    # The box on a page that shows one part of a block's text, as a rule one line of it. The
    # page is the page as a viewer shows it, turned the way its file says, and the box is
    # measured from its top left corner. A box lies on its page: one that reaches past an edge
    # is cut by cutting its far edges as fractions of the page and taking its size from them,
    # which unlike cutting in the file's own units never leaves it a hair past the edge.
    kind: Literal[PlaceKind.PAGE] = PlaceKind.PAGE
    start: Offset
    end: Offset
    page: PageNumber
    left: PageFraction
    top: PageFraction
    width: PageFraction
    height: PageFraction

    @model_validator(mode="after")
    def on_its_page(self) -> Self:
        if self.left + self.width > 1 or self.top + self.height > 1:
            raise ValueError("a box lies on its page")
        return self


class TimePlace(Tagged):
    # The stretch in which one part of a block's text is spoken, in the recording that is
    # kept as the document's snapshot.
    kind: Literal[PlaceKind.TIME] = PlaceKind.TIME
    start: Offset
    end: Offset
    moment: Seconds
    duration: Seconds


# Where a part of a block's text sits in its source, beyond its place in the flow of the
# text. A web page has no such places; a paged file has boxes on its pages; a recording has
# stretches of time. An anchor is a range of a block's text and reaches the source through
# the places whose ranges overlap it. The places of one document are all of one kind, and
# that kind decides how a reader shows the source.
Place = Annotated[PagePlace | TimePlace, Field(discriminator="kind")]


def places_in_text(places: list[Place], validation: ValidationInfo) -> list[Place]:
    # A text that failed its own validation is absent here; its error is the one to report.
    if TEXT_FIELD not in validation.data:
        return places
    length = len(validation.data[TEXT_FIELD])
    for place in places:
        if not place.start <= place.end <= length:
            raise ValueError(f"a place covers {place.start} to {place.end} of {length} characters")
    return places


PlacesInText = Annotated[list[Place], AfterValidator(places_in_text)]


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    QUOTATION = "quotation"
    CODE = "code"
    TABLE = "table"
    FIGURE = "figure"


class Heading(Tagged):
    kind: Literal[BlockKind.HEADING] = BlockKind.HEADING
    level: HeadingLevel
    text: str
    marks: list[Mark]
    places: PlacesInText = []


class Paragraph(Tagged):
    kind: Literal[BlockKind.PARAGRAPH] = BlockKind.PARAGRAPH
    text: str
    marks: list[Mark]
    places: PlacesInText = []


class ListItem(Tagged):
    # One list item may be several blocks, when a code block or a nested list interrupts its
    # text. The first carries the item's place in its list; the later ones continue it.
    kind: Literal[BlockKind.LIST_ITEM] = BlockKind.LIST_ITEM
    ordered: bool
    depth: Depth
    position: Annotated[int, Field(ge=1)]
    continues: bool
    text: str
    marks: list[Mark]
    places: PlacesInText = []


class Quotation(Tagged):
    kind: Literal[BlockKind.QUOTATION] = BlockKind.QUOTATION
    text: str
    marks: list[Mark]
    places: PlacesInText = []


class Code(Tagged):
    kind: Literal[BlockKind.CODE] = BlockKind.CODE
    text: str
    places: PlacesInText = []


class Table(Tagged):
    # The text holds every cell, rows split by one separator and cells by the other, so a
    # character range addresses table text the same way as any other block's text. The
    # block carries both separators so that a reader need not know them in advance.
    kind: Literal[BlockKind.TABLE] = BlockKind.TABLE
    text: str
    row_separator: RowSeparator = ROW_SEPARATOR
    cell_separator: CellSeparator = CELL_SEPARATOR
    places: PlacesInText = []


class Figure(Tagged):
    kind: Literal[BlockKind.FIGURE] = BlockKind.FIGURE
    source: str
    text: str
    places: PlacesInText = []


Block = Annotated[
    Heading | Paragraph | ListItem | Quotation | Code | Table | Figure,
    Field(discriminator="kind"),
]

BLOCKS = TypeAdapter(list[Block])


def of_one_place_kind(blocks: list[Block]) -> list[Block]:
    if len({place.kind for block in blocks for place in block.places}) > 1:
        raise ValueError("the places of one document are all of one kind")
    return blocks


DocumentBlocks = Annotated[list[Block], Field(min_length=1), AfterValidator(of_one_place_kind)]


class UnreadableSource(Exception):
    pass


# What a reader is told of a source that holds more than a bound of its fetch allows, and of
# one whose fetch outlasts the time it is allowed.
OVERSIZE_SOURCE = "{source} holds more than infonium fetches"
OVERDUE_SOURCE = "{source} takes longer than infonium waits"
# What a reader is told of a page in which the extractor finds no text, of an address that
# answered with an error status, and of a site that answered with a check in place of the
# page asked for. A page saved from a browser that passed the check can be uploaded instead.
NO_TEXT = "no readable text at {source}"
ERROR_STATUS = "{source} answered {status}"
TURNED_AWAY = (
    "{site} turned infonium away with a check in place of the page; "
    "open the page in a browser, save it, and upload the file instead"
)
STATUS_NAMES = {status.value: f"{status.value} {status.phrase}" for status in HTTPStatus}


def error_status(source: str, status: int) -> str:
    # A status is told by its number and the name HTTP gives it; a number that HTTP leaves
    # unnamed, such as one a single service made up, is told by itself.
    return ERROR_STATUS.format(source=source, status=STATUS_NAMES.get(status, str(status)))


def error_text(error: BaseException) -> str:
    # What an error says, and the name of its kind where it says nothing.
    return str(error) or type(error).__name__


class Extracted(BaseModel):
    # What an extractor makes of one source, before it is kept as a document. An extractor
    # that finds nothing to read raises UnreadableSource.
    title: str
    blocks: DocumentBlocks


class Document(BaseModel):
    id: str
    item_id: str
    extractor: str
    snapshot: str
    media_type: MediaType
    blocks: DocumentBlocks


def document_of(
    item_id: str, extractor: str, snapshot: str, media_type: str, blocks: list[Block]
) -> Document:
    # A block without places is hashed as blocks were before they had any, so a source that
    # was kept then is the same document when it is read again.
    unplaced = {number: {PLACES_FIELD} for number, block in enumerate(blocks) if not block.places}
    hashed = BLOCKS.dump_json(blocks, exclude=unplaced)
    return Document(
        id=sha256(b"\n".join([item_id.encode(), extractor.encode(), hashed])).hexdigest(),
        item_id=item_id,
        extractor=extractor,
        snapshot=snapshot,
        media_type=media_type,
        blocks=blocks,
    )


class TextRange(BaseModel):
    start: Offset
    end: Offset


class Joined(BaseModel):
    text: str
    ranges: list[TextRange]


def is_unspaced(character: str) -> bool:
    return any(ord(character) in code_points for code_points in UNSPACED_CODE_POINTS)


def word_count(blocks: list[Block]) -> int:
    # Words are counted as a reader would count them in each script: a run of letters between
    # spaces, and every character of a script that is written without spaces.
    text = WORD_GAP.join(block.text for block in blocks)
    unspaced = sum(map(is_unspaced, text))
    spaced = "".join(WORD_GAP if is_unspaced(character) else character for character in text)
    return unspaced + len(spaced.split())


def seams(parts: list[str]) -> list[str]:
    # What stands before each of the parts of one text that arrive apart, the way the text
    # would be written: nothing before the first, and one space before each other part, but
    # none where either side is written without spaces, and none after a part that ends in a
    # soft hyphen, which marks a word that goes on in the next part.
    return [
        WORD_GAP
        if earlier
        and not earlier.endswith(SOFT_HYPHEN)
        and not is_unspaced(earlier[-1])
        and not is_unspaced(later[0])
        else ""
        for earlier, later in zip(["", *parts], parts, strict=False)
    ]


def put_together(parts: list[str]) -> str:
    # The parts of one text, each after its seam. The soft hyphen that joins a part to the
    # next is dropped; the last part keeps its own, for a word that goes on after the text.
    kept = [part.removesuffix(SOFT_HYPHEN) for part in parts[:-1]] + parts[-1:]
    return "".join(f"{seam}{part}" for seam, part in zip(seams(parts), kept, strict=True))


def on_one_line(part: str) -> str:
    # One part of a text as it would be written on one line: runs of spaces become one space,
    # and the lines of the part are put together as parts are. A part may end in a soft
    # hyphen, for a word that goes on in the next part.
    lines = [WORD_GAP.join(line.split()) for line in part.splitlines()]
    return put_together([line for line in lines if line])


def has_words(part: str) -> bool:
    return bool(on_one_line(part).removesuffix(SOFT_HYPHEN))


def flowed(part: str) -> str:
    # A part of a text on one line; a part with no words is refused.
    if not has_words(part):
        raise ValueError(NO_WORDS)
    return on_one_line(part)


def joined(parts: list[str]) -> Joined:
    # Parts of one text that arrive apart, such as the lines of a paragraph or the utterances
    # of a speaker, put together; each part gets the range of the text that holds its words.
    words = [flowed(part) for part in parts]
    ranges: list[TextRange] = []
    end = 0
    for seam, part_words in zip(seams(words), words, strict=True):
        start = end + len(seam)
        end = start + len(part_words.removesuffix(SOFT_HYPHEN))
        ranges.append(TextRange(start=start, end=end))
    return Joined(text=put_together(words).removesuffix(SOFT_HYPHEN), ranges=ranges)


def tabled(rows: list[list[str]]) -> str:
    # The text of a table whose rows hold these cells. A row in which no cell says anything
    # is left out.
    return ROW_SEPARATOR.join(CELL_SEPARATOR.join(cells) for cells in rows if any(cells))
