import logging
import mimetypes
import re
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from email.message import Message
from functools import cached_property, partial
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Lock
from urllib.parse import urlsplit

import filetype
import httpx
from pydantic import BaseModel

from infonium import browser, captions, guard, media, pdf, recording, speech, webpage
from infonium.document import (
    BROKEN_OFF_SOURCE,
    HTML_MEDIA_TYPE,
    LITTLE_TEXT_WORDS,
    MEDIA_TYPE_PATTERN,
    OVERDUE_SOURCE,
    OVERSIZE_SOURCE,
    PDF_MEDIA_TYPE,
    Extracted,
    MediaType,
    UnreadableSource,
    document_of,
    error_status,
    turned_away,
    word_count,
)
from infonium.library import (
    FIRST_READING,
    Item,
    KeptSource,
    LibraryStore,
    Link,
    Origin,
    Read,
    Reading,
    Unread,
    Upload,
    arrival_of,
    is_underway,
    item_identity,
    item_with,
    reading_arrived,
    reading_begun,
    reading_failed,
    reading_settled,
    reading_started,
)
from infonium.reasons import failure_reason
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
# The kinds of source a reader can be shown as they arrived, before anything is read from them.
SHOWN_AS_ARRIVED = frozenset({PDF_MEDIA_TYPE})
OPENING_BYTES = 1024
# What a reader is told who asks to read again an upload that no reading has read.
NOTHING_KEPT = (
    "infonium keeps nothing of {name} to read again, since no reading of it succeeded;"
    " upload the file again"
)

logger = logging.getLogger(__name__)


class NothingToReadAgain(Exception):
    # An upload is in the library, but no file of it is, since no reading of it succeeded.
    pass


class Source(BaseModel):
    # The bytes of a source on disk, what kind of bytes they are, and what they go by: the
    # address they were fetched from, or the name of the file that was uploaded.
    file: Path
    media_type: MediaType
    reference: str


class Engines(BaseModel):
    # Where the programs and services that read sources are found: the browser renders a web
    # page that scripts build.
    pdf: Path
    recogniser: speech.Recogniser
    browser: Path


class PageCopy(BaseModel):
    # A copy of a page read from an address: its file, the address that served it, and what
    # the extractor found in it.
    file: Path
    served: str
    text: webpage.PageText

    @cached_property
    def words(self) -> int:
        return word_count(self.text.blocks)


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
        if not response.is_success:
            raise UnreadableSource(error_status(str(response.url), response.status_code))
        if guard.is_guarded(response.headers):
            raise UnreadableSource(turned_away(url))
        with file.open("wb") as kept:
            # Bytes are taken as they arrive, so that a source that only trickles is still
            # held to the time a fetch may take.
            for chunk in response.iter_bytes():
                kept.write(chunk)
                if kept.tell() > FETCH_LIMIT_BYTES:
                    raise UnreadableSource(OVERSIZE_SOURCE.format(source=url))
                if time.monotonic() - began > FETCH_LIMIT_SECONDS:
                    raise UnreadableSource(OVERDUE_SOURCE.format(source=url))
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


def page_copy(asked: str, file: Path, served: str) -> PageCopy:
    # A page read from an address is first asked whether it is the page asked for or one that
    # a guard answered with in its place.
    content = file.read_bytes()
    copy = PageCopy(file=file, served=served, text=webpage.page_text(content, served))
    if guard.is_check_page(
        guard.ServedPage(asked=asked, served=served, content=content, words=copy.words)
    ):
        raise UnreadableSource(turned_away(asked))
    return copy


def copy_readout(copy: PageCopy) -> Readout:
    return Readout(
        extractor=webpage.EXTRACTOR,
        extracted=webpage.extracted_text(copy.text, copy.served),
        file=copy.file,
        media_type=HTML_MEDIA_TYPE,
    )


def page_readout(url: str, source: Source, directory: Path, engines: Engines) -> Readout:
    # A page that holds little text may be one that its scripts build, and is rendered in a
    # browser and read again. What a rendering reads is kept when it holds more. A check that
    # the site answers the browser with is a rendering that failed, as an error status is.
    # Where changes of this machine's network broke off the attempt that was kept, what is read
    # may be only part of the page, which is logged; where nothing is left to read, those
    # changes are the reason.
    plain = page_copy(url, source.file, source.reference)
    if plain.words >= LITTLE_TEXT_WORDS:
        return copy_readout(plain)
    try:
        rendered = browser.rendered_page(source.reference, directory, engines.browser)
        if len(rendered.broken_off) > 1:
            logger.info(
                "rendering %s took %d attempts, %d of them broken off by changes of this "
                "machine's network",
                source.reference,
                len(rendered.broken_off),
                sum(rendered.broken_off),
            )
        built = page_copy(url, rendered.file, rendered.address)
    except UnreadableSource as failure:
        if not plain.text.blocks:
            raise
        logger.warning(
            "rendering %s failed: %s; its plain reading is kept", source.reference, failure
        )
        return copy_readout(plain)
    kept = built if built.words > plain.words else plain
    if rendered.broken_off_by_network:
        if not kept.text.blocks:
            raise UnreadableSource(BROKEN_OFF_SOURCE.format(source=source.reference))
        logger.warning(
            "rendering %s was broken off by changes of this machine's network; its reading may "
            "hold only part of the page",
            source.reference,
        )
    return copy_readout(kept)


