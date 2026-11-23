from pydantic import BaseModel

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


class DraftStatement(BaseModel):
    # A statement as a model writes it: its text and the passages it says it rests on.
    text: str
    citations: list[Citation]


class Statement(BaseModel):
    # A statement with the passages of the document it rests on, as they were found there.
    text: str
    anchors: list[Anchor]


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
    # A citation whose quote the document does not hold gives no anchor.
    return Statement(text=draft.text, anchors=anchors_of(document, draft.citations))
