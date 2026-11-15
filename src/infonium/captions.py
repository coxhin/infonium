import html
import re
from bisect import bisect_right
from collections import Counter
from enum import StrEnum
from math import inf
from urllib.parse import urljoin

import m3u8
from pydantic import BaseModel, ConfigDict
from yt_dlp.webvtt import CueBlock, parse_fragment

from infonium.document import LINE_BREAK, Seconds, has_words
from infonium.transcript import CLOSING_MARKS, SENTENCE_ENDS, Utterance

EXTRACTOR = "captions/1"
MILLISECONDS_PER_SECOND = 1000
# yt-dlp gives the times of a cue in ticks of the clock of MPEG streams.
TICKS_PER_SECOND = 90_000

MARKUP = re.compile(r"<[^<>]*>")
# What captions put in square brackets apart from a word, and without a number, is a sound
# or a speaker, not words spoken.
ANNOTATION = re.compile(r"(?<!\w)\[[^\[\]\d\n]*\]")
# What a line of captions begins with where the speaker changes: chevrons, or a dash.
SPEAKER_CHANGE = re.compile(r"^[ \t\xa0]*(?:(?:>{2,}|-(?=[ \t\xa0]))[ \t\xa0]*)+", re.MULTILINE)
# A short run of words before a colon at the start of a line, with what may follow it in
# brackets, on that line or the next: where captions name a speaker, and where words that
# are spoken stand too.
LABEL = re.compile(
    r"^[ \t\xa0]*([^\n:()]{1,40}?)(?:\s{0,3}\([^():]{0,40}\))?[ \t\xa0]*:(?=\s|$)", re.MULTILINE
)
LABEL_NAME = 1
# A name in capitals that labels this many lines of one track is a speaker's. In 197 tracks
# that people made, every name that did so was one, while of those that stood once some
# were words that are spoken or shown, such as "JAMA 2012:".
SPEAKER_LINES = 2
# Marks that end a clause inside a sentence.
CLAUSE_ENDS = SENTENCE_ENDS | frozenset(
    ",;:\N{FULLWIDTH COMMA}\N{FULLWIDTH SEMICOLON}\N{FULLWIDTH COLON}\N{IDEOGRAPHIC COMMA}"
)
# An utterance runs to the end of its clause when the captions mark one this soon after it
# begins; otherwise it ends with its caption line.
UTTERANCE_LIMIT_SECONDS = 10.0


class CaptionFormat(StrEnum):
    # The forms of captions that are read, the most exact first: YouTube's own, which times
    # each word and holds a rolling line once, and then WebVTT.
    JSON3 = "json3"
    VTT = "vtt"


class CaptionPart(BaseModel):
    # Words of a caption, the stretch of the recording in which they are shown, and whether
    # they end a line of the captions. A part is a cue, or one of the timed words of a cue.
    model_config = ConfigDict(frozen=True)

    text: str
    start: Seconds
    end: Seconds
    ends_line: bool


class CaptionSegment(BaseModel):
    # One segment of a caption track and the moment of the recording its times count from.
    url: str
    counts_from: Seconds


class PlaylistSegment(BaseModel):
    uri: str
    duration: Seconds
    discontinuity: bool


class MediaPlaylist(BaseModel):
    segments: list[PlaylistSegment]


class VttCue(BaseModel):
    text: str
    start: int
    end: int


class Json3Segment(BaseModel):
    utf8: str
    tOffsetMs: int = 0


class Json3Event(BaseModel):
    tStartMs: int
    dDurationMs: int = 0
    segs: list[Json3Segment] = []


class Json3Captions(BaseModel):
    events: list[Json3Event]


def json3_parts(content: bytes) -> list[CaptionPart]:
    return [
        CaptionPart(
            text=segment.utf8,
            start=(event.tStartMs + segment.tOffsetMs) / MILLISECONDS_PER_SECOND,
            end=(event.tStartMs + event.dDurationMs) / MILLISECONDS_PER_SECOND,
            ends_line=number == len(event.segs),
        )
        for event in Json3Captions.model_validate_json(content).events
        for number, segment in enumerate(event.segs, start=1)
    ]


