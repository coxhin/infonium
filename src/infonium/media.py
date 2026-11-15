import re
import subprocess
import time
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from statistics import median_low
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL
from yt_dlp.extractor import get_info_extractor

from infonium.captions import (
    CaptionFormat,
    CaptionPart,
    caption_segments,
    caption_utterances,
    json3_parts,
    segmented_parts,
    vtt_parts,
)
from infonium.document import WORD_GAP, MediaType, Seconds, Tagged, UnreadableSource
from infonium.transcript import Transcript

# yt-dlp also reads recordings out of news sites and blogs, whose pages are articles first,
# so its whole list of sites cannot tell a media page from an article. These are its
# extractors for sites whose pages are recordings, or lists of recordings; an address is a
# media page when one of them claims it, and only they are let read one.
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
AUDIO_MEDIA_TYPE = "audio/mp4"
AUDIO_FILE = "sound.m4a"
AAC = "aac"

# A file that is taken for a recording may be anything, and ffmpeg and ffprobe do what a
# file tells them. So they are pointed at one in a single way. The file stands on their
# standard input, so no name of it reaches them to be read as an option or an address. They
# open nothing but files of this machine. And they read only the containers of a recording,
# each by the name of ffmpeg's reader for it: the kinds of audio and video that a file's
# bytes can be told to be. A file of another kind may be a script that sends its reader to
# other files.
STANDARD_INPUT = "/dev/stdin"
LOCAL_FILE = "file"
OGG = "ogg"
RECORDING_FORMATS = f"aac,aiff,amr,asf,avi,flac,flv,matroska,mov,mp3,mpeg,{OGG},wav"
HELD_TO_RECORDINGS = ["-protocol_whitelist", LOCAL_FILE, "-format_whitelist", RECORDING_FORMATS]
# The sound of a file with several is its first.
FIRST_SOUND = "a:0"
# ffprobe answers from the index of a file, and reads no further into the sound than its
# first sixteen packets: enough to tell how long a packet usually lasts, whatever the first
# one does.
FIRST_PACKETS = "%+#16"
# ffprobe ends with this status when it cannot read a file. Its report then says why without
# the name the file stands under, which the line it writes of the file begins with.
UNREADABLE_STATUS = 1
FFPROBE = [
    "ffprobe",
    "-v",
    "error",
    *HELD_TO_RECORDINGS,
    "-show_error",
    "-of",
    "json",
    "-select_streams",
    FIRST_SOUND,
    "-read_intervals",
    FIRST_PACKETS,
    "-show_entries",
]
SOUND_ENTRIES = "stream=codec_name:packet=size"
TIMELINE_ENTRIES = "stream=start_pts,duration_ts,nb_frames:packet=duration"
LENGTH_ENTRIES = "format=duration"
TITLE_ENTRIES = "format=format_name:format_tags=title:stream_tags=title"
# ffmpeg reading the first sound of the recording on its standard input; what it is to make
# of that sound follows.
FFMPEG = [
    "ffmpeg",
    "-nostdin",
    "-v",
    "error",
    *HELD_TO_RECORDINGS,
    "-i",
    STANDARD_INPUT,
    "-map",
    f"0:{FIRST_SOUND}",
]
# The sound is written alone: without the chapters of its file, which would be a stream of
# their own, and with the index of samples before the sound, so that a player need not read
# to the end of the file before it can play or seek.
SOUND_ALONE = [*FFMPEG, "-y", "-map_chapters", "-1", "-movflags", "+faststart"]
# The timeline of a recording is the one its picture and its captions keep: where the sound
# starts late or breaks off, silence stands in the gap. Players and recognisers do not agree
# about a file that tells this only by the times of its packets, so the sound that is kept
# tells it in samples: it starts at zero and runs without a break. Sound in the coding that
# is kept is copied when the copy does so; other sound is coded anew through this filter,
# which puts the silence in.
ON_THE_TIMELINE = "aresample=async=1:first_pts=0"
COPIED = ["-c:a", "copy"]
CODED_ANEW = ["-c:a", AAC, "-af", ON_THE_TIMELINE]
# A coder leaves out of the length of a track what it trims from its ends, at most this
# many packets. A track that is shorter than its packets by more has packets that overlap,
# and one that is longer by a packet has packets that stand apart.
TRIMMED_PACKETS = 5
# The longest ffprobe may take over a file, and ffmpeg over writing its sound.
PROBE_LIMIT_SECONDS = 60.0
CONVERSION_LIMIT_SECONDS = 1800.0

