import logging
import mimetypes
import re
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from email.message import Message
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

import filetype
import httpx
from pydantic import BaseModel

from infonium import webpage
from infonium.document import (
    HTML_MEDIA_TYPE,
    MEDIA_TYPE_PATTERN,
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
OPENING_BYTES = 1024

logger = logging.getLogger(__name__)


class Source(BaseModel):
    # The bytes of a source on disk, what kind of bytes they are, and what they go by: the
    # address they were fetched from, or the name of the file that was uploaded.
    file: Path
    media_type: MediaType
    reference: str


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
    # What the bytes themselves say wins over what was declared. A web page cannot be told
    # by its bytes: a file is one when it is declared one, or when nothing is declared and
    # it opens like one.
    recognised = filetype.guess(file)
    if recognised is not None:
        return str(recognised.mime)
    named = declared_media_type(declared)
    with file.open("rb") as content:
        opening = content.read(OPENING_BYTES).lstrip().lower()
    is_page = named in PAGE_MEDIA_TYPES or (
        named == UNKNOWN_MEDIA_TYPE and opening.startswith(PAGE_OPENINGS)
    )
    return HTML_MEDIA_TYPE if is_page else named


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


def readout_of(source: Source) -> Readout:
    if source.media_type == HTML_MEDIA_TYPE:
        return Readout(
            extractor=webpage.EXTRACTOR,
            extracted=webpage.extract_page(source.file.read_bytes(), source.reference),
            file=source.file,
            media_type=source.media_type,
        )
    raise UnreadableSource(f"infonium cannot read {source.media_type}: {source.reference}")


class Intake:
    # Reads the sources of a library's items into documents, a few at a time and off the
    # thread that asks.
    def __init__(self, store: LibraryStore, readers: int) -> None:
        self.store = store
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
                    source = fetched(url, Path(directory))
                case Upload(name=name):
                    source = uploaded(self.store.upload(item_id), name)
            readout = readout_of(source)
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
            reason = str(error) or type(error).__name__
            self.changed(owner, item_id, partial(reading_failed, reason=reason))

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
