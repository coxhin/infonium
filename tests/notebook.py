import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import httpx2
import pytest
from hypothesis import strategies as st

from infonium import refinding as passage_module
from infonium.anchor import Anchor
from infonium.document import Document
from infonium.passage import Found, Passage, Standing
from tests.service import App, stand

# What the tests of highlights and notes share: passages built from a document's blocks, the
# standings a view gives, a second reading of a page, and a better search put in the place of
# the one infonium has.
NOWHERE_DOCUMENT = "f" * 64
NOWHERE_ITEM = "e" * 64
LOST: dict[str, Any] = {"kind": "lost"}
# Where an id stands in what a refusal says, so that refusals of two ids can be compared.
SOME_ID = "<id>"
# Text in several scripts, and characters outside the Basic Multilingual Plane.
SCRIPTS = (
    "The ferry left at a quarter past nine.",
    "港の帳簿は町の図書館に今も保管されている。",
    "Ἐν ἀρχῇ ἦν ὁ λόγος, καὶ ὁ λόγος ἦν πρὸς τὸν θεόν.",
    "שלום עולם, זה משפט בעברית.",
    "مرحبا بالعالم، هذه جملة.",
    "\N{MATHEMATICAL FRAKTUR CAPITAL U}\N{MATHEMATICAL FRAKTUR SMALL N}\N{GRINNING FACE}"
    " \U0002000b\U0002000c outside the plane",
    "e\N{COMBINING ACUTE ACCENT} and\N{ZERO WIDTH SPACE}joined\N{SOFT HYPHEN} text",
)
block_texts = st.one_of(st.sampled_from(SCRIPTS), st.text(min_size=1, max_size=12))


def anchor(document: str, blocks: list[str], number: int, start: int, end: int) -> dict[str, Any]:
    return {
        "document_id": document,
        "block_number": number,
        "start": start,
        "end": end,
        "quote": blocks[number][start:end],
    }


def passage(document: str, blocks: list[str], spans: list[tuple[int, int, int]]) -> dict[str, Any]:
    # A passage of a document by the block, start and end of each of its anchors.
    return {"anchors": [anchor(document, blocks, *span) for span in spans]}


def found(marked: dict[str, Any]) -> dict[str, Any]:
    return {"kind": "found", "passage": marked, "exact": True}


@st.composite
def spans_in(
    draw: st.DrawFn, blocks: list[str], most_blocks: int = 20
) -> list[tuple[int, int, int]]:
    # The anchors of a stretch of text through one block or several neighbours: from a place
    # in the first to the end of it, every block between whole, and from the start of the last
    # to a place in it.
    first = draw(st.integers(0, len(blocks) - 1))
    last = draw(st.integers(first, min(len(blocks), first + most_blocks) - 1))
    if first == last:
        start = draw(st.integers(0, len(blocks[first]) - 1))
        return [(first, start, draw(st.integers(start + 1, len(blocks[first]))))]
    head = (first, draw(st.integers(0, len(blocks[first]) - 1)), len(blocks[first]))
    middle = [(number, 0, len(blocks[number])) for number in range(first + 1, last)]
    tail = (last, 0, draw(st.integers(1, len(blocks[last]))))
    return [head, *middle, tail]


def block_number_of(app: App, item: str, text: str) -> int:
    blocks = document_of(app, item)["blocks"]
    numbers = [number for number, block in enumerate(blocks) if block["text"] == text]
    assert numbers, f"the document of a page does not hold a block that reads {text!r}"
    return numbers[0]


def document_of(app: App, item: str) -> dict[str, Any]:
    response = app.client.get(f"/api/items/{item}/document")
    assert response.status_code == 200, f"the document of an item read was not served: {response}"
    document: dict[str, Any] = response.json()
    return document


def phrase_passage(app: App, item: str, phrases: list[tuple[str, str]]) -> dict[str, Any]:
    # A passage of the document an item holds now, by a phrase in each of the blocks given by
    # their whole text.
    document = document_of(app, item)
    texts = [block["text"] for block in document["blocks"]]
    spans = []
    for block, phrase in phrases:
        number = block_number_of(app, item, block)
        start = texts[number].index(phrase)
        spans.append((number, start, start + len(phrase)))
    return passage(document["id"], texts, spans)


def reread(app: App, item: str, name: str, content: bytes) -> dict[str, Any]:
    # A second reading of a page a test made up, after the page has changed: the same link
    # imported again. The item then holds another document.
    before = app.client.get(f"/api/items/{item}").json()["reading"]["document"]
    app.site.pages[name] = content
    again = app.link(f"https://pages.test/page/{name}").json()
    assert again["id"] == item, "the same link imported again became another item"
    entry = app.settled(item)
    if stand(entry) != "read/settled" or entry["reading"]["document"] == before:
        pytest.fail(f"the page {name} read again did not give another document: {entry}")
    return entry


def mark(app: App, item: str, marked: dict[str, Any], comment: str = "") -> httpx2.Response:
    return app.client.post(
        f"/api/items/{item}/highlights", json={"passage": marked, "comment": comment}
    )


def marked_view(app: App, item: str, marked: dict[str, Any], comment: str = "") -> dict[str, Any]:
    # A highlight that a test needs made; the test fails here when it is not.
    response = mark(app, item, marked, comment)
    if response.status_code != 200:
        pytest.fail(f"a passage a test needs marked was refused: {response.text}")
    view: dict[str, Any] = response.json()
    return view


def highlights_of(app: App, item: str) -> list[dict[str, Any]]:
    response = app.client.get(f"/api/items/{item}/highlights")
    assert response.status_code == 200, f"an item's highlights were not listed: {response.text}"
    listed: list[dict[str, Any]] = response.json()
    return listed


def refusal(response: httpx2.Response, asked: str) -> tuple[int, str]:
    # What a refusal tells, with the id that was asked for put out of it: two refusals that
    # differ in nothing else tell their ids' things apart by nothing.
    detail = str(response.json().get("detail", "")) if response.content else ""
    return response.status_code, detail.replace(asked, SOME_ID)


def instant(text: str) -> datetime:
    return datetime.fromisoformat(text)


def better_search(sought: Passage, marked_in: Document, document: Document) -> Standing:
    # A search that finds every passage in any document: the first character of its first
    # block, as words that differ from those marked.
    first = document.blocks[0].text
    found_there = Anchor(document_id=document.id, block_number=0, start=0, end=1, quote=first[:1])
    return Found(passage=Passage(anchors=[found_there]), exact=False)


@contextmanager
def searched_better(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The search that finds a passage of one revision in another, replaced wherever infonium
    # has taken it from its module, as a better search replaces the first one.
    first_form = passage_module.standing_in
    holders = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith("infonium") and getattr(module, "standing_in", None) is first_form
    ]
    with monkeypatch.context() as patched:
        for module in holders:
            patched.setattr(module, "standing_in", better_search)
        yield