def vtt_parts(content: bytes, counts_from: float) -> list[CaptionPart]:
    cues = [
        VttCue.model_validate(cue.as_json)
        for cue in parse_fragment(content)
        if isinstance(cue, CueBlock)
    ]
    # A cue is trimmed so that it reads the same wherever in a file it stands.
    return [
        CaptionPart(
            text=html.unescape(MARKUP.sub("", cue.text)).strip(),
            start=counts_from + cue.start / TICKS_PER_SECOND,
            end=counts_from + cue.end / TICKS_PER_SECOND,
            ends_line=True,
        )
        for cue in cues
    ]


# The cues of a segment count their times from the start of the stretch of the stream the
# segment lies in: a stream spliced from several recordings, such as an introduction before
# a talk, starts counting anew at each splice.
def caption_segments(playlist: str, url: str) -> list[CaptionSegment]:
    segments: list[CaptionSegment] = []
    elapsed = 0.0
    counts_from = 0.0
    for segment in MediaPlaylist.model_validate(m3u8.loads(playlist).data).segments:
        counts_from = elapsed if segment.discontinuity else counts_from
        segments.append(CaptionSegment(url=urljoin(url, segment.uri), counts_from=counts_from))
        elapsed += segment.duration
    return segments


# A cue shown across the cut between two segments is in both of them, and is read once.
def segmented_parts(segments: list[CaptionSegment], contents: list[bytes]) -> list[CaptionPart]:
    cut = [
        part
        for segment, content in zip(segments, contents, strict=True)
        for part in vtt_parts(content, segment.counts_from)
    ]
    return list(dict.fromkeys(cut))


# The parts with what is not spoken taken out of their words: sounds and speakers in
# brackets, the marks of a change of speaker, and the names of speakers before a colon.
def spoken_parts(parts: list[CaptionPart]) -> list[CaptionPart]:
    unmarked = [SPEAKER_CHANGE.sub("", ANNOTATION.sub("", part.text)) for part in parts]
    labelled = Counter(label[LABEL_NAME] for text in unmarked for label in LABEL.finditer(text))
    speakers = {
        name
        for name, count in labelled.items()
        if count >= SPEAKER_LINES and name == name.upper() and name != name.lower()
    }
    return [
        part.model_copy(
            update={
                "text": LABEL.sub(
                    lambda label: "" if label[LABEL_NAME] in speakers else label.group(), text
                )
            }
        )
        for part, text in zip(parts, unmarked, strict=True)
    ]


# The parts that lie in a recording of a length, in the order they are spoken, each with the
# moment it is over. A part is over when a later one begins: a rolling line stays shown
# while the next is spoken. Parts that begin together are said together, and each lasts as
# long as it is shown. Captions may run on past the end of a recording that was cut after
# they were written; those words are not in it.
def heard_parts(parts: list[CaptionPart], length: float) -> list[CaptionPart]:
    spoken = spoken_parts(
        sorted((part for part in parts if part.start < length), key=lambda part: part.start)
    )
    moments = [part.start for part in spoken]
    later = [*moments, inf]
    return [
        part.model_copy(
            update={
                "end": min(
                    max(part.end, part.start), later[bisect_right(moments, part.start)], length
                )
            }
        )
        for part in spoken
    ]


# An utterance is a clause where the captions mark one, and a line of the captions where
# they do not, so that it begins where a listener would begin.
def caption_utterances(parts: list[CaptionPart], length: float) -> list[Utterance]:
    heard = heard_parts(parts, length)
    ends_clause = [part.text.rstrip().rstrip(CLOSING_MARKS)[-1:] in CLAUSE_ENDS for part in heard]
    # For each part, the moment the clause it lies in is over: never, where no mark ends it.
    clause_overs: list[float] = []
    clause_over = inf
    for part, ends in zip(reversed(heard), reversed(ends_clause), strict=True):
        clause_over = part.end if ends else clause_over
        clause_overs.append(clause_over)
    phrases: list[list[CaptionPart]] = [[]]
    for part, ends, clause_over in zip(heard, ends_clause, reversed(clause_overs), strict=True):
        phrase = phrases[-1]
        if not phrase and not has_words(part.text):
            continue
        phrase.append(part)
        if ends or (part.ends_line and clause_over > phrase[0].start + UTTERANCE_LIMIT_SECONDS):
            phrases.append([])
    return [
        Utterance(
            text="".join(part.text + (LINE_BREAK if part.ends_line else "") for part in phrase),
            moment=phrase[0].start,
            duration=phrase[-1].end - phrase[0].start,
        )
        for phrase in phrases
        if phrase
    ]
