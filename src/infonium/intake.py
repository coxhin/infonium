import logging
import mimetypes
import re
import subprocess
import sys
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from email.message import Message
from functools import partial
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.parse import urlsplit

import filetype
import httpx
import openai
from pydantic import BaseModel

from infonium import captions, media, pdf, recording, speech, webpage
from infonium.document import (
    HTML_MEDIA_TYPE,
    MEDIA_TYPE_PATTERN,
    PDF_MEDIA_TYPE,
    WORD_GAP,
    Extracted,
    MediaType,
    UnreadableSource,
    document_of,
)
from infonium.library import (
    FIRST_READING,
    Item,
    LibraryStore,
    Link,
    Origin,
    Reading,
    Upload,
    is_underway,
    item_identity,
    item_with,
    reading_begun,
    reading_failed,
    reading_settled,
    reading_started,
)
from infonium.transcript import Transcript, transcript_blocks

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36 infonium/0.1"
)
# The longest silence a server is allowed while a source is fetched from it.
FETCH_TIMEOUT_SECONDS = 20.0
# The longest a fetch may take in all, and the most it may bring.
FETCH_LIMIT_SECONDS = 600.0
FETCH_LIMIT_BYTES = 2 * 1024**3
FETCHED_FILE = "source"
CONTENT_TYPE = "content-type"
UNDECLARED = ""
# What bytes are taken for when nothing tells their kind.
UNKNOWN_MEDIA_TYPE = "application/octet-stream"
PAGE_MEDIA_TYPES = frozenset({HTML_MEDIA_TYPE, "application/xhtml+xml"})
PAGE_OPENINGS = (b"<!doctype html", b"<html")
# The kinds of file that hold a recording: sound alone, or sound with a picture.
RECORDING_MEDIA_TYPES = ("audio/", "video/")
OPENING_BYTES = 1024
# The most that is kept of the reason a reading failed, and what stands for the rest.
REASON_LIMIT_CHARACTERS = 500
CUT_SHORT = "\N{HORIZONTAL ELLIPSIS}"

logger = logging.getLogger(__name__)


class Source(BaseModel):
    # The bytes of a source on disk, what kind of bytes they are, and what they go by: the
    # address they were fetched from, or the name of the file that was uploaded.
    file: Path
    media_type: MediaType
    reference: str


class Engines(BaseModel):
    # Where the programs and services that read sources other than web pages are found.
    pdf: Path
    recogniser: speech.Recogniser


class Readout(BaseModel):
    # What reading a source gives: its text, and the file kept for a reader to show beside
    # the text, which for a recording is not the file that came in.
    extractor: str
    extracted: Extracted
    file: Path
    media_type: MediaType


def declared_media_type(declared: str) -> str:
    # The kind a server or a file name gives, without its parameters; a kind that is not
    # given, or not written as a media type, tells nothing.
    header = Message()
    header[CONTENT_TYPE] = declared
    named = header.get_content_type() if declared else UNKNOWN_MEDIA_TYPE
    return named if re.fullmatch(MEDIA_TYPE_PATTERN, named) else UNKNOWN_MEDIA_TYPE


def media_type_of(file: Path, declared: str) -> str:
    # Only the bytes themselves tell what a file is: a name or a server may call anything a
    # recording, and a program that plays one would then follow whatever the file says. A
    # web page is the one kind bytes cannot tell: a file is one when it is declared one, or
    # when nothing is declared and it opens like one.
    recognised = filetype.guess(file)
    if recognised is not None:
        return str(recognised.mime)
    named = declared_media_type(declared)
    with file.open("rb") as content:
        opening = content.read(OPENING_BYTES).lstrip().lower()
    is_page = named in PAGE_MEDIA_TYPES or (
        named == UNKNOWN_MEDIA_TYPE and opening.startswith(PAGE_OPENINGS)
    )
    return HTML_MEDIA_TYPE if is_page else UNKNOWN_MEDIA_TYPE


def fetched(url: str, directory: Path) -> Source:
    file = directory / FETCHED_FILE
    began = time.monotonic()
    with httpx.stream(
        "GET",
        url,
        follow_redirects=True,
        timeout=FETCH_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    ) as response:
        response.raise_for_status()
        with file.open("wb") as kept:
            # Bytes are taken as they arrive, so that a source that only trickles is still
            # held to the time a fetch may take.
            for chunk in response.iter_bytes():
                kept.write(chunk)
                if kept.tell() > FETCH_LIMIT_BYTES:
                    raise UnreadableSource(f"{url} holds more than infonium fetches")
                if time.monotonic() - began > FETCH_LIMIT_SECONDS:
                    raise UnreadableSource(f"{url} takes longer than infonium waits")
        declared = response.headers.get(CONTENT_TYPE, UNDECLARED)
        reference = str(response.url.copy_with(fragment=None))
    return Source(file=file, media_type=media_type_of(file, declared), reference=reference)


