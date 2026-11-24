from enum import StrEnum
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from infonium.anchor import Anchor, Citation, anchors_of
from infonium.document import Document, Tagged

SEVERAL_DOCUMENTS = "the anchors of a passage lie in one document"
OUT_OF_ORDER = "the anchors of a passage lie in different blocks, in the order of the document"
EMPTY_ANCHOR = "an anchor of a passage holds at least one character"
NOT_HELD = "block {block} of the document does not hold these words at {start} to {end}"


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


def standing_in(passage: Passage, marked_in: Document, document: Document) -> Standing:
    # Where a passage that was marked in one revision of a source stands in another: in the
    # revision it was marked in, where it is. This first form finds each anchor's quote by
    # itself, in the block of the same number or the nearest that holds it, and takes the
    # passage as lost unless every anchor is found; what stands around the passage in the
    # revision it was marked in is not used yet.
    if document.id == marked_in.id:
        return Found(passage=passage, exact=True)
    found = [
        anchors_of(document, [Citation(block_number=anchor.block_number, quote=anchor.quote)])
        for anchor in passage.anchors
    ]
    anchors = [anchor for anchors in found for anchor in anchors]
    numbers = [anchor.block_number for anchor in anchors]
    in_order = all(later > earlier for earlier, later in pairwise(numbers))
    if not all(found) or not in_order:
        return Lost()
    return Found(passage=Passage(anchors=anchors), exact=True)
