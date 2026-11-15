from itertools import pairwise
from typing import Annotated

from pydantic import AfterValidator, BaseModel

from infonium.document import Block, Paragraph, Place, Seconds, TimePlace, flowed, joined

# A silence this long between two utterances starts a new paragraph.
PARAGRAPH_PAUSE_SECONDS = 2.0
# A paragraph that has run this long ends with the utterance that next ends a sentence.
PARAGRAPH_TARGET_SECONDS = 45.0
# A paragraph ends once it has run this long, whether or not a sentence ends there: speech
# that was captioned without punctuation never ends one.
PARAGRAPH_LIMIT_SECONDS = 90.0
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
    return (
        following.moment - ended >= PARAGRAPH_PAUSE_SECONDS
        or lasted >= PARAGRAPH_LIMIT_SECONDS
        or (lasted >= PARAGRAPH_TARGET_SECONDS and ends_sentence(last.text))
    )


def paragraph_of(passage: list[Utterance]) -> Paragraph:
    flow = joined([utterance.text for utterance in passage])
    places: list[Place] = [
        TimePlace(
            start=spoken.start,
            end=spoken.end,
            moment=utterance.moment,
            duration=utterance.duration,
        )
        for spoken, utterance in zip(flow.ranges, passage, strict=True)
    ]
    return Paragraph(text=flow.text, marks=[], places=places)


def transcript_blocks(transcript: Transcript) -> list[Block]:
    passages: list[list[Utterance]] = []
    for utterance in transcript.utterances:
        if passages and not ends_passage(passages[-1], utterance):
            passages[-1].append(utterance)
        else:
            passages.append([utterance])
    return [paragraph_of(passage) for passage in passages]
