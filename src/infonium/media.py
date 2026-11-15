import html
import re
import subprocess
from enum import StrEnum
from itertools import takewhile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Final, Literal
from urllib.parse import urljoin

import m3u8
from pydantic import BaseModel, ConfigDict, Field
from yt_dlp import YoutubeDL
from yt_dlp.extractor import get_info_extractor
from yt_dlp.webvtt import CueBlock, parse_fragment

from infonium.document import SOFT_HYPHEN, MediaType, Seconds, Tagged, UnreadableSource
from infonium.transcript import CLOSING_MARKS, SENTENCE_ENDS, Transcript, Utterance
from infonium.webpage import LINE_BREAK

# yt-dlp also reads recordings out of news sites and blogs, whose pages are articles first,
# so its whole list of sites cannot tell a media page from an article. These are its
# extractors for sites whose pages are recordings, or lists of recordings.
MEDIA_SITES = (
    "Youtube",
    "YoutubeYtBe",
    "YoutubeTab",
    "BiliBili",
    "BilibiliSpaceVideo",
    "BilibiliCollectionList",
    "BilibiliSeriesList",
    "BilibiliFavoritesList",
    "BilibiliPlaylist",
    "Vimeo",
    "TedTalk",
    "TedPlaylist",
    "TedSeries",
    "ApplePodcasts",
    "Soundcloud",
    "SoundcloudSet",
    "SoundcloudUser",
)

# AAC in an MP4 file: Chrome, Firefox and Safari play it in an audio element, its index of
# samples leads a seek to the exact frame, and ffmpeg decodes it for a speech recogniser.
# Most sites already serve this coding, so their sound is kept as it is.
EXTRACTOR = "captions/1"
AUDIO_MEDIA_TYPE = "audio/mp4"
AUDIO_FILE = "sound.m4a"
AAC = "aac"
# ffmpeg's word for sound that is moved as it is, not coded anew.
KEPT = "copy"
# The sound of a file with several is its first.
FIRST_SOUND = "a:0"
FFPROBE = ["ffprobe", "-v", "error", "-of", "default=noprint_wrappers=1:nokey=1"]
CODEC_OF_SOUND = ["-select_streams", FIRST_SOUND, "-show_entries", "stream=codec_name"]
LENGTH_OF_FILE = ["-show_entries", "format=duration"]
FFMPEG = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i"]
# The index of samples goes before the sound, so that a player need not read to the end of
# the file before it can play or seek.
SOUND_ALONE = ["-map", f"0:{FIRST_SOUND}", "-movflags", "+faststart", "-c:a"]

# A format without a picture, in the coding that is kept, before any other: only the sound
# is kept.
SOUND_FORMATS = "bestaudio/best"
FORMAT_ORDER = [f"acodec:{AAC}"]
DOWNLOAD_NAME = "download.%(ext)s"
# The recordings of a list are named, not fetched, and one of them is enough to know a list.
LISTED_NOT_FOLLOWED: Final = "in_playlist"
ENTRIES = "entries"

# yt-dlp marks with this the automatic captions of the words as they were spoken; the other
# automatic tracks are translations of them by machine.
ORIGINAL_CAPTIONS = "-orig"
LANGUAGE_SUBTAGS = re.compile(r"[-_]")
# The caption channels of a broadcast (CC1, DTVCC1 and the like) were typed while it was on
# air, and run seconds behind the words.
BROADCAST_CHANNEL = re.compile(r"\b(?:DTV)?CC\d+$")
# A caption track cut into the segments of a stream.
SEGMENTED = "m3u8_native"
MILLISECONDS_PER_SECOND = 1000
# yt-dlp gives the times of a cue in ticks of the clock of MPEG streams.
TICKS_PER_SECOND = 90_000

MARKUP = re.compile(r"<[^>]*>")
# What captions put in square brackets apart from a word, and without a number, is a sound
# or a speaker, not words spoken.
ANNOTATION = re.compile(r"(?<!\w)\[[^\]\d\n]*\]")
# What a line of captions begins with to say who speaks: chevrons or a dash where the
# speaker changes, and a name in capitals before a colon and words in lower case.
SPEAKER_TAG = re.compile(
    r"^(?:[ \t]*(?:>{2,}|-(?=\s)|[A-Z][A-Z0-9\s.'-]*(?:\([^()]*\))?:(?=\s+[^\n]*[a-z])))+",
    re.MULTILINE,
)
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


class CaptionFile(BaseModel):
    # One form in which a site serves a caption track, as yt-dlp names its parts.
    ext: str
    url: str
    name: str | None = None
    protocol: str | None = None


class Listing(BaseModel):
    # What yt-dlp reports of the recording on a page, under yt-dlp's names.
    title: str
    language: str | None = None
    is_live: bool | None = None
    subtitles: dict[str, list[CaptionFile]] = {}
    automatic_captions: dict[str, list[CaptionFile]] = {}


