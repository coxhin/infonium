import os
import re
import signal
import subprocess
import sys
import time
import traceback
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, Field, TypeAdapter
from yt_dlp import YoutubeDL
from yt_dlp.extractor import get_info_extractor

from infonium import lineage
from infonium.captions import (
    CaptionFormat,
    CaptionPart,
    caption_fragments,
    caption_utterances,
    fragmented_parts,
    json3_parts,
    vtt_parts,
)
from infonium.document import (
    OVERDUE_SOURCE,
    OVERSIZE_SOURCE,
    Tagged,
    UnreadableSource,
    error_text,
)
from infonium.recording import AAC, AudioTrack, audio_length, audio_track
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
# in one file or in the fragments of a stream, may hold. The slowest site seen sent 120 KiB a
# second, at which three hours of sound take twenty-four minutes. The sizes count what a
# compressed answer holds once it is inflated: yt-dlp reads with requests, which inflates an
# answer piece by piece as it is read, where the standard library's handler inflates it
# whole before any of it is counted.
SILENCE_LIMIT_SECONDS = 20
REQUEST_RETRIES = 3
MEDIA_FETCH_LIMIT_SECONDS = 3600.0
RECORDING_LIMIT_BYTES = 2 * 1024**3
# A caption track is bounded by its bytes. A recording of the longest length that is fetched
# has, in YouTube's automatic captions as YouTube writes them, a track of 4.9 MB, and the
# bound is about twice that. A track of a real layout within the bound is read in about two
# thirds of the fetcher's memory or less. A track made of very many very short parts can
# still need more memory than the fetcher has, and the fetch then ends without a report.
CAPTION_LIMIT_BYTES = 10 * 1024**2
DOWNLOADED_BYTES = "downloaded_bytes"

# yt-dlp waits on a host for the bytes it has asked for, however slowly they come, so no
# check from inside it holds a fetch to a time. A media page is therefore fetched by a
# process of its own, this module run as a program, which is ended at the limit together
# with whatever it has started. It leaves the recording in the folder it is given, and a
# report of the fetch beside it. Python is told the module by the name in its specification:
# run as a program, the module calls itself "__main__".
# The fetcher is given the address of the page and the folder. It is to be started as the
# leader of a session of its own, with a pipe for its standard input that the process which
# starts it holds open and writes nothing to: when the pipe closes, the fetcher ends the
# group it leads. Started with a standard input that is at its end, it ends at once.
FETCHER = [sys.executable, lineage.RUN_AS_MODULE, __spec__.name]
# The fetcher takes, of infonium's environment, the PATH, on which yt-dlp finds ffmpeg, the
# locale, the proxies, which yt-dlp reads as urllib does, and yt-dlp's own settings, such as
# YTDLP_NO_PLUGINS; the ffmpeg it starts is given the same. Driven as here, yt-dlp reads no
# configuration file, and its cache is a folder of the fetch.
FETCHER_TAKES = re.compile(
    "|".join((lineage.SEARCH_PATH, lineage.LOCALE, lineage.PROXIES, r"YTDLP_\w+"))
)
FETCH_REPORT_FILE = "fetch.json"
# How long the process that started the fetcher lets pass between two looks at whether it
# has ended.
FETCHER_WATCH_SECONDS = 0.05
# The most data the fetcher, and each program it starts, may hold in memory: yt-dlp reads a
# page and the manifest of a streamed recording whole, however much a host sends for one.
# Fetching from YouTube and TED the fetcher holds 70 MiB of data and yt-dlp's ffmpeg 46 MiB.
# The bound is on data, not on address space, of which a program reserves many times what
# it uses: 72 MiB for each thread it starts, and a JavaScript runtime, which yt-dlp starts
# for YouTube where one is installed, a gigabyte.
FETCHER_MEMORY_BYTES = 512 * 1024**2
# Memory the fetcher holds from its start and gives up first when a fetch fails: one that
# ran out of memory in small pieces that it still holds has none left to report the ending.
REPORT_ROOM_BYTES = 4 * 1024**2

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
# A caption track cut into the fragments of a stream.
FRAGMENTED = "m3u8_native"


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


class FetchReportKind(StrEnum):
    COMPLETED = "completed"
    ABORTED = "aborted"


class CompletedFetch(Tagged):
    # What the fetch of a media page brings: the title of its recording, the parts of its
    # captions, and the file its recording was downloaded to.
    kind: Literal[FetchReportKind.COMPLETED] = FetchReportKind.COMPLETED
    title: str
    parts: list[CaptionPart]
    recording: Path


class AbortedFetch(Tagged):
    # Why a media page was not fetched, in the words of what ended the fetch: a site or a
    # rule of this module that refused the page, or a fault.
    kind: Literal[FetchReportKind.ABORTED] = FetchReportKind.ABORTED
    reason: str


# What the fetcher tells the process that started it of how the fetch ended.
FetchReport = Annotated[CompletedFetch | AbortedFetch, Field(discriminator="kind")]
FETCH_REPORT = TypeAdapter(FetchReport)


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


