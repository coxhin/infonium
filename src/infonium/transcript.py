from itertools import pairwise
from typing import Annotated
from unicodedata import category

from pydantic import AfterValidator, BaseModel

from infonium.document import (
    WORD_GAP,
    Block,
    Paragraph,
    Place,
    Seconds,
    TimePlace,
    flowed,
    is_unspaced,
)

# A silence this long between two utterances starts a new paragraph.
PARAGRAPH_PAUSE_SECONDS = 2.0
# A paragraph that has run this long ends with the utterance that next ends a sentence, and
# at once when none of its utterances has ended one: speech that was captioned without
# punctuation never does.
PARAGRAPH_TARGET_SECONDS = 45.0
# A paragraph ends once it has run this long, however long its sentence goes on.
PARAGRAPH_LIMIT_SECONDS = 90.0
PUNCTUATION = "P"
SENTENCE_ENDS = frozenset(
    ".?!\N{HORIZONTAL ELLIPSIS}"
    "\N{IDEOGRAPHIC FULL STOP}\N{FULLWIDTH QUESTION MARK}\N{FULLWIDTH EXCLAMATION MARK}"
)
# Marks that may close a quotation or a bracket after the mark that ends a sentence.
CLOSING_MARKS = (
    "\"')]"
    "\N{RIGHT DOUBLE QUOTATION MARK}\N{RIGHT SINGLE QUOTATION MARK}"
    "\N{RIGHT CORNER BRACKET}\N{RIGHT WHITE CORNER BRACKET}\N{FULLWIDTH RIGHT PARENTHESIS}"
)


class Utterance(BaseModel):
    # A stretch of speech and the words spoken in it, as a recogniser or a caption gives them.
    # The words are kept as one line, and there is at least one.
    text: Annotated[str, AfterValidator(flowed)]
    moment: Seconds
    duration: Seconds


def in_spoken_order(utterances: list[Utterance]) -> list[Utterance]:
    if any(later.moment < earlier.moment for earlier, later in pairwise(utterances)):
        raise ValueError("the utterances of a transcript are in the order they are spoken")
    return utterances


class Transcript(BaseModel):
    utterances: Annotated[list[Utterance], AfterValidator(in_spoken_order)]


def ends_sentence(text: str) -> bool:
    return text.rstrip(CLOSING_MARKS)[-1:] in SENTENCE_ENDS


def ends_passage(passage: list[Utterance], following: Utterance) -> bool:
    last = passage[-1]
    ended = last.moment + last.duration
    lasted = ended - passage[0].moment
    unpunctuated = not any(ends_sentence(utterance.text) for utterance in passage)
    return (
        following.moment - ended >= PARAGRAPH_PAUSE_SECONDS
        or lasted >= PARAGRAPH_LIMIT_SECONDS
        or (lasted >= PARAGRAPH_TARGET_SECONDS and (ends_sentence(last.text) or unpunctuated))
    )


def gap_after(spoken: str) -> str:
    # Two utterances are two things said, and a space stands between them in any script, as
    # in captions written without punctuation. Only a mark of a script written without
    # spaces is its own gap.
    closed = is_unspaced(spoken[-1]) and category(spoken[-1]).startswith(PUNCTUATION)
    return "" if closed else WORD_GAP


def paragraph_of(passage: list[Utterance]) -> Paragraph:
    text = ""
    places: list[Place] = []
    for utterance in passage:
        text = f"{text}{gap_after(text) if text else ''}{utterance.text}"
        places.append(
            TimePlace(
                start=len(text) - len(utterance.text),
                end=len(text),
                moment=utterance.moment,
                duration=utterance.duration,
            )
        )
    return Paragraph(text=text, marks=[], places=places)


def transcript_blocks(transcript: Transcript) -> list[Block]:
    passages: list[list[Utterance]] = []
    for utterance in transcript.utterances:
        if passages and not ends_passage(passages[-1], utterance):
            passages[-1].append(utterance)
        else:
            passages.append([utterance])
    return [paragraph_of(passage) for passage in passages]
