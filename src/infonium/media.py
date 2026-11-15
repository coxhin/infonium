import re
import subprocess
import time
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
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
from infonium.document import MediaType, Seconds, Tagged, UnreadableSource
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
# The sound of a file with several is its first.
FIRST_SOUND = "a:0"
FFPROBE = ["ffprobe", "-v", "error", "-of", "json", "-select_streams", FIRST_SOUND, "-show_entries"]
SOUND_ENTRIES = "stream=codec_name,start_time:format=start_time"
LENGTH_ENTRIES = "format=duration"
FFMPEG = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i"]
# The sound is written alone: without the chapters of its file, which would be a stream of
# their own, and with the index of samples before the sound, so that a player need not read
# to the end of the file before it can play or seek.
SOUND_ALONE = ["-map", f"0:{FIRST_SOUND}", "-map_chapters", "-1", "-movflags", "+faststart"]
COPIED = ["-c:a", "copy"]
# Sound that is coded anew begins when its presentation does: silence stands before a sound
# that starts late, and in any hole in it.
CODED_ANEW = ["-c:a", AAC, "-af", "aresample=async=1:first_pts=0"]
# Sound that starts this long after its presentation cannot be copied: with nothing before
# it, it would play that much early.
LATE_START_SECONDS = 0.1
# The longest ffprobe may take over a file, and ffmpeg over writing its sound.
PROBE_LIMIT_SECONDS = 60.0
CONVERSION_LIMIT_SECONDS = 1800.0

# The bounds of the fetch of a media page. A recording may run this long, which lets a
# podcast of three hours through with room to spare.
SECONDS_PER_HOUR = 3600
LONGEST_RECORDING_SECONDS = 4.0 * SECONDS_PER_HOUR
# The longest silence a site is allowed, how often a request that broke off is made again,
# the longest the fetch may take in all, and the most the recording and one caption file may
# hold. The slowest site seen sent 120 KiB a second, at which three hours of sound take
# twenty-four minutes.
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
LANGUAGE_SUBTAGS = re.compile(r"[-_]")
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


class SoundStream(BaseModel):
    # A file that does not say when a stream or its presentation starts is taken to start
    # both together.
    codec_name: str
    start_time: float = 0.0


class Presentation(BaseModel):
    start_time: float = 0.0


class SoundProbe(BaseModel):
    streams: list[SoundStream]
    format: Presentation


class FileLength(BaseModel):
    duration: Seconds


class LengthProbe(BaseModel):
    format: FileLength


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
        rf"{re.escape(LANGUAGE_SUBTAGS.split(language)[0])}(?:{LANGUAGE_SUBTAGS.pattern}.*)?",
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
        contents = [caption_content(downloader, fetch, segment.url) for segment in segments]
        return segmented_parts(segments, contents)
    if file.ext == CaptionFormat.JSON3:
        return json3_parts(content)
    return vtt_parts(content, counts_from=0.0)


def probed[Report: BaseModel](file: Path, entries: str, report: type[Report]) -> Report:
    told = subprocess.run(
        [*FFPROBE, entries, str(file)],
        check=True,
        capture_output=True,
        timeout=PROBE_LIMIT_SECONDS,
    )
    return report.model_validate_json(told.stdout)


def audio_track(file: Path, directory: Path) -> AudioTrack:
    probe = probed(file, SOUND_ENTRIES, SoundProbe)
    if not probe.streams:
        raise UnreadableSource(f"no sound in {file.name}")
    first = probe.streams[0]
    on_time = first.start_time - probe.format.start_time < LATE_START_SECONDS
    coding = COPIED if first.codec_name == AAC and on_time else CODED_ANEW
    sound = directory / AUDIO_FILE
    subprocess.run(
        [*FFMPEG, str(file), *SOUND_ALONE, *coding, str(sound)],
        check=True,
        capture_output=True,
        timeout=CONVERSION_LIMIT_SECONDS,
    )
    return AudioTrack(file=sound, media_type=AUDIO_MEDIA_TYPE)


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
