from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from infonium.document import BlockNumber, Mark, Offset, Tagged


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
