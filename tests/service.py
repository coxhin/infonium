import json
import re
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient

from infonium import api
from infonium.library import LibraryStore
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
BLOCK_LINE = re.compile(r"^\[(\d+)(?: heading)?\] (.*)$", re.MULTILINE)
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
        # Pages a test makes up, served under /pages/ by their names.
        self.pages: dict[str, bytes] = {}

    def answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.hits[path] += 1
        match path.split("/")[1:]:
            case ["pages", name] if name in self.pages:
                return httpx.Response(
                    200, headers={"content-type": "text/html"}, content=self.pages[name]
                )
            case ["reader"]:
                return httpx.Response(200, headers={"content-type": "text/html"}, content=READER)
            case ["ledger"]:
                return httpx.Response(200, html=article("Quillmoor ledger", LEDGER).decode())
            case ["revised"] if self.hits[path] == 1:
                return httpx.Response(200, html=article("Quillmoor ledger", LEDGER).decode())
            case ["revised"]:
                return httpx.Response(200, html=article("Quillmoor revised", REVISED).decode())
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


def numbered_blocks_asked(asked: Asked) -> list[tuple[int, str]]:
    # The blocks of a document as a request gives them to the model, by their numbers.
    return [(int(number), text) for number, text in BLOCK_LINE.findall(asked.text())]


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


class App:
    def __init__(
        self, client: TestClient, store: LibraryStore, site: Site, model: ModelStandin
    ) -> None:
        self.client = client
        self.store = store
        self.site = site
        self.model = model
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
        # {SITE}/pages/{name} and read.
        self.site.pages[name] = content
        return self.read(f"/pages/{name}")

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