def tracks_in(tracks: dict[str, list[CaptionFile]], language: str) -> list[list[CaptionFile]]:
    # The tracks in one language, the one named exactly as it first.
    in_language = re.compile(
        rf"{re.escape(LANGUAGE_NARROWING.split(language)[0])}(?:{LANGUAGE_NARROWING.pattern}.*)?",
        re.IGNORECASE,
    )
    named = [name for name in tracks if in_language.fullmatch(name)]
    return [
        tracks[name]
        for name in sorted(named, key=lambda name: name.casefold() != language.casefold())
    ]


def usable_captions(page: MediaPage) -> list[CaptionFile]:
    # The captions that hold the words as they were spoken, the best first: those people
    # made before the automatic ones, and each track in the form that is read most exactly.
    # A page that does not say which language is spoken has none, since any of its tracks
    # may be a translation.
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
        raise UnreadableSource(OVERSIZE_SOURCE.format(source=page_url))


def downloader_for(url: str, downloads: Path) -> YoutubeDL:
    def hold_recording_to_size(progress: Mapping[str, Any]) -> None:
        # yt-dlp reports how a download goes as it goes, and a report that raises ends it.
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
    if file.protocol == FRAGMENTED:
        fragments = caption_fragments(content.decode(), file.url)
        contents: list[bytes] = []
        for fragment in fragments:
            contents.append(caption_content(downloader, page_url, fragment.url))
            hold_to_size(page_url, sum(map(len, contents)), CAPTION_LIMIT_BYTES)
        return fragmented_parts(fragments, contents)
    if file.ext == CaptionFormat.JSON3:
        return json3_parts(content)
    return vtt_parts(content, counts_from=0.0)


def page_fetch(url: str, downloads: Path) -> CompletedFetch:
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
    return CompletedFetch(
        title=page.title, parts=parts, recording=download.requested_downloads[0].filepath
    )


def end_when_abandoned() -> None:
    # The process that started the fetcher holds the other end of its standard input open
    # and writes nothing to it. When that end closes, that process is gone and no longer
    # holds the fetcher to its time, so the fetcher ends the group it leads: itself and
    # whatever it has started.
    os.read(sys.stdin.fileno(), 1)
    os.killpg(os.getpid(), signal.SIGKILL)


def main() -> None:
    # What this module does when it is run as a program: it fetches the page it is given
    # into the folder it is given, and writes the report. An exception does not cross from
    # one process to another, so whatever ends the fetch is reported in the words it would
    # give a reader had it been raised in the starting process, and where it arose is
    # written to standard error, which is that process's own. A fetch in which Python runs
    # out of the memory it is allowed has met a page that holds more than is fetched, and
    # is reported as one.
    lineage.hold_data_to(FETCHER_MEMORY_BYTES)
    room = bytearray(REPORT_ROOM_BYTES)
    url, downloads = sys.argv[1], Path(sys.argv[2])
    Thread(target=end_when_abandoned, daemon=True).start()
    try:
        report: FetchReport = page_fetch(url, downloads)
    except Exception as ending:
        del room
        traceback.print_exc()
        outgrown = isinstance(ending, MemoryError)
        report = AbortedFetch(
            reason=OVERSIZE_SOURCE.format(source=url) if outgrown else error_text(ending)
        )
    (downloads / FETCH_REPORT_FILE).write_bytes(FETCH_REPORT.dump_json(report))


def reported_fetch(url: str, downloads: Path) -> CompletedFetch:
    # The fetch of a page as the fetcher reports it.
    command = [*FETCHER, url, str(downloads)]
    began = time.monotonic()
    environment = lineage.program_environment(os.environ, FETCHER_TAKES, {})
    with lineage.started_in_own_session(command, environment) as fetcher:
        while fetcher.poll() is None:
            if time.monotonic() - began > MEDIA_FETCH_LIMIT_SECONDS:
                raise UnreadableSource(OVERDUE_SOURCE.format(source=url))
            time.sleep(FETCHER_WATCH_SECONDS)
    # A fetcher that ends with a failing status has written no report.
    if fetcher.returncode != 0:
        raise subprocess.CalledProcessError(fetcher.returncode, command)
    match FETCH_REPORT.validate_json((downloads / FETCH_REPORT_FILE).read_bytes()):
        case CompletedFetch() as fetch:
            return fetch
        case AbortedFetch(reason=reason):
            raise UnreadableSource(reason)


def fetch_media(url: str, directory: Path) -> FetchedMedia:
    with TemporaryDirectory(dir=directory) as downloads:
        fetch = reported_fetch(url, Path(downloads))
        audio = audio_track(fetch.recording, directory)
    utterances = caption_utterances(fetch.parts, audio_length(audio))
    if utterances:
        return CaptionedMedia(
            title=fetch.title, audio=audio, transcript=Transcript(utterances=utterances)
        )
    return UncaptionedMedia(title=fetch.title, audio=audio)


if __name__ == "__main__":
    main()