def link_readout(
    url: str, directory: Path, engines: Engines, arrived: Callable[[Source], None]
) -> Readout:
    # A file fetched from the address is handed to arrived before it is read; the recording a
    # media page leads to is not.
    if media.is_media_page(url):
        return media_page_readout(url, directory, engines)
    source = fetched(url, directory)
    arrived(source)
    # An address may lead on to a media page, as a short link does.
    if media.is_media_page(source.reference):
        return media_page_readout(source.reference, directory, engines)
    if source.media_type == HTML_MEDIA_TYPE:
        return page_readout(url, source, directory, engines)
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
        return self.begin_waiting(
            owner, self.store.upload_identity(owner, arrived), Upload(name=name), arrived
        )

    def begin_again(self, owner: str, item_id: str) -> Item:
        # A link is fetched again. An upload is read again from the file kept beside the
        # document the item holds, under its name: for a recording, the sound that was kept.
        with self.store.library(owner) as library:
            item = library.item(item_id)
            kept = [library.kept_source(item_id)] if isinstance(item.reading, Read) else []
        if is_underway(item.reading):
            return item
        match item.origin:
            case Link(url=url):
                return self.begin_link(owner, url)
            case Upload(name=name) if not kept:
                # The file of an upload is kept only beside a document read from it.
                raise NothingToReadAgain(NOTHING_KEPT.format(name=name))
            case Upload():
                with self.store.snapshot(kept[0].snapshot).open("rb") as content:
                    arrived = self.store.staged(content)
                return self.begin_waiting(owner, item_id, item.origin, arrived)

    def begin_waiting(self, owner: str, item_id: str, origin: Upload, arrived: Path) -> Item:
        # A reading of a file that has arrived; where one of the item is under way already, that
        # one goes on, and the file is not wanted.
        with self.turn:
            underway = self.underway(owner, item_id)
            if underway:
                arrived.unlink()
                item = underway[0]
            else:
                arrived.replace(self.store.upload(item_id))
                item = self.started(owner, item_id, origin)
        if not underway:
            # The reading starts whether or not the file is kept as it arrived.
            try:
                self.keep_arrival(owner, item_id, uploaded(self.store.upload(item_id), origin.name))
            finally:
                self.read_in_background(owner, item_id)
        return item

    def keep_arrival(self, owner: str, item_id: str, source: Source) -> None:
        # The source of a first reading, where a reader can be shown it as it is, is kept as it
        # arrived while it is read. A reading that ended meanwhile keeps nothing.
        if source.media_type not in SHOWN_AS_ARRIVED:
            return
        staged = self.store.staged_snapshot(source.file)
        arrived = KeptSource(snapshot=staged.name, media_type=source.media_type)
        with self.turn:
            with self.store.library(owner) as library:
                item = library.item(item_id)
                first = isinstance(item.reading, Unread) and is_underway(item.reading)
                if first:
                    self.store.keep_snapshot(staged)
                    library.keep_item(item_with(item, reading_arrived(item.reading, arrived)))
            if first:
                self.let_go(item.reading, [arrived.snapshot])
            else:
                staged.file.unlink()

    def let_go(self, earlier: Reading, kept: list[str]) -> None:
        # The file a reading kept of its source as it arrived, once a change of the reading
        # keeps it no longer.
        for arrived in arrival_of(earlier):
            if arrived.snapshot not in kept:
                self.store.release_arrival(arrived)

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
                    readout = link_readout(
                        url,
                        Path(directory),
                        self.engines,
                        partial(self.keep_arrival, owner, item_id),
                    )
                case Upload(name=name):
                    source = uploaded(self.store.upload(item_id), name)
                    readout = readout_of(source, Path(directory), self.engines)
            staged = self.store.staged_snapshot(readout.file)
        document = document_of(
            item_id=item.id,
            extractor=readout.extractor,
            snapshot=staged.name,
            media_type=readout.media_type,
            blocks=readout.extracted.blocks,
        )
        # A reading that gives a document the library keeps already leaves that document and
        # its file as they are, and keeps no other file beside it.
        with self.turn:
            with self.store.library(owner) as library:
                current = library.item(item_id)
                settled = reading_settled(current.reading, readout.extracted.title, document.id)
                new_document = library.keep_document(document, datetime.now(UTC))
                if new_document:
                    self.store.keep_snapshot(staged)
                else:
                    staged.file.unlink()
                library.keep_item(item_with(current, settled))
            self.drop_upload(current)
            self.let_go(current.reading, [staged.name] if new_document else [])

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
            self.let_go(item.reading, [arrived.snapshot for arrived in arrival_of(changed.reading)])
        return changed

    def drop_upload(self, item: Item) -> None:
        if isinstance(item.origin, Upload):
            self.store.upload(item.id).unlink()