# The bounds of the fetch of a media page. A recording may run this long, which lets a
# podcast of three hours through with room to spare.
SECONDS_PER_HOUR = 3600
LONGEST_RECORDING_SECONDS = 4.0 * SECONDS_PER_HOUR
# The longest silence a site is allowed, how often a request that broke off is made again,
# the longest the fetch may take in all, and the most the recording and its caption track,
# in one file or in the segments of a stream, may hold. The slowest site seen sent 120 KiB a
# second, at which three hours of sound take twenty-four minutes.
SILENCE_LIMIT_SECONDS = 20
REQUEST_RETRIES = 3
MEDIA_FETCH_LIMIT_SECONDS = 3600.0
RECORDING_LIMIT_BYTES = 2 * 1024**3
CAPTION_LIMIT_BYTES = 16 * 1024**2
DOWNLOADED_BYTES = "downloaded_bytes"

# A format without a picture before any with one, of those with a picture the smallest, and
# sound in the coding that is kept before other sound: only the sound is kept.
SOUND_FORMATS = "bestaudio/best"
FORMAT_ORDER = ["+res", f"acodec:{AAC}"]
DOWNLOAD_NAME = "download.%(ext)s"
CACHE_FOLDER = "cache"
# The recordings of a list are named, not fetched, and one of them is enough to know a list.
LISTED_NOT_FOLLOWED: Final = "in_playlist"
ENTRIES_READ = 1
ENTRIES = "entries"
# What yt-dlp reports is shown to the reader, so it carries no codes that colour a terminal.
UNCOLOURED: Final = "never"

# yt-dlp marks with this the automatic captions of the words as they were spoken; the other
# automatic tracks are translations of them by machine.
ORIGINAL_CAPTIONS = "-orig"
# A language is named by its primary language and then, after one of these marks, by what
# narrows it down: a script or a region.
LANGUAGE_NARROWING = re.compile(r"[-_]")
# The caption channels of a broadcast (CC1, DTVCC1 and the like) were typed while it was on
# air, and run seconds behind the words.
BROADCAST_CHANNEL = re.compile(r"\b(?:DTV)?CC\d+$")
# A caption track cut into the segments of a stream.
SEGMENTED = "m3u8_native"


class CaptionFile(BaseModel):
    # One form in which a site serves a caption track, as yt-dlp names its parts.
    ext: str
    url: str
    name: str | None = None
    protocol: str | None = None


class MediaPage(BaseModel):
    # What yt-dlp reports of the recording on a media page, under yt-dlp's names.
    title: str
    language: str | None = None
    duration: float | None = None
    is_live: bool | None = None
    subtitles: dict[str, list[CaptionFile]] = {}
    automatic_captions: dict[str, list[CaptionFile]] = {}


class DownloadedFile(BaseModel):
    filepath: Path


class Download(BaseModel):
    requested_downloads: list[DownloadedFile]


class ProbeError(BaseModel):
    string: str


class FailedProbe(BaseModel):
    # What ffprobe reports of a file it cannot read.
    error: ProbeError


class SoundStream(BaseModel):
    codec_name: str


class SoundPacket(BaseModel):
    size: int


class SoundProbe(BaseModel):
    # The first sound of a file and the first packets of that sound, where the file has them.
    streams: list[SoundStream]
    packets: list[SoundPacket]


class CopiedStream(BaseModel):
    # What the index of a copy tells of its track, in ticks of the track's clock: when it
    # starts, how long it runs, and how many packets it holds.
    start_pts: int
    duration_ts: int
    nb_frames: int


class CopiedPacket(BaseModel):
    duration: int


class CopyProbe(BaseModel):
    # A copy of sound holds that sound: one track, and packets in it.
    streams: Annotated[list[CopiedStream], Field(min_length=1)]
    packets: Annotated[list[CopiedPacket], Field(min_length=1)]


class FileLength(BaseModel):
    duration: Seconds


class LengthProbe(BaseModel):
    format: FileLength


class StreamTags(BaseModel):
    tags: dict[str, str] = {}


class FileTags(BaseModel):
    # The tags of a file that were asked for, each under the name its kind of file gives it,
    # and ffmpeg's reader for that kind.
    format_name: str
    tags: dict[str, str] = {}


