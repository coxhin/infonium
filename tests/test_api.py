import json
import re
import time
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import filelock
import httpx
import httpx2
import openai
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from infonium import api, intake, search, speech
from infonium.language_model import ChatCompletionsLanguageModel
from infonium.library import (
    CUT_OFF,
    LOCAL_OWNER,
    READING_STARTS,
    Item,
    LibraryStore,
    Link,
    Underway,
    Unread,
    item_identity,
)
from tests.pdf_engine_standin import PDF_DATA

SITE = "https://pages.test"
STUB_MODEL = "stub-model"
# The smallest file that is a picture by its bytes.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)
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


def article(title: str) -> bytes:
    return (
        f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
        f"{FILLER}</article></body></html>"
    ).encode()


class Site:
    # The sites a test reads from, answered in this process: how often each path was asked
    # for, and what it answers.
    def __init__(self) -> None:
        self.hits: Counter[str] = Counter()

    def answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.hits[path] += 1
        match path.split("/")[1:]:
            case ["reader"]:
                return httpx.Response(200, headers={"content-type": "text/html"}, content=READER)
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


class StubModel:
    # A model endpoint answered in this process. Its digest cites the longest block with a
    # quote of it, the same quote under another block's number, and a quote that is nowhere.
    def __init__(self) -> None:
        self.calls = 0

    def answer(self, request: httpx2.Request) -> httpx2.Response:
        self.calls += 1
        asked = json.loads(request.content)
        blocks = [
            (int(number), text)
            for number, text in BLOCK_LINE.findall(asked["messages"][-1]["content"])
        ]
        number, text = max(blocks, key=lambda block: len(block[1]))
        quote = " ".join(text.split()[2:12])
        statements = [
            {"text": "Quoted.", "citations": [{"block_number": number, "quote": quote}]},
            {"text": "Misnumbered.", "citations": [{"block_number": number + 2, "quote": quote}]},
            {"text": "Made up.", "citations": [{"block_number": number, "quote": MADE_UP_QUOTE}]},
        ]
        content = json.dumps({"sections": [{"title": "Summary", "statements": statements}]})
        return httpx2.Response(
            200,
            json={
                "id": "chatcmpl-stub",
                "object": "chat.completion",
                "created": 0,
                "model": asked["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content, "refusal": None},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            },
        )

    def language_model(self) -> ChatCompletionsLanguageModel:
        client = openai.OpenAI(
            api_key="stub",
            base_url="https://model.test/v1",
            http_client=httpx2.Client(transport=httpx2.MockTransport(self.answer)),
            max_retries=0,
        )
        return ChatCompletionsLanguageModel(client=client, name=STUB_MODEL)


class App:
    # infonium's service in this process, on a library of its own, reading from the stand-in
    # sites with the stand-in model, a stand-in PDF engine and no browser.
    def __init__(self, client: TestClient, store: LibraryStore, site: Site, model: StubModel):
        self.client = client
        self.store = store
        self.site = site
        self.model = model

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

    def left_in(self, folder: Path) -> list[str]:
        return sorted(entry.name for entry in folder.iterdir())


def stand(entry: dict[str, Any]) -> str:
    return f"{entry['reading']['kind']}/{entry['reading']['attempt']['kind']}"


@pytest.fixture
def start_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, standin_pdf_engine: Callable[[str], Path]
) -> Iterator[Callable[[], App]]:
    site, model = Site(), StubModel()
    monkeypatch.setattr(httpx, "stream", site.stream)
    monkeypatch.setattr(intake, "FETCH_LIMIT_SECONDS", LOWERED_FETCH_SECONDS)
    monkeypatch.setattr(intake, "FETCH_LIMIT_BYTES", LOWERED_FETCH_BYTES)
    monkeypatch.setattr(api, "language_model", model.language_model)
    engines = intake.Engines(
        pdf=standin_pdf_engine("trap"),
        recogniser=speech.Recogniser(
            base_url="https://speech.test/v1", api_key=SecretStr("unused"), model="none"
        ),
        browser=Path("no-such-browser"),
    )
    embedding = search.EmbeddingEngine(
        base_url="https://embedding.test/v1", api_key=SecretStr("unused"), model="none"
    )
    started: list[TestClient] = []

    def start() -> App:
        store = LibraryStore(tmp_path / "data")
        reader = intake.Intake(store=store, readers=api.READERS, engines=engines)
        monkeypatch.setattr(api, "library_store", lambda: store)
        monkeypatch.setattr(api, "intake", lambda: reader)
        index = search.BlockIndex(directory=tmp_path / "data", engine=embedding)
        monkeypatch.setattr(api, "block_index", lambda: index)
        client = TestClient(api.app)
        client.__enter__()
        started.append(client)
        return App(client, store, site, model)

    yield start
    for client in started:
        client.__exit__(None, None, None)


@pytest.fixture
def app(start_app: Callable[[], App]) -> App:
    return start_app()


def test_a_link_is_read_into_its_document_source_and_digest(app: App) -> None:
    first = app.link(f"{SITE}/reader").json()
    assert stand(first) == "unread/underway" and first["reading"]["attempt"]["starts"] == 0, (
        f"a link did not come back at once, unread with its reading waiting: {first['reading']}"
    )
    read = app.settled(first["id"])
    assert stand(read) == "read/settled" and read["reading"]["title"] == "Reader fixture", (
        f"a page was not read and called by its title: {read['reading']}"
    )
    document = app.client.get(f"/api/items/{first['id']}/document").json()
    assert document["id"] == read["reading"]["document"], "the item names another document"
    assert all(block["places"] == [] for block in document["blocks"]), "a web block has places"
    source = app.client.get(f"/api/items/{first['id']}/source")
    assert source.content == READER, "the source served is not the page that was read"
    assert source.headers["content-type"].startswith("text/html"), "the source is of another kind"
    assert source.headers["content-security-policy"] == "sandbox", "the source is not inert"
    assert source.headers["x-content-type-options"] == "nosniff", "the source may be sniffed"
    part = app.client.get(f"/api/items/{first['id']}/source", headers={"Range": "bytes=10-19"})
    assert (part.status_code, part.content) == (206, READER[10:20]), "a part was not served"


def test_a_digest_keeps_the_citations_found_in_the_document(app: App) -> None:
    item_id = app.link(f"{SITE}/reader").json()["id"]
    app.settled(item_id)
    blocks = app.client.get(f"/api/items/{item_id}/document").json()["blocks"]
    digest = app.client.put(f"/api/items/{item_id}/digest").json()
    quoted, misnumbered, made_up = digest["sections"][0]["statements"]
    [anchor] = quoted["anchors"]
    assert (
        blocks[anchor["block_number"]]["text"][anchor["start"] : anchor["end"]] == (anchor["quote"])
    ), "an anchor's range does not hold its quote"
    assert [found["block_number"] for found in misnumbered["anchors"]] == [
        anchor["block_number"]
    ], "a quote cited under another block's number was not found in its own block"
    assert made_up["anchors"] == [], "a quote found nowhere in the document was kept"
    assert digest["model"] == STUB_MODEL, "the digest does not name the model that wrote it"


def test_the_same_page_given_again_is_the_same_item_document_and_digest(app: App) -> None:
    first = app.link(f"{SITE}/reader").json()
    read = app.settled(first["id"])
    digest = app.client.put(f"/api/items/{first['id']}/digest").json()
    again = app.link(f"{SITE}/reader#some-section").json()
    reread = app.settled(again["id"])
    kept = app.client.put(f"/api/items/{first['id']}/digest").json()
    assert again["id"] == first["id"], "the page given with a fragment became another item"
    assert reread["reading"] == read["reading"], "reading the same page again changed it"
    assert (kept, app.model.calls) == (digest, 1), "the kept digest was written anew"


def test_an_address_imported_many_times_at_once_is_fetched_once(app: App) -> None:
    paths = [f"/counted/page{number}" for number in range(8)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        answers = list(pool.map(lambda path: app.link(f"{SITE}{path}").json(), paths + paths))
    ends = [app.settled(answer["id"]) for answer in answers]
    assert {stand(end) for end in ends} == {"read/settled"}, "an import at once was not read"
    assert [app.site.hits[path] for path in paths] == [1] * len(paths), (
        "an address imported twice at once was fetched more than once"
    )


def test_a_file_uploaded_many_times_at_once_is_read_once(app: App) -> None:
    contents = [article(f"Upload {number}") for number in range(8)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        answers = list(
            pool.map(
                lambda numbered: app.upload(f"upload{numbered[0] % 8}.html", numbered[1]).json(),
                enumerate(contents + contents),
            )
        )
    ends = [app.settled(answer["id"]) for answer in answers]
    assert len({answer["id"] for answer in answers}) == len(contents), "a file became two items"
    assert {stand(end) for end in ends} == {"read/settled"}, "an upload at once was not read"
    assert app.left_in(app.store.uploads) == [], "an upload was left waiting"


def test_a_reread_that_fails_keeps_the_document_and_digest(app: App) -> None:
    item_id = app.link(f"{SITE}/flaky").json()["id"]
    good = app.settled(item_id)
    digest = app.client.put(f"/api/items/{item_id}/digest").json()
    app.link(f"{SITE}/flaky")
    bad = app.settled(item_id)
    assert stand(bad) == "read/failed", f"a failed reread did not end failed: {bad['reading']}"
    assert bad["reading"]["attempt"]["reason"] == f"{SITE}/flaky answered 404 Not Found", (
        "a failed reread does not give the site's answer as its reason"
    )
    assert (bad["reading"]["title"], bad["reading"]["document"]) == (
        good["reading"]["title"],
        good["reading"]["document"],
    ), "a failed reread lost the document read before"
    still = app.client.get(f"/api/items/{item_id}/document")
    kept = app.client.put(f"/api/items/{item_id}/digest").json()
    assert still.status_code == 200, "the document read before is no longer served"
    assert (kept, app.model.calls) == (digest, 1), "the digest was written anew"


def test_a_link_never_read_ends_failed_without_a_document(app: App) -> None:
    item_id = app.link(f"{SITE}/no-such-page").json()["id"]
    failed = app.settled(item_id)
    assert stand(failed) == "unread/failed", f"a missing page did not fail: {failed['reading']}"
    assert (
        failed["reading"]["attempt"]["reason"] == f"{SITE}/no-such-page answered 404 Not Found"
    ), "a missing page does not give the site's answer as the reason"
    assert app.client.get(f"/api/items/{item_id}/document").status_code == 404, (
        "an item that was never read has a document"
    )
    assert app.client.get(f"/api/items/{item_id}/source").status_code == 404, (
        "an item that was never read has a source"
    )
    assert app.client.get(f"/api/items/{'0' * 64}").status_code == 404, "an unknown item exists"


@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        ("picture.png", PNG, "infonium cannot read picture.png: image/png"),
        (
            "talk.mp3",
            b"<MPD><BaseURL>http://127.0.0.1:1/inner.m4a</BaseURL></MPD>",
            "infonium cannot read talk.mp3: application/octet-stream",
        ),
        (
            "notes.bin",
            bytes(range(256)) * 16,
            "infonium cannot read notes.bin: application/octet-stream",
        ),
        ("notes", bytes(range(256)) * 16, "infonium cannot read notes: application/octet-stream"),
    ],
)
def test_an_upload_is_taken_for_what_its_bytes_are(
    app: App, name: str, content: bytes, reason: str
) -> None:
    ended = app.settled(app.upload(name, content).json()["id"])
    assert ended["reading"]["attempt"].get("reason") == reason, (
        f"{name} was not taken for what its bytes are: {ended['reading']}"
    )
    assert app.left_in(app.store.uploads) == [], "a failed upload's waiting copy was kept"


def test_an_upload_is_named_by_its_file_name_alone(app: App) -> None:
    nameless = app.client.post("/api/items/upload", files={"file": ("/", READER, "text/html")})
    assert nameless.status_code == 400, "an upload without a name was taken"
    named = app.upload("../../etc/saved page.html", READER).json()
    assert named["origin"] == {"kind": "upload", "name": "saved page.html"}, (
        "an upload was named by more than its file name"
    )
    assert stand(app.settled(named["id"])) == "read/settled", "a saved page was not read"


@pytest.mark.parametrize(
    ("path", "title"), [("xhtml", "Xhtml fixture"), ("untyped", "Untyped fixture")]
)
def test_a_page_served_as_xhtml_or_untyped_is_read_as_a_page(
    app: App, path: str, title: str
) -> None:
    ended = app.settled(app.link(f"{SITE}/{path}").json()["id"])
    assert (stand(ended), ended["reading"].get("title")) == ("read/settled", title), (
        f"a page served as {path} was not read as a page: {ended['reading']}"
    )


def test_a_page_of_another_site_may_not_change_the_library(app: App) -> None:
    crossed = app.upload("crossed.html", READER, **{"Sec-Fetch-Site": "cross-site"})
    beside = app.link(f"{SITE}/reader", **{"Sec-Fetch-Site": "same-site"})
    elsewhere = app.link(f"{SITE}/reader", Origin="http://elsewhere.test:8000")
    assert [crossed.status_code, beside.status_code, elsewhere.status_code] == [403] * 3, (
        "a request of another site changed the library"
    )
    assert app.client.get("/api/items").json() == [], "a refused request left an item"
    own = app.link(f"{SITE}/reader", **{"Sec-Fetch-Site": "same-origin"})
    here = app.link(f"{SITE}/xhtml", Origin="http://testserver")
    listed = app.client.get("/api/items", headers={"Sec-Fetch-Site": "cross-site"})
    assert [own.status_code, here.status_code, listed.status_code] == [200] * 3, (
        "infonium's own page was refused, or another site may not read"
    )
    app.settled(own.json()["id"])
    app.settled(here.json()["id"])


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("endless", "takes longer than infonium waits"),
        ("flood", "holds more than infonium fetches"),
    ],
)
def test_a_fetch_without_end_is_given_up(app: App, path: str, reason: str) -> None:
    ended = app.settled(app.link(f"{SITE}/{path}").json()["id"])
    assert ended["reading"]["attempt"].get("reason") == f"{SITE}/{path} {reason}", (
        f"a fetch without end was not given up: {ended['reading']}"
    )
    assert app.left_in(app.store.work) == [], "a reading left files in the work folder"


