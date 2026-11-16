import subprocess
import sys
import wave
from collections.abc import Generator, Iterable, Iterator
from contextlib import closing
from io import BytesIO
from math import exp, sumprod
from pathlib import Path
from tempfile import TemporaryFile
from typing import Annotated, Final, Self

from openai import OpenAI
from pydantic import BaseModel, Field, SecretStr, model_validator

from infonium.document import Seconds, UnreadableSource, has_words
from infonium.recording import COMPLAINER, FFMPEG, started_on
from infonium.transcript import Transcript, Utterance

EXTRACTOR = "speech/1"

# The recogniser hears one channel at this rate, each sample a whole number of two bytes.
CHANNELS = 1
SAMPLE_RATE = 16000
SAMPLE_BYTES = 2
BYTES_PER_SECOND = CHANNELS * SAMPLE_RATE * SAMPLE_BYTES
SAMPLE_FORMATS = {"little": "s16le", "big": "s16be"}
SAMPLE_TYPE_CODE: Final = "h"

# How loud the sound is, is measured over slivers a tenth of a second long.
SLIVERS_PER_SECOND = 10
SLIVER_BYTES = BYTES_PER_SECOND // SLIVERS_PER_SECOND
# A recogniser hears half a minute at a time. Given more, it stitches what it hears together
# itself, and there it repeats itself, drops speech and gives times past the end. So a
# recording is cut here into pieces the length of an utterance, and each piece is one
# request: no piece is longer than 16 seconds, and where a recogniser returns a piece as
# one segment, that is how long an utterance gets.
PIECE_SLIVERS = 16 * SLIVERS_PER_SECOND

PIECE_NAME = "piece.wav"
WAVE_MEDIA_TYPE = "audio/wav"
TIMED_REPLY: Final = "verbose_json"
SEGMENT_TIMES: Final = "segment"
# The longest a recogniser may take over one piece, which covers loading its model first.
RECOGNISER_TIMEOUT_SECONDS = 120.0
RECOGNISER_RETRIES = 1

Probability = Annotated[float, Field(ge=0, le=1)]
LogProbability = Annotated[float, Field(le=0, allow_inf_nan=False)]


class Recogniser(BaseModel):
    # Where a recogniser is reached, the key it asks for, and the model it transcribes with.
    base_url: str
    api_key: SecretStr
    model: str


class Piece(BaseModel):
    # A stretch of a recording that one request carries: its samples, the moment of the
    # recording at which it starts, and how long it lasts.
    moment: Seconds
    duration: Seconds
    sound: bytes


class Segment(BaseModel):
    # What a recogniser says of one stretch of a piece: the words, when in the piece they
    # are spoken, and its own doubt about them, as the probability that nothing is spoken
    # there and the mean log probability of the words it wrote. A reply whose times or
    # doubts are not such numbers is refused here, where it comes in.
    text: str
    start: Seconds
    end: Seconds
    no_speech_prob: Probability
    avg_logprob: LogProbability

    @model_validator(mode="after")
    def ends_no_earlier_than_it_starts(self) -> Self:
        if self.end < self.start:
            raise ValueError("a segment ends no earlier than it starts")
        return self


class Recognition(BaseModel):
    segments: list[Segment]


def cut_sliver(loudness: list[float]) -> int:
    # The sliver before which a piece that has reached its limit ends: the quietest of the
    # later half of what the piece may hold. That is a gap between words where a gap fills a
    # sliver. Slivers stand side by side, a tenth of a second each, so a gap that is shorter,
    # or that lies across two of them, fills none: the quietest sliver may then hold the end
    # of a word, and the piece is cut in it. A longer stretch weighed as one can be the
    # quietest with a short word between two gaps in its middle.
    first = len(loudness) // 2
    later = loudness[first:]
    return first + later.index(min(later))


def piece_after(slivers_before: int, sound: bytes) -> Piece:
    return Piece(
        moment=slivers_before / SLIVERS_PER_SECOND,
        duration=len(sound) / BYTES_PER_SECOND,
        sound=sound,
    )