class TagProbe(BaseModel):
    # The tags of a file, and those of its first sound where it has one.
    format: FileTags
    streams: list[StreamTags]


class Fetch(BaseModel):
    # The media page a fetch is for, and the moment on the clock by which it must be done.
    url: str
    deadline: float


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


# The tracks in one language, the one named exactly as it first.
def tracks_in(tracks: dict[str, list[CaptionFile]], language: str) -> list[list[CaptionFile]]:
    in_language = re.compile(
        rf"{re.escape(LANGUAGE_NARROWING.split(language)[0])}(?:{LANGUAGE_NARROWING.pattern}.*)?",
        re.IGNORECASE,
    )
    named = [name for name in tracks if in_language.fullmatch(name)]
    return [
        tracks[name]
        for name in sorted(named, key=lambda name: name.casefold() != language.casefold())
    ]


# The captions that hold the words as they were spoken, the best first: those people made
# before the automatic ones, and each track in the form that is read most exactly. A page
# that does not say which language is spoken has none, since any of its tracks may be a
# translation.
def usable_captions(page: MediaPage) -> list[CaptionFile]:
    if page.language is None:
        return []
    originals = {
        name.removesuffix(ORIGINAL_CAPTIONS): files
        for name, files in page.automatic_captions.items()
        if name.endswith(ORIGINAL_CAPTIONS)
    }
    tracks = [*tracks_in(page.subtitles, page.language), *tracks_in(originals, page.language)]
    return [
        file
        for track in tracks
        for form in CaptionFormat
        for file in track
        if file.ext == form and not BROADCAST_CHANNEL.search(file.name or "")
    ]


def within_bounds(fetch: Fetch, brought: int, limit: int) -> None:
    if brought > limit:
        raise UnreadableSource(f"{fetch.url} holds more than infonium fetches")
    if time.monotonic() > fetch.deadline:
        raise UnreadableSource(f"{fetch.url} takes longer than infonium waits")


def downloader_for(fetch: Fetch, downloads: Path) -> YoutubeDL:
    # yt-dlp reports how a download goes as it goes, and a report that raises ends it.
    def held_to_bounds(progress: Mapping[str, Any]) -> None:
        within_bounds(fetch, progress.get(DOWNLOADED_BYTES) or 0, RECORDING_LIMIT_BYTES)

    return YoutubeDL(
        {
            "allowed_extractors": [
                re.escape(get_info_extractor(site).IE_NAME) for site in MEDIA_SITES
            ],
            "format": SOUND_FORMATS,
            "format_sort": FORMAT_ORDER,
            "outtmpl": str(downloads / DOWNLOAD_NAME),
            "cachedir": str(downloads / CACHE_FOLDER),
            "noplaylist": True,
            "extract_flat": LISTED_NOT_FOLLOWED,
            "playlistend": ENTRIES_READ,
            "socket_timeout": SILENCE_LIMIT_SECONDS,
            "retries": REQUEST_RETRIES,
            "fragment_retries": REQUEST_RETRIES,
            # A recording with a part missing is not the recording.
            "skip_unavailable_fragments": False,
            "progress_hooks": [held_to_bounds],
            "color": {"stderr": UNCOLOURED, "stdout": UNCOLOURED},
            "quiet": True,
            "noprogress": True,
        }
    )


def caption_content(downloader: YoutubeDL, fetch: Fetch, url: str) -> bytes:
    content = downloader.urlopen(url).read(CAPTION_LIMIT_BYTES + 1)
    within_bounds(fetch, len(content), CAPTION_LIMIT_BYTES)
    return content


def fetched_parts(downloader: YoutubeDL, fetch: Fetch, file: CaptionFile) -> list[CaptionPart]:
    content = caption_content(downloader, fetch, file.url)
    if file.protocol == SEGMENTED:
        segments = caption_segments(content.decode(), file.url)
        contents: list[bytes] = []
        for segment in segments:
            contents.append(caption_content(downloader, fetch, segment.url))
            within_bounds(fetch, sum(map(len, contents)), CAPTION_LIMIT_BYTES)
        return segmented_parts(segments, contents)
    if file.ext == CaptionFormat.JSON3:
        return json3_parts(content)
    return vtt_parts(content, counts_from=0.0)