class DownloadedFile(BaseModel):
    filepath: Path


class Download(BaseModel):
    requested_downloads: list[DownloadedFile]


class PlaylistSegment(BaseModel):
    uri: str
    duration: Seconds
    discontinuity: bool


class MediaPlaylist(BaseModel):
    segments: list[PlaylistSegment]


class CaptionSegment(BaseModel):
    # One segment of a caption track and the moment of the recording its times count from.
    url: str
    origin: Seconds


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


class CaptionPart(BaseModel):
    # Words of a caption, the stretch of the recording they belong to, and whether they end
    # a line of the captions. A part is a cue, or one of the timed words of a cue.
    model_config = ConfigDict(frozen=True)

    text: str
    start: Seconds
    end: Seconds
    ends_line: bool


class AudioTrack(BaseModel):
    file: Path
    media_type: MediaType


class FetchedMediaKind(StrEnum):
    CAPTIONED = "captioned"
    UNCAPTIONED = "uncaptioned"


class CaptionedMedia(Tagged):
    kind: Literal[FetchedMediaKind.CAPTIONED] = FetchedMediaKind.CAPTIONED
    title: str
    audio: AudioTrack
    transcript: Transcript


class UncaptionedMedia(Tagged):
    kind: Literal[FetchedMediaKind.UNCAPTIONED] = FetchedMediaKind.UNCAPTIONED
    title: str
    audio: AudioTrack


FetchedMedia = Annotated[CaptionedMedia | UncaptionedMedia, Field(discriminator="kind")]


def is_media_page(url: str) -> bool:
    return any(get_info_extractor(site).suitable(url) for site in MEDIA_SITES)


def primary_language(tag: str) -> str:
    return LANGUAGE_SUBTAGS.split(tag)[0].casefold()


# The tracks in one language, the one tagged exactly as it first.
def tracks_in(tracks: dict[str, list[CaptionFile]], language: str) -> list[list[CaptionFile]]:
    spoken = [tag for tag in tracks if primary_language(tag) == primary_language(language)]
    return [
        tracks[tag] for tag in sorted(spoken, key=lambda tag: tag.casefold() != language.casefold())
    ]


# The captions that hold the words as they were spoken, the best first: those people made
# before the automatic ones, and each track in the form that is read most exactly. A page
# that does not say which language is spoken has none, since any of its tracks may be a
# translation.
def usable_captions(listing: Listing) -> list[CaptionFile]:
    if listing.language is None:
        return []
    originals = {
        tag.removesuffix(ORIGINAL_CAPTIONS): files
        for tag, files in listing.automatic_captions.items()
        if tag.endswith(ORIGINAL_CAPTIONS)
    }
    tracks = [
        *tracks_in(listing.subtitles, listing.language),
        *tracks_in(originals, listing.language),
    ]
    return [
        file
        for track in tracks
        for form in CaptionFormat
        for file in track
        if file.ext == form and not BROADCAST_CHANNEL.search(file.name or "")
    ]


def has_words(text: str) -> bool:
    return bool(text.replace(SOFT_HYPHEN, "").split())


def untagged(text: str) -> str:
    return SPEAKER_TAG.sub("", ANNOTATION.sub("", text))


def json3_parts(content: bytes) -> list[CaptionPart]:
    events = [event for event in Json3Captions.model_validate_json(content).events if event.segs]
    shown_until = [event.tStartMs + event.dDurationMs for event in events]
    # A line of rolling captions stays shown while the next is spoken.
    line_ends = [
        *(
            min(shown, later.tStartMs)
            for shown, later in zip(shown_until, events[1:], strict=False)
        ),
        *shown_until[-1:],
    ]
    parts: list[CaptionPart] = []
    for event, line_end in zip(events, line_ends, strict=True):
        starts = [event.tStartMs + segment.tOffsetMs for segment in event.segs]
        ends = [*starts[1:], max(starts[-1], line_end)]
        last = [*[False] * (len(event.segs) - 1), True]
        parts.extend(
            CaptionPart(
                text=untagged(segment.utf8),
                start=start / MILLISECONDS_PER_SECOND,
                end=end / MILLISECONDS_PER_SECOND,
                ends_line=ends_line,
            )
            for segment, start, end, ends_line in zip(event.segs, starts, ends, last, strict=True)
        )
    return parts


