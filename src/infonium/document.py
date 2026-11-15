from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationInfo,
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
Seconds = Annotated[
    float,
    Field(
        ge=0,
        allow_inf_nan=False,
        description="A length of time. A moment is the time from the start of its recording.",
    ),
]
MediaType = Annotated[str, StringConstraints(pattern=r"^[a-z]+/[a-z0-9][a-z0-9.+-]*$")]

HTML_MEDIA_TYPE = "text/html"

RowSeparator = Literal["\n"]
CellSeparator = Literal["\t"]
ROW_SEPARATOR: RowSeparator = "\n"
CELL_SEPARATOR: CellSeparator = "\t"

WORD_GAP = " "
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
TEXT = "text"
PLACES = "places"


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
    # page is the page as a viewer shows it, turned the way its file says; the box is cut to
    # the page and measured from its top left corner.
    kind: Literal[PlaceKind.PAGE] = PlaceKind.PAGE
    start: Offset
    end: Offset
    page: PageNumber
    left: PageFraction
    top: PageFraction
    width: PageFraction
    height: PageFraction


class TimePlace(Tagged):
    # The stretch of a recording in which one part of a block's text is spoken.
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
    # A text that failed its own validation is absent here, and its error is the one to report.
    length = len(validation.data.get(TEXT, ""))
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
    level: Annotated[int, Field(ge=1, le=6)]
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
    unplaced = {number: {PLACES} for number, block in enumerate(blocks) if not block.places}
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


def joined(parts: list[str]) -> Joined:
    # Parts of one text that arrive apart, such as the lines of a paragraph or the utterances
    # of a speaker, are put together the way the text would be written: one space between
    # words, none where either side is written without spaces, and none after a part that
    # ends in a soft hyphen, which marks a word that goes on in the next part and is dropped.
    # Each part gets the range of the text that holds its words.
    text = ""
    ranges: list[TextRange] = []
    for part in parts:
        words = WORD_GAP.join(part.split())
        if not words:
            raise ValueError("a part of a text holds at least one word")
        mid_word = text.endswith(SOFT_HYPHEN)
        text = text.removesuffix(SOFT_HYPHEN)
        apart = text and not mid_word and not is_unspaced(text[-1]) and not is_unspaced(words[0])
        start = len(text) + (len(WORD_GAP) if apart else 0)
        ranges.append(TextRange(start=start, end=start + len(words.removesuffix(SOFT_HYPHEN))))
        text = f"{text}{WORD_GAP if apart else ''}{words}"
    return Joined(text=text.removesuffix(SOFT_HYPHEN), ranges=ranges)