# What one of the programs does when it is run on a recording, within a time.
def run_on(file: Path, command: list[str], limit: float) -> subprocess.CompletedProcess[bytes]:
    with file.open("rb") as recording:
        return subprocess.run(command, stdin=recording, capture_output=True, timeout=limit)


# A file that ffprobe cannot read is a source with nothing to read. An ffprobe that ends
# without an answer for any other reason fails as a program.
def probed[Report: BaseModel](file: Path, entries: str, report: type[Report]) -> Report:
    done = run_on(file, [*FFPROBE, entries, STANDARD_INPUT], PROBE_LIMIT_SECONDS)
    if done.returncode == UNREADABLE_STATUS:
        failure = FailedProbe.model_validate_json(done.stdout).error.string
        raise UnreadableSource(f"infonium cannot read the recording: {failure}")
    done.check_returncode()
    return report.model_validate_json(done.stdout)


# The title a recording carries in the tags of its file, and no words where it carries none.
# An Ogg file carries its tags in the comments of its sound. In a file of another kind, a
# title on the sound names its track, such as "English".
def own_title(file: Path) -> str:
    probe = probed(file, TITLE_ENTRIES, TagProbe)
    commented = probe.streams if probe.format.format_name == OGG else []
    titles = [
        *probe.format.tags.values(),
        *(title for sound in commented for title in sound.tags.values()),
    ]
    return WORD_GAP.join("".join(titles).split())


# Whether a copy starts at zero and runs as long as its packets last.
def has_plain_timeline(copy: CopyProbe) -> bool:
    stream = copy.streams[0]
    usual = median_low(packet.duration for packet in copy.packets)
    longer = stream.duration_ts - stream.nb_frames * usual
    return stream.start_pts == 0 and -TRIMMED_PACKETS * usual <= longer <= usual


def audio_track(file: Path, directory: Path) -> AudioTrack:
    probe = probed(file, SOUND_ENTRIES, SoundProbe)
    if not probe.streams or not probe.packets:
        raise UnreadableSource("no sound in the recording")
    audio = AudioTrack(file=directory / AUDIO_FILE, media_type=AUDIO_MEDIA_TYPE)
    # Named as a file, the place the sound is written to cannot be read as anything else.
    sound = f"{LOCAL_FILE}:{audio.file}"
    if probe.streams[0].codec_name == AAC:
        run_on(file, [*SOUND_ALONE, *COPIED, sound], CONVERSION_LIMIT_SECONDS).check_returncode()
        if has_plain_timeline(probed(audio.file, TIMELINE_ENTRIES, CopyProbe)):
            return audio
    run_on(file, [*SOUND_ALONE, *CODED_ANEW, sound], CONVERSION_LIMIT_SECONDS).check_returncode()
    return audio


def fetch_media(url: str, directory: Path) -> FetchedMedia:
    fetch = Fetch(url=url, deadline=time.monotonic() + MEDIA_FETCH_LIMIT_SECONDS)
    with (
        TemporaryDirectory(dir=directory) as downloads,
        downloader_for(fetch, Path(downloads)) as downloader,
    ):
        found = downloader.extract_info(url, download=False)
        if ENTRIES in found:
            raise UnreadableSource(f"{url} lists recordings; give the address of one of them")
        page = MediaPage.model_validate(found)
        if page.is_live:
            raise UnreadableSource(f"{url} is still being broadcast and has no recording yet")
        if page.duration is not None and page.duration > LONGEST_RECORDING_SECONDS:
            raise UnreadableSource(
                f"{url} runs {page.duration / SECONDS_PER_HOUR:.1f} hours, and infonium fetches "
                f"recordings of up to {LONGEST_RECORDING_SECONDS / SECONDS_PER_HOUR:g} hours"
            )
        parts = [
            part
            for file in usable_captions(page)[:1]
            for part in fetched_parts(downloader, fetch, file)
        ]
        download = Download.model_validate(downloader.process_ie_result(found, download=True))
        audio = audio_track(download.requested_downloads[0].filepath, directory)
    length = probed(audio.file, LENGTH_ENTRIES, LengthProbe).format.duration
    utterances = caption_utterances(parts, length)
    if utterances:
        return CaptionedMedia(
            title=page.title, audio=audio, transcript=Transcript(utterances=utterances)
        )
    return UncaptionedMedia(title=page.title, audio=audio)
