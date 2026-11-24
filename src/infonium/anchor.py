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
JSON_ESCAPE = re.compile(r'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))')
JSON_ESCAPES = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}
FORMAT_CHARACTER = "Cf"
COMPARISON_FORM: Final = "NFKD"
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


def is_invisible(character: str) -> bool:
    return unicodedata.category(character) == FORMAT_CHARACTER


def plain_character(character: str) -> str:
    # A character that is neither white space nor invisible, as it is compared: decomposed,
    # with plain quotation marks and dashes, and with its case folded.
    decomposed = unicodedata.normalize(COMPARISON_FORM, character)
    return decomposed.translate(PLAIN_PUNCTUATION).casefold()


def comparable(text: str, ignored: set[int]) -> Comparable:
    # A quote counts as found only when the source holds the same words. What may differ is
    # how they are typeset: letter case, spacing, the shape of quotation marks and dashes,
    # composed or decomposed accents and invisible format characters. The comparable form
    # drops exactly those differences and remembers, for each character it keeps, where in the
    # text that character came from. Characters at the ignored offsets are left out as well.
    kept: list[str] = []
    origins: list[int] = []
    for offset, character in enumerate(text):
        if offset in ignored or is_invisible(character):
            continue
        if character.isspace():
            plain = "" if kept[-1:] in ([], [" "]) else " "
        else:
            plain = plain_character(character)
        kept.extend(plain)
        origins.extend([offset] * len(plain))
    return Comparable(text="".join(kept), origins=origins)


def footnote_offsets(text: str) -> set[int]:
    return {offset for mark in REFERENCE_MARK.finditer(text) for offset in range(*mark.span())}


def block_forms(text: str) -> list[Comparable]:
    # A block is compared as it stands and, failing that, without its footnote marks, which a
    # quote of the sentence around them leaves out. The quote itself is always taken whole, so
    # bracketed numbers that it does hold have to match.
    return [comparable(text, ignored=set()), comparable(text, ignored=footnote_offsets(text))]


def quote_holders(
    forms: list[list[Comparable]], needle: str, cited_number: int
) -> list[tuple[int, Comparable]]:
    # The block a quote is anchored in: the cited one when it holds the quote, otherwise the
    # nearest block that does. A short quote is not moved, since it could turn up anywhere.
    movable = len(needle) >= RELOCATED_QUOTE_FLOOR
    holders = [
        (number, form)
        for number, candidates in enumerate(forms)
        if movable or number == cited_number
        for form in [candidate for candidate in candidates if needle in candidate.text][:1]
    ]
    return sorted(holders, key=lambda holder: abs(holder[0] - cited_number))[:1]


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


def json_unescaped(quote: str) -> str:
    # A quote as it reads with the escape sequences of JSON decoded: a document is given to a
    # model as JSON, and a model may copy a tab, a line break or a backslash of a block as the
    # sequence that stood for it there.
    return JSON_ESCAPE.sub(
        lambda escape: chr(int(escape[1], 16)) if escape[1] else JSON_ESCAPES[escape[2]], quote
    )


def anchors_of(document: Document, citations: list[Citation]) -> list[Anchor]:
    # A quote that is not found as written is looked for once more as it reads with the
    # escapes of JSON decoded.
    forms = [block_forms(block.text) for block in document.blocks]
    anchors: list[Anchor] = []
    for citation in citations:
        for quote in dict.fromkeys([citation.quote, json_unescaped(citation.quote)]):
            needle = comparable(quote, ignored=set()).text.strip(QUOTE_EDGE)
            holders = quote_holders(forms, needle, citation.block_number) if needle else []
            anchors.extend(anchor_in(document, number, held, needle) for number, held in holders)
            if holders:
                break
    return anchors
