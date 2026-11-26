import json
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from infonium import api
from infonium.document import HTML_MEDIA_TYPE, Paragraph, document_of
from infonium.library import Item, LibraryStore, Link, Read, Settled, item_identity
from infonium.search import BlockIndex
from tests.model_standin import Asked, ModelStandin, Reply
from tests.unix_server import unix_server

# infonium's service in the test's process, on a library of its own, reading from stand-in
# sites with the stand-in model, a stand-in PDF engine and no browser.
SITE = "https://pages.test"
SERVED_BASE_URL = "http://infonium.test"
FILLER = "".join(
    f"<p>Filler paragraph number {number} keeps the extractor sure that this page is an "
    "article with running text enough to be worth reading, sentence after sentence.</p>"
    for number in range(8)
)
SETTLE_SECONDS = 30
POLL_SECONDS = 0.05
# The limits of a fetch, lowered so that they are met in a second and in megabytes.
LOWERED_FETCH_SECONDS = 1.0
LOWERED_FETCH_BYTES = 4 * 1024**2
MADE_UP_QUOTE = "This sentence was never written anywhere in the source at all."
# How a request gives a document to the model: JSON with a list of its blocks.
USER_ROLE = "user"
BLOCKS_KEY = "blocks"
BLOCK_NUMBER_KEY = "block"
BLOCK_TEXT_KEY = "text"
# The paragraphs of a made-up article that a test asks about: each says one thing in words
# no other says, so that a quote of it is found in it alone.
LEDGER = (
    "The harbour ledger of Quillmoor records that the night ferry left at a quarter past "
    "nine on every weekday of the winter season.",
    "Its keeper, Odalys Brenn, wrote each departure in green ink and each delay in red, so "
    "that a reader could count the late crossings at a glance.",
    "In the third winter the ferry ran late on forty-one nights, most of them in the weeks "
    "when fog lay over the eastern breakwater.",
    "The town council answered by moving the departure to half past eight, which cut the "
    "late crossings to nine in the winter that followed.",
    "日本語の段落もある。港の帳簿は町の図書館に今も保管されている。",
)
# A page that is read again with other words in it: what it says the second time.
REVISED = (
    "A revised account of the Quillmoor ferry says the ledger was lost in a flood and "
    "rewritten from memory by the keeper's daughter in a later year.",
)


def article(title: str, paragraphs: tuple[str, ...] = ()) -> bytes:
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    return (
        f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
        f"{body}{FILLER}</article></body></html>"
    ).encode()


READER = article("Reader fixture")


# A page that trickles, and a file that pours, each for far longer than the limits allow. They
# end at last, so that a limit that is not kept fails the test rather than holding it.
def drip() -> Iterator[bytes]:
    yield b"<html><body>"
    for _ in range(round(10 * LOWERED_FETCH_SECONDS / 0.1)):
        time.sleep(0.1)
        yield b"<p>more</p>" * 20


