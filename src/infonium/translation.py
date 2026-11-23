from enum import StrEnum
from typing import Annotated, Literal
from unicodedata import category

from pydantic import BaseModel, Field

from infonium.document import (
    WORD_GAP,
    Block,
    BlockNumber,
    Code,
    Document,
    FormulaMark,
    Heading,
    ListItem,
    Mark,
    Offset,
    Paragraph,
    Quotation,
    Table,
    Tagged,
    TextRange,
    TimePlace,
)
from infonium.rendition import Rendered, Wording
from infonium.transcript import ends_sentence

# A segment of a recording ends where a place ends a sentence, and once it has run this long
# where none does, so that a line of the translation can be read while it is heard.
SEGMENT_LIMIT_SECONDS = 10.0
LETTER = "L"


class Segment(BaseModel):
    # A part of a block's text, and what it says in the translation's language. The marks are
    # counted in the translated text.
    start: Offset
    end: Offset
    text: str
    marks: list[Mark]


class TranslatedBlock(BaseModel):
    # A block of a document in the translation's language: the parts of its text that were
    # translated, in the order of the text, and the model that wrote them. A block of a
    # recording is translated a sentence at a time, so that each sentence keeps the stretch
    # of time in which it is spoken; any other block is one segment.
    block_number: BlockNumber
    model: str
    segments: list[Segment]


class ProgressKind(StrEnum):
    UNASKED = "unasked"
    TRANSLATING = "translating"
    STOPPED = "stopped"
    TRANSLATED = "translated"
    UNNEEDED = "unneeded"


class Unasked(Tagged):
    # Nobody has asked for the document in this language.
    kind: Literal[ProgressKind.UNASKED] = ProgressKind.UNASKED


class Translating(Tagged):
    # The translation is under way: of the blocks that are to be translated, so many are.
    kind: Literal[ProgressKind.TRANSLATING] = ProgressKind.TRANSLATING
    translated: Annotated[int, Field(ge=0)]
    total: Annotated[int, Field(ge=0)]


class Stopped(Tagged):
    # The translation stopped before its end, for this reason. What was translated is kept,
    # and a translation that is asked for again goes on from there.
    kind: Literal[ProgressKind.STOPPED] = ProgressKind.STOPPED
    reason: str


class Translated(Tagged):
    # Every block that is to be translated is.
    kind: Literal[ProgressKind.TRANSLATED] = ProgressKind.TRANSLATED


class Unneeded(Tagged):
    # The document is written in the translation's language already.
    kind: Literal[ProgressKind.UNNEEDED] = ProgressKind.UNNEEDED


# How far the translation of one document into one language has come.
Progress = Annotated[
    Unasked | Translating | Stopped | Translated | Unneeded, Field(discriminator="kind")
]


def says_words(text: str) -> bool:
    return any(category(character).startswith(LETTER) for character in text)


def block_marks(block: Block) -> list[Mark]:
    match block:
        case Heading() | Paragraph() | ListItem() | Quotation():
            return block.marks
        case _:
            return []


def is_translated(block: Block) -> bool:
    # A listing is kept as it is written, and so is a formula; a block with no letter of any
    # script says nothing in words.
    whole_formula = any(
        isinstance(mark, FormulaMark) and mark.start == 0 and mark.end == len(block.text)
        for mark in block_marks(block)
    )
    return not isinstance(block, Code) and not whole_formula and says_words(block.text)


def translated_numbers(document: Document) -> list[BlockNumber]:
    return [number for number, block in enumerate(document.blocks) if is_translated(block)]


def segment_ranges(block: Block) -> list[TextRange]:
    # A block spoken in a recording is cut after a place that ends a sentence, or once it has
    # run long; any other block is one segment.
    times = [place for place in block.places if isinstance(place, TimePlace)]
    ranges: list[TextRange] = []
    first = 0
    for at, place in enumerate(times):
        lasted = place.moment + place.duration - times[first].moment
        ended = ends_sentence(block.text[place.start : place.end])
        if at + 1 == len(times) or ended or lasted >= SEGMENT_LIMIT_SECONDS:
            ranges.append(TextRange(start=times[first].start, end=place.end))
            first = at + 1
    return ranges or [TextRange(start=0, end=len(block.text))]


def table_cells(table: Table) -> list[list[str]]:
    return [row.split(table.cell_separator) for row in table.text.split(table.row_separator)]


def segment_wording(block: Block, segment: TextRange) -> Wording:
    # The part of a block's text in a segment, with the parts of its marks that lie in it.
    marks = [
        mark.model_copy(
            update={
                "start": max(mark.start, segment.start) - segment.start,
                "end": min(mark.end, segment.end) - segment.start,
            }
        )
        for mark in block_marks(block)
        if mark.start < segment.end and segment.start < mark.end
    ]
    return Wording(text=block.text[segment.start : segment.end], marks=marks)


def block_wordings(block: Block) -> list[Wording]:
    # What of a block is asked for: a table cell by cell, those that say something in words,
    # and any other block segment by segment.
    if isinstance(block, Table):
        cells = [cell for row in table_cells(block) for cell in row]
        return [Wording(text=cell, marks=[]) for cell in cells if says_words(cell)]
    return [segment_wording(block, segment) for segment in segment_ranges(block)]


def translated_block(
    block_number: BlockNumber, block: Block, model: str, rendered: list[Rendered]
) -> TranslatedBlock:
    # The translated cells of a table take the places of their sources, each on one line, so
    # that the translated text keeps the table's grid; a cell with no words stays as it is.
    if isinstance(block, Table):
        answers = iter(rendered)
        rows = [
            block.cell_separator.join(
                WORD_GAP.join(next(answers).text.split()) if says_words(cell) else cell
                for cell in row
            )
            for row in table_cells(block)
        ]
        text = block.row_separator.join(rows)
        segments = [Segment(start=0, end=len(block.text), text=text, marks=[])]
    else:
        segments = [
            Segment(start=segment.start, end=segment.end, text=done.text, marks=done.marks)
            for segment, done in zip(segment_ranges(block), rendered, strict=True)
        ]
    return TranslatedBlock(block_number=block_number, model=model, segments=segments)