def uploaded(file: Path, name: str) -> Source:
    declared = mimetypes.guess_type(name)[0] or UNDECLARED
    return Source(file=file, media_type=media_type_of(file, declared), reference=name)


def spoken_readout(
    extractor: str, title: str, audio: recording.AudioTrack, transcript: Transcript
) -> Readout:
    blocks = transcript_blocks(transcript)
    if not blocks:
        raise UnreadableSource(f"no speech in {title}")
    return Readout(
        extractor=extractor,
        extracted=Extracted(title=title, blocks=blocks),
        file=audio.file,
        media_type=audio.media_type,
    )


def recognised_readout(
    title: str, audio: recording.AudioTrack, recogniser: speech.Recogniser
) -> Readout:
    # The words are recognised in the sound that is kept, so that their times are times of
    # the recording a reader plays.
    transcript = speech.transcribe(audio, recogniser)
    return spoken_readout(speech.EXTRACTOR, title, audio, transcript)


def media_page_readout(url: str, directory: Path, engines: Engines) -> Readout:
    match media.fetch_media(url, directory):
        case media.CaptionedMedia(title=title, audio=audio, transcript=transcript):
            return spoken_readout(captions.EXTRACTOR, title, audio, transcript)
        case media.UncaptionedMedia(title=title, audio=audio):
            return recognised_readout(title, audio, engines.recogniser)


def link_readout(url: str, directory: Path, engines: Engines) -> Readout:
    if media.is_media_page(url):
        return media_page_readout(url, directory, engines)
    source = fetched(url, directory)
    # An address may lead on to a media page, as a short link does.
    if media.is_media_page(source.reference):
        return media_page_readout(source.reference, directory, engines)
    return readout_of(source, directory, engines)


def title_of(reference: str) -> str:
    # What a source with no title of its own goes by: the last name in its address or its
    # file name, without the suffix.
    return PurePosixPath(urlsplit(reference).path).stem or reference


def readout_of(source: Source, directory: Path, engines: Engines) -> Readout:
    if source.media_type == HTML_MEDIA_TYPE:
        return Readout(
            extractor=webpage.EXTRACTOR,
            extracted=webpage.extract_page(source.file.read_bytes(), source.reference),
            file=source.file,
            media_type=source.media_type,
        )
    if source.media_type == PDF_MEDIA_TYPE:
        return Readout(
            extractor=pdf.EXTRACTOR,
            extracted=pdf.extract_pdf(
                source.file, title_of(source.reference), engines.pdf, directory
            ),
            file=source.file,
            media_type=source.media_type,
        )
    if source.media_type.startswith(RECORDING_MEDIA_TYPES):
        return recognised_readout(
            recording.own_title(source.file) or title_of(source.reference),
            recording.audio_track(source.file, directory),
            engines.recogniser,
        )
    raise UnreadableSource(f"infonium cannot read {source.reference}: {source.media_type}")


def asked_address(error: openai.APIError) -> str:
    # Where a request went, without the parts of an address that may hold a secret.
    return str(error.request.url.copy_with(userinfo=b"", query=None))


def program_name(command: object) -> str:
    # The program a command line starts, by its name alone: the rest of the line names files
    # of the work folder, which say nothing to a person. Where Python runs a module, the
    # module is the program.
    match command:
        case [sys.executable, media.RUN_AS_MODULE, module, *_]:
            return str(module)
        case [first, *_]:
            return PurePosixPath(str(first)).name
        case _:
            return PurePosixPath(str(command)).name


def failure_reason(error: BaseException) -> str:
    # An error's own text is the reason, but for the kinds that leave the reason out of it.
    # A program that fails says why in what it writes, not in the error that reports how it
    # exited, so its last written line is the reason, without the mark ffmpeg begins a line
    # with; a program that ended without a word, or ran out of time, is named with how it
    # ended. A request that a service refused does not carry what the service answered, and
    # one that no service answered does not say which service that was, so the reason gives
    # the address and the answer. A reason is kept as one line of a length a person reads: a
    # service may answer with a whole page.
    if isinstance(error, subprocess.CalledProcessError) and error.stderr and error.stderr.strip():
        written = error.stderr if isinstance(error.stderr, str) else error.stderr.decode()
        said = recording.COMPLAINER.sub("", written.strip().splitlines()[-1])
    elif isinstance(error, subprocess.CalledProcessError):
        said = f"{program_name(error.cmd)} ended with status {error.returncode}"
    elif isinstance(error, subprocess.TimeoutExpired):
        said = f"{program_name(error.cmd)} took longer than {error.timeout:g} seconds"
    elif isinstance(error, openai.APIStatusError):
        said = f"{asked_address(error)} answered {error.status_code}: {error.response.text}"
    elif isinstance(error, openai.APIConnectionError):
        said = f"{asked_address(error)} did not answer: {error}"
    else:
        said = str(error) or type(error).__name__
    line = WORD_GAP.join(said.split())
    cut = len(line) > REASON_LIMIT_CHARACTERS
    return f"{line[:REASON_LIMIT_CHARACTERS]}{CUT_SHORT}" if cut else line