def pour() -> Iterator[bytes]:
    for _ in range(4 * LOWERED_FETCH_BYTES // 1024**2):
        yield bytes(1024**2)


class Site:
    # The sites a test reads from, answered in this process: how often each path was asked
    # for, and what it answers.
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()
        # Pages a test makes up, served under /page/ by their names until it changes them,
        # as the bytes they are, so that a page may be in any encoding.
        self.pages: dict[str, bytes] = {}
        # Pages whose first fetch is held until the test lets it go, so that a reading stays
        # under way as long as a test needs; later fetches are answered at once.
        self.held: dict[str, Event] = {}
        # Pictures a test makes up, served under /picture/ by their names, each as the kind
        # given; and the last request heard for each path.
        self.pictures: dict[str, tuple[bytes, str]] = {}
        self.requests: dict[str, httpx.Request] = {}

    def let_go(self) -> None:
        for held in self.held.values():
            held.set()

    def answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.hits[path] += 1
        self.requests[path] = request
        match path.split("/")[1:]:
            case ["reader"]:
                return httpx.Response(200, headers={"content-type": "text/html"}, content=READER)
            case ["ledger"]:
                return httpx.Response(200, html=article("Quillmoor ledger", LEDGER).decode())
            case ["revised"] if self.hits[path] == 1:
                return httpx.Response(200, html=article("Quillmoor ledger", LEDGER).decode())
            case ["revised"]:
                return httpx.Response(200, html=article("Quillmoor revised", REVISED).decode())
            case ["page", name] if name in self.pages:
                if name in self.held and self.hits[path] == 1:
                    self.held[name].wait(SETTLE_SECONDS)
                return httpx.Response(
                    200, headers={"content-type": "text/html"}, content=self.pages[name]
                )
            case ["picture", name] if name in self.pictures:
                # A picture's bytes are streamed, as a server's are, for a reader of them as they
                # come.
                content, kind = self.pictures[name]
                return httpx.Response(200, headers={"content-type": kind}, content=iter([content]))
            case ["counted", name]:
                time.sleep(0.3)
                return httpx.Response(200, html=article(f"Counted {name}").decode())
            case ["flaky"] if self.hits[path] == 1:
                return httpx.Response(200, html=article("Flaky fixture").decode())
            case ["xhtml"]:
                return httpx.Response(
                    200,
                    headers={"content-type": "application/xhtml+xml"},
                    content=article("Xhtml fixture"),
                )
            case ["untyped"]:
                return httpx.Response(200, content=article("Untyped fixture"))
            case ["endless"]:
                return httpx.Response(200, headers={"content-type": "text/html"}, content=drip())
            case ["flood"]:
                return httpx.Response(200, content=pour())
            case _:
                return httpx.Response(404)

    @contextmanager
    def stream(self, method: str, url: str, **options: Any) -> Iterator[httpx.Response]:
        with (
            httpx.Client(transport=httpx.MockTransport(self.answer)) as client,
            client.stream(method, url, **options) as response,
        ):
            yield response


def given_json(text: str) -> object:
    # What a message gives as JSON; a message in words gives nothing.
    try:
        return json.loads(text)
    except ValueError:
        return None


def numbered_blocks_asked(asked: Asked) -> list[tuple[int, str]]:
    # The blocks of a document as a request gives them to the model, by their numbers. The
    # document is a message of the asker in JSON, with a list "blocks" of objects, each with its
    # number ("block") and its text ("text"), as the instructions tell the model.
    documents = [given_json(text) for role, text in asked.messages() if role == USER_ROLE]
    return [
        (block[BLOCK_NUMBER_KEY], block[BLOCK_TEXT_KEY])
        for document in documents
        if isinstance(document, dict) and isinstance(document.get(BLOCKS_KEY), list)
        for block in document[BLOCKS_KEY]
    ]


def digest_reply(asked: Asked) -> Reply:
    # A digest that cites the longest block with a quote of it, the same quote under another
    # block's number, and a quote that is nowhere.
    number, text = max(numbered_blocks_asked(asked), key=lambda block: len(block[1]))
    quote = " ".join(text.split()[2:12])
    statements = [
        {"text": "Quoted.", "citations": [{"block_number": number, "quote": quote}]},
        {"text": "Misnumbered.", "citations": [{"block_number": number + 2, "quote": quote}]},
        {"text": "Made up.", "citations": [{"block_number": number, "quote": MADE_UP_QUOTE}]},
    ]
    return Reply(
        pieces=[json.dumps({"sections": [{"title": "Summary", "statements": statements}]})]
    )


class Source(BaseModel):
    # A document kept for an item of the library, as a reading that settled keeps it.
    item: str
    document: str
    title: str
    blocks: list[str]


def keep_sources(
    store: LibraryStore, owner: str, titled: list[tuple[str, list[str]]]
) -> list[Source]:
    now = datetime.now(UTC)
    kept: list[Source] = []
    with store.library(owner) as library:
        for title, texts in titled:
            url = f"{SITE}/source/{uuid4().hex}"
            item_id = item_identity(owner, url)
            document = document_of(
                item_id=item_id,
                extractor="test",
                snapshot="0" * 64,
                media_type=HTML_MEDIA_TYPE,
                blocks=[Paragraph(text=text, marks=[]) for text in texts],
            )
            reading = Read(title=title, document=document.id, attempt=Settled())
            library.keep_item(Item(id=item_id, origin=Link(url=url), added=now, reading=reading))
            library.keep_document(document, now)
            kept.append(Source(item=item_id, document=document.id, title=title, blocks=texts))
    return kept


@contextmanager
def as_owner(owner: str) -> Iterator[None]:
    # The service answers as another reader, whose library is their own.
    api.app.dependency_overrides[api.current_owner] = lambda: owner
    try:
        yield
    finally:
        api.app.dependency_overrides.pop(api.current_owner, None)


def new_owner() -> str:
    return f"reader-{uuid4().hex}"


class App:
    def __init__(
        self,
        client: TestClient,
        store: LibraryStore,
        site: Site,
        model: ModelStandin,
        index: BlockIndex,
    ) -> None:
        self.client = client
        self.store = store
        self.site = site
        self.model = model
        self.index = index
        self.running = True

    def link(self, url: str, **headers: str) -> httpx2.Response:
        return self.client.post("/api/items", json={"url": url}, headers=headers)

    def upload(self, name: str, content: bytes, **headers: str) -> httpx2.Response:
        files = {"file": (name, content, "application/octet-stream")}
        return self.client.post("/api/items/upload", files=files, headers=headers)

    def settled(self, item_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + SETTLE_SECONDS
        while True:
            entry: dict[str, Any] = self.client.get(f"/api/items/{item_id}").json()
            if entry["reading"]["attempt"]["kind"] != "underway":
                return entry
            if time.monotonic() > deadline:
                pytest.fail(f"the reading of {entry['origin']} did not settle in time")
            time.sleep(POLL_SECONDS)

    def read(self, path: str) -> str:
        # The item of a page of the stand-in site, read; a test that needs it read fails here
        # when it is not.
        entry = self.settled(self.link(f"{SITE}{path}").json()["id"])
        if stand(entry) != "read/settled":
            pytest.fail(f"the page {path} that a test needs was not read: {entry['reading']}")
        return str(entry["id"])

    def read_page(self, name: str, content: bytes) -> str:
        # The item of a page the test made up, served by the stand-in site at
        # {SITE}/page/{name} and read.
        self.site.pages[name] = content
        return self.read(f"/page/{name}")

    def left_in(self, folder: Path) -> list[str]:
        return sorted(entry.name for entry in folder.iterdir())

    def stop(self) -> None:
        # The service stopped, its library let go so that another may open it.
        self.client.__exit__(None, None, None)
        self.store.lock.release()
        self.running = False

    @contextmanager
    def served(self) -> Iterator[httpx2.Client]:
        # A client of the same service served by uvicorn over a real connection, which the
        # client may close in the middle of a response as a reader who leaves does.
        with (
            unix_server(api.app, lifespan="off") as path,
            httpx2.Client(
                transport=httpx2.HTTPTransport(uds=str(path)), base_url=SERVED_BASE_URL
            ) as client,
        ):
            yield client


def stand(entry: dict[str, Any]) -> str:
    return f"{entry['reading']['kind']}/{entry['reading']['attempt']['kind']}"
