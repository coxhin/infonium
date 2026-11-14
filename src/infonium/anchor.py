import re
import unicodedata
from typing import Final

from pydantic import BaseModel

from infonium.document import BlockNumber, Document, Offset

RELOCATED_QUOTE_FLOOR = 12
QUOTE_EDGE = (
    " .,;:!?\"'"
    "\N{IDEOGRAPHIC FULL STOP}\N{FULLWIDTH COMMA}\N{FULLWIDTH SEMICOLON}"
    "\N{FULLWIDTH COLON}\N{FULLWIDTH EXCLAMATION MARK}\N{FULLWIDTH QUESTION MARK}"
)
REFERENCE_MARK = re.compile(r"\[\d+\]")
FORMAT_CHARACTER = "Cf"
COMPATIBILITY_FORM: Final = "NFKC"
PLAIN_PUNCTUATION = str.maketrans(
    {
        "\N{LEFT DOUBLE QUOTATION MARK}": '"',
        "\N{RIGHT DOUBLE QUOTATION MARK}": '"',
        "\N{DOUBLE LOW-9 QUOTATION MARK}": '"',
        "\N{LEFT-POINTING DOUBLE ANGLE QUOTATION MARK}": '"',
        "\N{RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK}": '"',
        "\N{LEFT SINGLE QUOTATION MARK}": "'",
        "\N{RIGHT SINGLE QUOTATION MARK}": "'",
        "\N{SINGLE LOW-9 QUOTATION MARK}": "'",
        "\N{HYPHEN}": "-",
        "\N{NON-BREAKING HYPHEN}": "-",
        "\N{FIGURE DASH}": "-",
        "\N{EN DASH}": "-",
        "\N{EM DASH}": "-",
        "\N{HORIZONTAL BAR}": "-",
    }
)


class Citation(BaseModel):
    block_number: int
    quote: str


class Anchor(BaseModel):
    document_id: str
    block_number: BlockNumber
    start: Offset
    end: Offset
    quote: str


class Comparable(BaseModel):
    text: str
    origins: list[int]


# A quote counts as found only when the source holds the same words. What may differ is how
# they are typeset: letter case, spacing, the shape of quotation marks and dashes, invisible
# format characters and footnote marks. The comparable form drops exactly those differences
# and remembers, for each character it keeps, where in the text that character came from.
def comparable(text: str) -> Comparable:
    footnotes = {offset for mark in REFERENCE_MARK.finditer(text) for offset in range(*mark.span())}
    kept: list[str] = []
    origins: list[int] = []
    for offset, character in enumerate(text):
        if offset in footnotes or unicodedata.category(character) == FORMAT_CHARACTER:
            continue
        if character.isspace():
            plain = "" if kept[-1:] in ([], [" "]) else " "
        else:
            compatible = unicodedata.normalize(COMPATIBILITY_FORM, character)
            plain = compatible.translate(PLAIN_PUNCTUATION).casefold()
        kept.extend(plain)
        origins.extend([offset] * len(plain))
    return Comparable(text="".join(kept), origins=origins)


# The block a quote is anchored in: the cited one when it holds the quote, otherwise the
# nearest block that does. A short quote is not moved, since it could turn up anywhere.
def quote_holders(comparables: list[Comparable], needle: str, cited_number: int) -> list[int]:
    movable = len(needle) >= RELOCATED_QUOTE_FLOOR
    holders = [
        number
        for number, candidate in enumerate(comparables)
        if needle in candidate.text and (movable or number == cited_number)
    ]
    return sorted(holders, key=lambda number: abs(number - cited_number))[:1]


def anchor_in(document: Document, block_number: int, held: Comparable, needle: str) -> Anchor:
    found = held.text.index(needle)
    start = held.origins[found]
    end = held.origins[found + len(needle) - 1] + 1
    return Anchor(
        document_id=document.id,
        block_number=block_number,
        start=start,
        end=end,
        quote=document.blocks[block_number].text[start:end],
    )


def anchors_of(document: Document, citations: list[Citation]) -> list[Anchor]:
    comparables = [comparable(block.text) for block in document.blocks]
    anchors: list[Anchor] = []
    for citation in citations:
        needle = comparable(citation.quote).text.strip(QUOTE_EDGE)
        holders = quote_holders(comparables, needle, citation.block_number) if needle else []
        anchors.extend(
            anchor_in(document, number, comparables[number], needle) for number in holders
        )
    return anchors