def vtt_parts(content: bytes, origin: float) -> list[CaptionPart]:
    cues = [
        VttCue.model_validate(block.as_json)
        for block in parse_fragment(content)
        if isinstance(block, CueBlock)
    ]
    # A cue is trimmed so that it reads the same wherever in a file it stands.
    return [
        CaptionPart(
            text=untagged(html.unescape(MARKUP.sub("", cue.text))).strip(),
            start=origin + cue.start / TICKS_PER_SECOND,
            end=origin + cue.end / TICKS_PER_SECOND,
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
    origin = 0.0
    for segment in MediaPlaylist.model_validate(m3u8.loads(playlist).data).segments:
        origin = elapsed if segment.discontinuity else origin
        segments.append(CaptionSegment(url=urljoin(url, segment.uri), origin=origin))
        elapsed += segment.duration
    return segments


def ends_clause(text: str) -> bool:
    return text.rstrip().rstrip(CLOSING_MARKS)[-1:] in CLAUSE_ENDS


def clause_ends_by(parts: list[CaptionPart], moment: float) -> bool:
    return any(ends_clause(part.text) for part in takewhile(lambda part: part.end <= moment, parts))


# An utterance is a clause where the captions mark one, and a line of the captions where
# they do not, so that it begins where a listener would begin. Captions may run on past the
# end of a recording that was cut after they were written; those words are not in it.
def caption_utterances(parts: list[CaptionPart], length: float) -> list[Utterance]:
    heard = sorted((part for part in parts if part.start < length), key=lambda part: part.start)
    passages: list[list[CaptionPart]] = [[]]
    for number, part in enumerate(heard):
        passage = passages[-1]
        if not passage and not has_words(part.text):
            continue
        passage.append(part)
        limit = passage[0].start + UTTERANCE_LIMIT_SECONDS
        if ends_clause(part.text) or (
            part.ends_line and not clause_ends_by(heard[number + 1 :], limit)
        ):
            passages.append([])
    return [
        Utterance(
            text="".join(part.text + (LINE_BREAK if part.ends_line else "") for part in passage),
            moment=passage[0].start,
            duration=min(passage[-1].end, length) - passage[0].start,
        )
        for passage in passages
        if passage
    ]


def fetch_caption_parts(downloader: YoutubeDL, file: CaptionFile) -> list[CaptionPart]:
    content = downloader.urlopen(file.url).read()
    if file.protocol == SEGMENTED:
        cut = [
            part
            for segment in caption_segments(content.decode(), file.url)
            for part in vtt_parts(downloader.urlopen(segment.url).read(), segment.origin)
        ]
        # A cue shown across the cut between two segments is in both of them.
        return list(dict.fromkeys(cut))
    if file.ext == CaptionFormat.JSON3:
        return json3_parts(content)
    return vtt_parts(content, origin=0.0)


# What ffprobe reports of the first sound of a file. It reports nothing of a file without
# sound, and of one it cannot read, for which it gives its reason.
def probed(file: Path, entries: list[str]) -> list[str]:
    report = subprocess.run([*FFPROBE, *entries, str(file)], capture_output=True, text=True)
    if not report.stdout.split():
        raise UnreadableSource(report.stderr.strip() or f"no sound in {file.name}")
    return report.stdout.split()


def audio_track(file: Path, directory: Path) -> AudioTrack:
    coding = KEPT if probed(file, CODEC_OF_SOUND)[0] == AAC else AAC
    sound = directory / AUDIO_FILE
    subprocess.run(
        [*FFMPEG, str(file), *SOUND_ALONE, coding, str(sound)], check=True, capture_output=True
    )
    return AudioTrack(file=sound, media_type=AUDIO_MEDIA_TYPE)


def fetch_media(url: str, directory: Path) -> FetchedMedia:
    with (
        TemporaryDirectory(dir=directory) as downloads,
        YoutubeDL(
            {
                "format": SOUND_FORMATS,
                "format_sort": FORMAT_ORDER,
                "outtmpl": str(Path(downloads) / DOWNLOAD_NAME),
                "noplaylist": True,
                "extract_flat": LISTED_NOT_FOLLOWED,
                "playlistend": 1,
                "quiet": True,
                "noprogress": True,
            }
        ) as downloader,
    ):
        found = downloader.extract_info(url, download=False)
        if ENTRIES in found:
            raise UnreadableSource(f"{url} lists recordings; give the address of one of them")
        listing = Listing.model_validate(found)
        if listing.is_live:
            raise UnreadableSource(f"{url} is still being broadcast and has no recording yet")
        parts = [
            part
            for file in usable_captions(listing)[:1]
            for part in fetch_caption_parts(downloader, file)
        ]
        download = Download.model_validate(downloader.process_ie_result(found, download=True))
        audio = audio_track(download.requested_downloads[0].filepath, directory)
    utterances = caption_utterances(parts, length=float(probed(audio.file, LENGTH_OF_FILE)[0]))
    if utterances:
        return CaptionedMedia(
            title=listing.title, audio=audio, transcript=Transcript(utterances=utterances)
        )
    return UncaptionedMedia(title=listing.title, audio=audio)
