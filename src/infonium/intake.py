import logging
import mimetypes
import time
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
    Read,
    Settled,
    Upload,
    is_underway,
    item_with,
    reading_failed,
    reading_resumed,
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


def media_type_of(file: Path, declared: str) -> str:
    # What the bytes themselves say wins over what a server or a file name declared. A web
    # page cannot be told by its bytes, so a file is one when it is declared one or opens
    # like one.
    recognised = filetype.guess(file)
    if recognised is not None:
        return str(recognised.mime)
    header = Message()
    header[CONTENT_TYPE] = declared
    with file.open("rb") as content:
        opening = content.read(OPENING_BYTES).lower()
    is_page = header.get_content_type() in PAGE_MEDIA_TYPES or any(
        mark in opening for mark in PAGE_OPENINGS
    )
    return HTML_MEDIA_TYPE if is_page else header.get_content_type()


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
        # A change to an item's reading is made by one thread at a time, so an item that is
        # being read is not taken for reading a second time.
        self.turn = Lock()

    def begin(self, owner: str, item_id: str, origin: Origin) -> Item:
        with self.turn, self.store.library(owner) as library:
            earlier = library.sought(item_id)
            if earlier and is_underway(earlier[0].reading):
                return earlier[0]
            item = Item(
                id=item_id,
                origin=origin,
                added=earlier[0].added if earlier else datetime.now(UTC),
                reading=reading_started(earlier[0].reading) if earlier else FIRST_READING,
            )
            library.keep_item(item)
        self.read_in_background(owner, item_id)
        return item

    def resume(self) -> None:
        # A reading that was under way when the service last stopped goes on, or is given up.
        self.store.clear_work()
        for underway in self.store.underway():
            with self.turn, self.store.library(underway.owner) as library:
                item = library.item(underway.item_id)
                resumed = item_with(item, reading_resumed(item.reading))
                library.keep_item(resumed)
            if is_underway(resumed.reading):
                self.read_in_background(underway.owner, underway.item_id)
            else:
                self.drop_upload(resumed)

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    def read_in_background(self, owner: str, item_id: str) -> None:
        finished = self.pool.submit(self.read, owner, item_id)
        finished.add_done_callback(partial(self.record, owner, item_id))

    def read(self, owner: str, item_id: str) -> None:
        with self.store.library(owner) as library:
            item = library.item(item_id)
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
        read = Read(title=readout.extracted.title, document=document.id, attempt=Settled())
        with self.turn, self.store.library(owner) as library:
            library.keep_document(document, datetime.now(UTC))
            library.keep_item(item_with(item, read))
        self.drop_upload(item)

    def record(self, owner: str, item_id: str, finished: Future[None]) -> None:
        error = None if finished.cancelled() else finished.exception()
        if error is not None:
            logger.error("reading item %s failed", item_id, exc_info=error)
            with self.turn, self.store.library(owner) as library:
                item = library.item(item_id)
                reason = str(error) or type(error).__name__
                library.keep_item(item_with(item, reading_failed(item.reading, reason)))
            self.drop_upload(item)

    def drop_upload(self, item: Item) -> None:
        # An upload's waiting copy goes once its reading has ended, either way.
        if isinstance(item.origin, Upload):
            self.store.upload(item.id).unlink()
