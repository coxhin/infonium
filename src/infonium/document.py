from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Offset = Annotated[int, Field(ge=0)]

RowSeparator = Literal["\n"]
CellSeparator = Literal["\t"]
ROW_SEPARATOR: RowSeparator = "\n"
CELL_SEPARATOR: CellSeparator = "\t"


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


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    QUOTE = "quote"
    CODE = "code"
    TABLE = "table"
    FIGURE = "figure"


class Heading(Tagged):
    kind: Literal[BlockKind.HEADING] = BlockKind.HEADING
    level: Annotated[int, Field(ge=1, le=6)]
    text: str
    marks: list[Mark]


class Paragraph(Tagged):
    kind: Literal[BlockKind.PARAGRAPH] = BlockKind.PARAGRAPH
    text: str
    marks: list[Mark]


class ListItem(Tagged):
    kind: Literal[BlockKind.LIST_ITEM] = BlockKind.LIST_ITEM
    ordered: bool
    depth: Offset
    text: str
    marks: list[Mark]


class Quote(Tagged):
    kind: Literal[BlockKind.QUOTE] = BlockKind.QUOTE
    text: str
    marks: list[Mark]


class Code(Tagged):
    kind: Literal[BlockKind.CODE] = BlockKind.CODE
    text: str


class Table(Tagged):
    # The text holds every cell, rows split by one separator and cells by the other, so a
    # character range addresses table text the same way as any other block's text. The
    # block carries both separators so that a reader need not know them in advance.
    kind: Literal[BlockKind.TABLE] = BlockKind.TABLE
    text: str
    row_separator: RowSeparator = ROW_SEPARATOR
    cell_separator: CellSeparator = CELL_SEPARATOR


class Figure(Tagged):
    kind: Literal[BlockKind.FIGURE] = BlockKind.FIGURE
    source: str
    text: str


Block = Annotated[
    Heading | Paragraph | ListItem | Quote | Code | Table | Figure,
    Field(discriminator="kind"),
]

BLOCKS = TypeAdapter(list[Block])


class Document(BaseModel):
    id: str
    item_id: str
    extractor: str
    blocks: Annotated[list[Block], Field(min_length=1)]


def document_of(item_id: str, extractor: str, blocks: list[Block]) -> Document:
    content = b"\n".join([item_id.encode(), extractor.encode(), BLOCKS.dump_json(blocks)])
    return Document(
        id=sha256(content).hexdigest(), item_id=item_id, extractor=extractor, blocks=blocks
    )
