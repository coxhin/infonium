from typing import Annotated

from pydantic import BaseModel, Field

from infonium.anchor import Anchor, Citation, anchors_of
from infonium.document import BlockKind, Document

HEADING_LABEL = "heading"
BLOCKS_EXPLAINED = f"""\
The document arrives as numbered blocks. Each block starts with its number in square \
brackets. A block whose brackets also say "{HEADING_LABEL}" is a heading.
"""
# The rules every statement a model writes about a document keeps. The instructions that
# include them say which statements carry citations, and how many.
CITING_RULES = """\
- State only what the document says. Add no outside knowledge and no opinion of your own.
- A citation names one block by its number and quotes the words in that block that support \
the statement.
- Copy each quote from the block character for character, in the document's own language. \
Do not translate it, correct it, or shorten it with an ellipsis.
- Keep a quote to one sentence or a part of one, within a single block. Choose the passage \
where the document makes the point most directly.
"""
# What a citation of several documents names besides, for the instructions that give a model
# several documents.
SOURCED_CITING_RULE = """\
- A citation also names the document its block lies in, by the document's handle. Where \
these rules say "the document", they mean the document a statement cites.
"""


class DraftStatement(BaseModel):
    # A statement as a model writes it: its text and the passages it says it rests on.
    text: str
    citations: list[Citation]


class Statement(BaseModel):
    # A statement with the passages of the documents it rests on, as they were found there,
    # and how many of the citations the model gave it named no passage that was found: a
    # statement whose citations were all lost is a claim without its source, not a statement
    # that makes no claim of a source. A statement stored before the count was kept lost none.
    text: str
    anchors: list[Anchor]
    lost_citations: Annotated[int, Field(ge=0)] = 0


def numbered_blocks(title: str, document: Document) -> str:
    # The document as a model is given it: its title, then each block that holds text, under
    # its number.
    numbered = [
        f"[{number}{f' {HEADING_LABEL}' if block.kind == BlockKind.HEADING else ''}] {block.text}"
        for number, block in enumerate(document.blocks)
        if block.text
    ]
    return "\n\n".join([f"Title: {title}", *numbered])


def grounded(document: Document, draft: DraftStatement) -> Statement:
    # A citation whose quote the document does not hold gives no anchor, and is lost.
    anchors = anchors_of(document, draft.citations)
    return Statement(
        text=draft.text, anchors=anchors, lost_citations=len(draft.citations) - len(anchors)
    )


class SourcedCitation(BaseModel):
    # A citation of a block of one of several documents, which it names by its handle.
    document: str
    block_number: int
    quote: str


class SourcedDraftStatement(BaseModel):
    # A statement as a model writes it about several documents: its text and the passages,
    # in whichever documents, it says it rests on.
    text: str
    citations: list[SourcedCitation]


def grounded_across(documents: dict[str, Document], draft: SourcedDraftStatement) -> Statement:
    # Each citation is looked up in the document that the handle it gives names, as the
    # documents map them; one whose handle names none of them, or whose quote that document
    # does not hold, gives no anchor, and is lost.
    anchors = [
        anchor
        for citation in draft.citations
        if citation.document in documents
        for anchor in anchors_of(
            documents[citation.document],
            [Citation(block_number=citation.block_number, quote=citation.quote)],
        )
    ]
    return Statement(
        text=draft.text, anchors=anchors, lost_citations=len(draft.citations) - len(anchors)
    )