class Intake:
    # Reads the sources of a library's items into documents, a few at a time and off the
    # thread that asks.
    def __init__(self, store: LibraryStore, readers: int, engines: Engines) -> None:
        self.store = store
        self.engines = engines
        self.pool = ThreadPoolExecutor(max_workers=readers, thread_name_prefix="reader")
        # A change to an item's reading, and to the waiting copy of its upload, is made by
        # one thread at a time. So an item that is being read is not taken for reading a
        # second time, and a copy is not taken away from under a reading.
        self.turn = Lock()

    def begin_link(self, owner: str, url: str) -> Item:
        item_id = item_identity(owner, url)
        with self.turn:
            underway = self.underway(owner, item_id)
            item = underway[0] if underway else self.started(owner, item_id, Link(url=url))
        if not underway:
            self.read_in_background(owner, item_id)
        return item

    def begin_upload(self, owner: str, name: str, arrived: Path) -> Item:
        item_id = self.store.upload_identity(owner, arrived)
        with self.turn:
            underway = self.underway(owner, item_id)
            if underway:
                # The same bytes are waiting already for the reading that is under way.
                arrived.unlink()
                item = underway[0]
            else:
                arrived.replace(self.store.upload(item_id))
                item = self.started(owner, item_id, Upload(name=name))
        if not underway:
            self.read_in_background(owner, item_id)
        return item

    def underway(self, owner: str, item_id: str) -> list[Item]:
        # The item when a reading of it is under way, and nothing otherwise.
        with self.store.library(owner) as library:
            return [item for item in library.sought(item_id) if is_underway(item.reading)]

    def started(self, owner: str, item_id: str, origin: Origin) -> Item:
        with self.store.library(owner) as library:
            earlier = library.sought(item_id)
            item = Item(
                id=item_id,
                origin=origin,
                added=earlier[0].added if earlier else datetime.now(UTC),
                reading=reading_started(earlier[0].reading) if earlier else FIRST_READING,
            )
            library.keep_item(item)
        return item

    def resume(self) -> None:
        # What the service's last stopping left: work in progress goes, readings that were
        # under way wait their turn again, and no other upload is kept waiting.
        self.store.clear_work()
        underway = self.store.underway()
        self.store.clear_uploads({reading.item_id for reading in underway})
        for reading in underway:
            self.read_in_background(reading.owner, reading.item_id)

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    def read_in_background(self, owner: str, item_id: str) -> None:
        finished = self.pool.submit(self.read, owner, item_id)
        finished.add_done_callback(partial(self.record, owner, item_id))

    def read(self, owner: str, item_id: str) -> None:
        item = self.changed(owner, item_id, reading_begun)
        if not is_underway(item.reading):
            return
        with TemporaryDirectory(dir=self.store.work) as directory:
            match item.origin:
                case Link(url=url):
                    readout = link_readout(url, Path(directory), self.engines)
                case Upload(name=name):
                    source = uploaded(self.store.upload(item_id), name)
                    readout = readout_of(source, Path(directory), self.engines)
            document = document_of(
                item_id=item.id,
                extractor=readout.extractor,
                snapshot=self.store.keep_snapshot(readout.file),
                media_type=readout.media_type,
                blocks=readout.extracted.blocks,
            )
        with self.turn:
            with self.store.library(owner) as library:
                settled = reading_settled(item.reading, readout.extracted.title, document.id)
                library.keep_document(document, datetime.now(UTC))
                library.keep_item(item_with(item, settled))
            self.drop_upload(item)

    def record(self, owner: str, item_id: str, finished: Future[None]) -> None:
        error = None if finished.cancelled() else finished.exception()
        if error is not None:
            logger.error("reading item %s failed", item_id, exc_info=error)
            self.changed(owner, item_id, partial(reading_failed, reason=failure_reason(error)))

    def changed(self, owner: str, item_id: str, change: Callable[[Reading], Reading]) -> Item:
        # Puts an item's reading through one change. A reading that ends with it has no more
        # use for the waiting copy of its upload.
        with self.turn:
            with self.store.library(owner) as library:
                item = library.item(item_id)
                changed = item_with(item, change(item.reading))
                library.keep_item(changed)
            if not is_underway(changed.reading):
                self.drop_upload(changed)
        return changed

    def drop_upload(self, item: Item) -> None:
        if isinstance(item.origin, Upload):
            self.store.upload(item.id).unlink()
