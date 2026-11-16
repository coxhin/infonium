import os
import re
import signal
import subprocess
import sys
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL
from yt_dlp.extractor import get_info_extractor
from yt_dlp.utils import YoutubeDLError

from infonium.captions import (
    CaptionFormat,
    CaptionPart,
    caption_segments,
    caption_utterances,
    json3_parts,
    segmented_parts,
    vtt_parts,
)
from infonium.document import Tagged, UnreadableSource
from infonium.recording import AAC, LENGTH_ENTRIES, AudioTrack, LengthProbe, audio_track, probed
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

# The bounds of the fetch of a media page. A recording may run this long, which lets a
# podcast of three hours through with room to spare.
SECONDS_PER_HOUR = 3600
LONGEST_RECORDING_SECONDS = 4.0 * SECONDS_PER_HOUR
# The longest silence a site is allowed, how often a request that broke off is made again,
# the longest the fetch may take in all, and the most the recording and its caption track,
# in one file or in the segments of a stream, may hold. The slowest site seen sent 120 KiB a
# second, at which three hours of sound take twenty-four minutes. The sizes count what a
# compressed answer holds once it is inflated: yt-dlp reads with requests, which inflates an
# answer piece by piece as it is read, where the standard library's handler inflates it
# whole before any of it is counted.
SILENCE_LIMIT_SECONDS = 20
REQUEST_RETRIES = 3
MEDIA_FETCH_LIMIT_SECONDS = 3600.0
RECORDING_LIMIT_BYTES = 2 * 1024**3
CAPTION_LIMIT_BYTES = 16 * 1024**2
DOWNLOADED_BYTES = "downloaded_bytes"

# yt-dlp waits on a host for the bytes it has asked for, however slowly they come, so no
# check from inside it holds a fetch to a time. A media page is therefore fetched by a
# process of its own, this module run as a program, which is ended at the limit together
# with whatever it has started. It leaves the recording in the folder it is given, and a
# report of the fetch beside it.
FETCHER = [sys.executable, "-m", __spec__.name]
FETCH_REPORT = "fetch.json"
# The fetcher ends with this status, the usual one for an input that cannot be read, when
# the page is refused; its report then says why.
REFUSED_STATUS = os.EX_NOINPUT

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


class PageFetch(BaseModel):
    # What the fetch of a media page brings: the title of its recording, the parts of its
    # captions, and the file its recording was downloaded to.
    title: str
    parts: list[CaptionPart]
    recording: Path


class Refusal(BaseModel):
    # Why a media page was not fetched, in the words of whoever refused it.
    reason: str


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


def hold_to_size(page_url: str, brought: int, limit: int) -> None:
    if brought > limit:
        raise UnreadableSource(f"{page_url} holds more than infonium fetches")


def downloader_for(url: str, downloads: Path) -> YoutubeDL:
    # yt-dlp reports how a download goes as it goes, and a report that raises ends it.
    def hold_recording_to_size(progress: Mapping[str, Any]) -> None:
        hold_to_size(url, progress.get(DOWNLOADED_BYTES) or 0, RECORDING_LIMIT_BYTES)

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
            "progress_hooks": [hold_recording_to_size],
            "color": {"stderr": UNCOLOURED, "stdout": UNCOLOURED},
            "quiet": True,
            "noprogress": True,
        }
    )


def caption_content(downloader: YoutubeDL, page_url: str, url: str) -> bytes:
    with downloader.urlopen(url) as answer:
        content = answer.read(CAPTION_LIMIT_BYTES + 1)
    hold_to_size(page_url, len(content), CAPTION_LIMIT_BYTES)
    return content


def fetched_parts(downloader: YoutubeDL, page_url: str, file: CaptionFile) -> list[CaptionPart]:
    content = caption_content(downloader, page_url, file.url)
    if file.protocol == SEGMENTED:
        segments = caption_segments(content.decode(), file.url)
        contents: list[bytes] = []
        for segment in segments:
            contents.append(caption_content(downloader, page_url, segment.url))
            hold_to_size(page_url, sum(map(len, contents)), CAPTION_LIMIT_BYTES)
        return segmented_parts(segments, contents)
    if file.ext == CaptionFormat.JSON3:
        return json3_parts(content)
    return vtt_parts(content, counts_from=0.0)


def page_fetch(url: str, downloads: Path) -> PageFetch:
    with downloader_for(url, downloads) as downloader:
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
            for part in fetched_parts(downloader, url, file)
        ]
        download = Download.model_validate(downloader.process_ie_result(found, download=True))
    return PageFetch(
        title=page.title, parts=parts, recording=download.requested_downloads[0].filepath
    )


# The process that started the fetcher holds the other end of its standard input open and
# writes nothing to it. When that end closes, that process is gone and no longer holds the
# fetcher to its time, so the fetcher ends the group it leads: itself and whatever it has
# started.
def end_when_abandoned() -> None:
    os.read(sys.stdin.fileno(), 1)
    os.killpg(os.getpid(), signal.SIGKILL)


# What this module does when it is run as a program: it fetches the page it is given into
# the folder it is given, and writes the report. An exception does not cross from one
# process to another, so a page that is refused, by its site or by a rule of this module, is
# reported with the reason, and the status says that it was.
def main() -> int:
    url, downloads = sys.argv[1], Path(sys.argv[2])
    Thread(target=end_when_abandoned, daemon=True).start()
    report = downloads / FETCH_REPORT
    try:
        fetch = page_fetch(url, downloads)
    except (UnreadableSource, YoutubeDLError) as refusal:
        report.write_bytes(Refusal(reason=str(refusal)).model_dump_json().encode())
        return REFUSED_STATUS
    report.write_bytes(fetch.model_dump_json().encode())
    return os.EX_OK


# The fetch of a page as the fetcher reports it.
def reported_fetch(url: str, downloads: Path) -> PageFetch:
    command = [*FETCHER, url, str(downloads)]
    # The fetcher leads a session of its own, so that all it has started is ended with it,
    # and its standard input is a pipe that closes when this process is gone.
    with subprocess.Popen(command, stdin=subprocess.PIPE, start_new_session=True) as fetcher:
        try:
            fetcher.wait(timeout=MEDIA_FETCH_LIMIT_SECONDS)
        finally:
            if fetcher.returncode is None:
                os.killpg(fetcher.pid, signal.SIGKILL)
    report = downloads / FETCH_REPORT
    if fetcher.returncode == REFUSED_STATUS:
        raise UnreadableSource(Refusal.model_validate_json(report.read_bytes()).reason)
    subprocess.CompletedProcess(command, fetcher.returncode).check_returncode()
    return PageFetch.model_validate_json(report.read_bytes())


def fetch_media(url: str, directory: Path) -> FetchedMedia:
    with TemporaryDirectory(dir=directory) as downloads:
        fetch = reported_fetch(url, Path(downloads))
        audio = audio_track(fetch.recording, directory)
    length = probed(audio.file, LENGTH_ENTRIES, LengthProbe).format.duration
    utterances = caption_utterances(fetch.parts, length)
    if utterances:
        return CaptionedMedia(
            title=fetch.title, audio=audio, transcript=Transcript(utterances=utterances)
        )
    return UncaptionedMedia(title=fetch.title, audio=audio)


if __name__ == "__main__":
    sys.exit(main())