def decoded(recording: Path) -> Generator[bytes]:
    # The sound of a recording as the decoder gives it, a sliver at a time; the last may be a
    # short one. The recording is the sound that is kept of a source, which starts at zero
    # and runs without a break, so a count of its samples is a time from its start.
    command = [
        *FFMPEG,
        "-ac",
        str(CHANNELS),
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        SAMPLE_FORMATS[sys.byteorder],
        "-",
    ]
    # The decoder's complaints go to a file: a pipe that nobody reads would fill and stall it.
    with (
        TemporaryFile() as complaints,
        started_on(recording, command, stdout=subprocess.PIPE, stderr=complaints) as decoder,
    ):
        if decoder.stdout is None:
            raise RuntimeError("the decoder was started without a pipe for its sound")
        sliver = decoder.stdout.read(SLIVER_BYTES)
        while len(sliver) == SLIVER_BYTES:
            yield sliver
            sliver = decoder.stdout.read(SLIVER_BYTES)
        # What is left when the sound ends is given only once the decoder is known to have
        # ended well: one that was killed may leave half a sample.
        status = decoder.wait()
        if status != 0:
            complaints.seek(0)
            written = complaints.read().decode(errors="replace").strip()
            first_complaint = COMPLAINER.sub("", written.partition("\n")[0])
            # A decoder that is killed complains of nothing. Nor does one that is told to end,
            # by SIGTERM or SIGINT: ffmpeg then gives the rest of the sound and ends with a
            # failing status. How it ended is then the reason, in the words Python has for
            # it. Sound may have been read by then, so the reason says no more than that the
            # decoder failed.
            ending = subprocess.CalledProcessError(status, command[0])
            raise UnreadableSource(f"the decoder failed: {first_complaint or ending}")
        if sliver:
            yield sliver


def pieces(slivers: Iterable[bytes]) -> Iterator[Piece]:
    # Sound that arrives a sliver at a time, cut into pieces.
    sound = bytearray()
    loudness: list[float] = []
    slivers_before = 0
    for sliver in slivers:
        samples = memoryview(sliver).cast(SAMPLE_TYPE_CODE)
        sound += sliver
        loudness.append(sumprod(samples, samples))
        if len(loudness) == PIECE_SLIVERS:
            cut = cut_sliver(loudness)
            yield piece_after(slivers_before, bytes(sound[: cut * SLIVER_BYTES]))
            slivers_before += cut
            del sound[: cut * SLIVER_BYTES]
            del loudness[:cut]
    if sound:
        yield piece_after(slivers_before, bytes(sound))


def wave_file(sound: bytes) -> bytes:
    packed = BytesIO()
    with wave.open(packed, "wb") as file:
        file.setnchannels(CHANNELS)
        file.setsampwidth(SAMPLE_BYTES)
        file.setframerate(SAMPLE_RATE)
        file.writeframes(sound)
    return packed.getvalue()


def utterances_of(recognition: Recognition, piece: Piece) -> list[Utterance]:
    # What a recogniser heard in one piece, timed from the start of the recording. Three
    # kinds of segment are no utterance. One without words. One that starts after the piece
    # has ended: nothing is spoken there, the recogniser has gone on by itself. And one that
    # the recogniser itself holds more probably silent than worded: music and noise that it
    # takes for speech come back as words it does not believe. An utterance ends with its
    # piece at the latest.
    return [
        Utterance(
            text=segment.text,
            moment=piece.moment + segment.start,
            duration=min(segment.end, piece.duration) - segment.start,
        )
        for segment in recognition.segments
        if has_words(segment.text)
        and segment.start < piece.duration
        and segment.no_speech_prob < exp(segment.avg_logprob)
    ]


def transcribe(recording: Path, recogniser: Recogniser) -> Transcript:
    # What is spoken in a recording, as a recogniser behind OpenAI's transcription API hears
    # it. The recogniser detects the language, piece by piece. A recording without speech
    # has no utterances.
    utterances: list[Utterance] = []
    # The connection to the recogniser and the decoder are ended with the reading, however
    # the reading ends.
    with (
        OpenAI(
            base_url=recogniser.base_url,
            api_key=recogniser.api_key.get_secret_value(),
            timeout=RECOGNISER_TIMEOUT_SECONDS,
            max_retries=RECOGNISER_RETRIES,
        ) as endpoint,
        closing(decoded(recording)) as slivers,
    ):
        for piece in pieces(slivers):
            reply = endpoint.audio.transcriptions.with_raw_response.create(
                file=(PIECE_NAME, wave_file(piece.sound), WAVE_MEDIA_TYPE),
                model=recogniser.model,
                response_format=TIMED_REPLY,
                timestamp_granularities=[SEGMENT_TIMES],
            )
            utterances.extend(utterances_of(Recognition.model_validate_json(reply.text), piece))
    return Transcript(utterances=utterances)