def test_a_pdf_upload_is_read_with_its_places(app: App) -> None:
    pdf = (PDF_DATA / "samples" / "trap.pdf").read_bytes()
    ended = app.settled(app.upload("trap.pdf", pdf).json()["id"])
    assert stand(ended) == "read/settled", f"a PDF was not read: {ended['reading']}"
    blocks = app.client.get(f"/api/items/{ended['id']}/document").json()["blocks"]
    assert blocks and all(
        block["places"] and {place["kind"] for place in block["places"]} == {"page"}
        for block in blocks
    ), "a block of a PDF has no place on a page"
    source = app.client.get(f"/api/items/{ended['id']}/source")
    assert (source.content, source.headers["content-type"]) == (pdf, "application/pdf"), (
        "the source of a PDF is not served as the file uploaded"
    )


def test_a_second_service_on_the_same_library_stops(app: App, tmp_path: Path) -> None:
    with pytest.raises(filelock.Timeout):
        LibraryStore(tmp_path / "data")


def test_readings_cut_off_begin_again_until_they_are_given_up(
    start_app: Callable[[], App], tmp_path: Path
) -> None:
    store = LibraryStore(tmp_path / "data")
    urls = [f"{SITE}/counted/cut{starts}" for starts in range(READING_STARTS + 1)]
    with store.library(LOCAL_OWNER) as library:
        for starts, url in enumerate(urls):
            library.keep_item(
                Item(
                    id=item_identity(LOCAL_OWNER, url),
                    origin=Link(url=url),
                    added=datetime.now(UTC),
                    reading=Unread(attempt=Underway(starts=starts)),
                )
            )
    (store.work / "leftover").mkdir()
    (store.uploads / "stray").write_bytes(b"left by a service that stopped")
    store.lock.release()
    started = start_app()
    ends = [started.settled(item_identity(LOCAL_OWNER, url)) for url in urls]
    assert [stand(end) for end in ends[:-1]] == ["read/settled"] * READING_STARTS, (
        "a reading cut off fewer times than allowed was not read again"
    )
    assert ends[-1]["reading"]["attempt"] == {
        "kind": "failed",
        "reason": CUT_OFF.format(starts=READING_STARTS),
    }, "a reading cut off as often as allowed was not given up with the reason"
    assert started.left_in(started.store.work) == [], "the work folder was not cleared at start"
    assert started.left_in(started.store.uploads) == [], "a stray upload was kept at start"
