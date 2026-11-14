from pydantic import BaseModel
from rapidfuzz import fuzz
from rapidfuzz.distance import ScoreAlignment

from infonium.document import Document, Offset

QUOTE_SCORE_FLOOR = 90.0
QUOTE_LENGTH_FLOOR = 8


class Citation(BaseModel):
    block: int
    quote: str


class Anchor(BaseModel):
    document_id: str
    block: Offset
    start: Offset
    end: Offset
    quote: str


class QuoteMatch(BaseModel):
    block: Offset
    start: Offset
    end: Offset
    score: float


def quote_alignment(quote: str, text: str) -> ScoreAlignment:
    alignment = fuzz.partial_ratio_alignment(quote, text)
    if alignment is None:
        raise TypeError("an alignment without a score cutoff always exists")
    return alignment


def quote_match(block: int, quote: str, text: str) -> QuoteMatch:
    alignment = quote_alignment(quote, text)
    found = text[alignment.dest_start : alignment.dest_end]
    # A block shorter than the quote aligns with only part of it; that is not the quote.
    whole_quote = alignment.src_end - alignment.src_start == len(quote)
    return QuoteMatch(
        block=block,
        start=alignment.dest_start + len(found) - len(found.lstrip()),
        end=alignment.dest_end - len(found) + len(found.rstrip()),
        score=alignment.score if whole_quote else 0.0,
    )


def best_match(document: Document, citation: Citation) -> QuoteMatch:
    matches = [
        quote_match(block=number, quote=citation.quote, text=block.text)
        for number, block in enumerate(document.blocks)
    ]
    return max(matches, key=lambda match: (match.score, match.block == citation.block))


def anchors_of(document: Document, citations: list[Citation]) -> list[Anchor]:
    matches = [
        best_match(document, citation)
        for citation in citations
        if len(citation.quote) >= QUOTE_LENGTH_FLOOR
    ]
    return [
        Anchor(
            document_id=document.id,
            block=match.block,
            start=match.start,
            end=match.end,
            quote=document.blocks[match.block].text[match.start : match.end],
        )
        for match in matches
        if match.score >= QUOTE_SCORE_FLOOR
    ]
