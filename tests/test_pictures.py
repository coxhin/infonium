from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from infonium import pdf, webpictures
from infonium.document import Block, Extracted, ExtractedPicture, Figure, Paragraph
from tests.service import FILLER, SITE, App, as_owner, new_owner, stand

# The pictures a reading keeps with its document, and how they are served: a stand-in for the
# PDF extractor hands pictures over as MinerU's reading does, each the drawing of a region of
# a page for a figure with no source, words or places.
PDF_NAME = "valley.pdf"
# Bytes that are a PDF by their opening, which is all the stand-in extractor needs.
PDF_FILE = b"%PDF-1.7\n% a stand-in for a paper about a valley\n"
WORDS = "Rainfall by month in the valley, as the station measured it."
OTHER_WORDS = "Rainfall by month in the valley, as the station measured it again."
WEBP = "image/webp"
PNG = "image/png"
PICTURE_PATH = "/api/documents/{document}/pictures/{name}"
NO_PICTURE = "0" * 64
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="400"/></svg>'


def drawing(
    colour: tuple[int, int, int], width: int = 320, height: int = 200, kind: str = "WEBP"
) -> bytes:
    with BytesIO() as drawn:
        Image.new("RGB", (width, height), colour).save(drawn, kind)
        return drawn.getvalue()


RED = drawing((200, 40, 40))
BLUE = drawing((40, 40, 200))
GREEN = drawing((40, 200, 40), width=180, height=120)


def name_of(content: bytes) -> str:
    return sha256(content).hexdigest()


class StandinPdf:
    # Each reading of any PDF gives the next of the readings given, the last one for every
    # reading after it: a paragraph of the words given, then a figure for each drawing given,
    # with its picture where the drawing is not None.
    def __init__(self, readings: list[tuple[str, list[bytes | None]]]) -> None:
        self.readings = readings
        self.count = 0

    def extract_pdf(self, file: Path, name: str, engine: Path, folder: Path) -> Extracted:
        words, drawings = self.readings[min(self.count, len(self.readings) - 1)]
        self.count += 1
        blocks: list[Block] = [Paragraph(text=words, marks=[])]
        pictures: list[ExtractedPicture] = []
        for number, drawn in enumerate(drawings):
            if drawn is not None:
                region = folder / f"figure-{number}.webp"
                region.write_bytes(drawn)
                with Image.open(region) as picture:
                    width, height = picture.size
                pictures.append(
                    ExtractedPicture(
                        block=len(blocks), file=region, media_type=WEBP, width=width, height=height
                    )
                )
            blocks.append(Figure(source="", text=""))
        return Extracted(title=name, blocks=blocks, pictures=pictures)


def read_pdf(app: App, monkeypatch: pytest.MonkeyPatch, standin: StandinPdf) -> str:
    monkeypatch.setattr(pdf, "extract_pdf", standin.extract_pdf)
    entry = app.settled(app.upload(PDF_NAME, PDF_FILE).json()["id"])
    assert stand(entry) == "read/settled", f"the stand-in PDF was not read: {entry['reading']}"
    return str(entry["id"])


def read_again(app: App, item: str) -> None:
    started = app.client.post(f"/api/items/{item}/reading")
    assert started.status_code == 200, f"an item was not read again: {started.text}"
    entry = app.settled(item)
    assert stand(entry) == "read/settled", f"a reading again did not settle: {entry['reading']}"


def document_of(app: App, item: str) -> dict[str, Any]:
    document: dict[str, Any] = app.client.get(f"/api/items/{item}/document").json()
    return document


def pictures_of(document: dict[str, Any]) -> list[str | None]:
    return [
        None if block["picture"] is None else block["picture"]["name"]
        for block in document["blocks"]
        if block["kind"] == "figure"
    ]


def kept_pictures(app: App) -> list[str]:
    return sorted(path.name for path in app.store.pictures.rglob("*") if path.is_file())


def picture_answer(app: App, document: str, name: str) -> Any:
    return app.client.get(PICTURE_PATH.format(document=document, name=name))


def test_the_pictures_a_reading_hands_over_are_kept_once_each_with_their_figures(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = read_pdf(app, monkeypatch, StandinPdf([(WORDS, [RED, BLUE, RED, None])]))
    document = document_of(app, item)
    assert pictures_of(document) == [name_of(RED), name_of(BLUE), name_of(RED), None], (
        "the figures of a document do not carry the pictures the reading handed over"
    )
    first = document["blocks"][1]["picture"]
    assert (first["media_type"], first["width"], first["height"]) == (WEBP, 320, 200), (
        f"a kept picture does not say its kind and size: {first}"
    )
    assert kept_pictures(app) == sorted([name_of(RED), name_of(BLUE)]), (
        "the library does not keep each picture once, under the name its bytes hash to"
    )
    assert app.store.picture(name_of(RED)).read_bytes() == RED, "a picture was not kept as it is"
    assert app.left_in(app.store.work) == [], "a reading left a picture in the work folder"


def test_a_kept_picture_is_served_inert_and_long_kept_to_its_owner_alone(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = read_pdf(app, monkeypatch, StandinPdf([(WORDS, [RED, None])]))
    document = document_of(app, item)["id"]
    served = picture_answer(app, document, name_of(RED))
    assert (served.status_code, served.content) == (200, RED), "a kept picture was not served"
    assert served.headers["content-type"] == WEBP, "a picture is served as another kind"
    assert served.headers["content-security-policy"] == "sandbox", "a picture is not inert"
    assert served.headers["x-content-type-options"] == "nosniff", "a picture may be sniffed"
    assert served.headers["cache-control"] == "private, max-age=31536000, immutable", (
        "a picture is not kept by the browser alone for as long as its bytes stand"
    )
    with as_owner(new_owner()):
        stranger = picture_answer(app, document, name_of(RED))
    assert stranger.status_code == 404, "another owner was served a picture of the library"
    for asked, name in ((document, NO_PICTURE), ("f" * 64, name_of(RED))):
        answer = picture_answer(app, asked, name)
        assert answer.status_code == 404, (
            f"a picture was served for a document that does not name it: {answer.status_code}"
        )


def test_a_picture_is_served_for_every_revision_that_names_it_and_no_other(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    standin = StandinPdf([(WORDS, [RED]), (OTHER_WORDS, [GREEN])])
    item = read_pdf(app, monkeypatch, standin)
    earlier = document_of(app, item)["id"]
    read_again(app, item)
    later = document_of(app, item)["id"]
    assert later != earlier, "the second reading gave no new revision: a test's fault"
    assert picture_answer(app, earlier, name_of(RED)).content == RED, (
        "a picture of an earlier revision was not served"
    )
    assert picture_answer(app, later, name_of(GREEN)).content == GREEN, (
        "a picture of the revision the item holds was not served"
    )
    assert picture_answer(app, later, name_of(RED)).status_code == 404, (
        "a picture was served through a revision that does not name it"
    )


def test_a_reading_that_gives_a_document_kept_already_gives_only_its_figures_without_pictures(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    standin = StandinPdf([(WORDS, [RED, None, None]), (WORDS, [BLUE, GREEN, None])])
    item = read_pdf(app, monkeypatch, standin)
    first = document_of(app, item)
    revisions = app.client.get(f"/api/items/{item}/revisions").json()
    read_again(app, item)
    again = document_of(app, item)
    assert again["id"] == first["id"], "a reading of the same text gave another document"
    assert pictures_of(again) == [name_of(RED), name_of(GREEN), None], (
        "a figure with a picture lost it, or one without was not given the picture read again"
    )
    assert [block for block in again["blocks"] if block["kind"] != "figure"] == [
        block for block in first["blocks"] if block["kind"] != "figure"
    ], "a document given pictures changed otherwise"
    assert app.client.get(f"/api/items/{item}/revisions").json() == revisions, (
        "a document given pictures became another revision"
    )
    assert kept_pictures(app) == sorted([name_of(RED), name_of(GREEN)]), (
        "a reading of a document kept already kept a picture that the document does not name"
    )
    assert picture_answer(app, again["id"], name_of(GREEN)).content == GREEN, (
        "a picture given to a document kept already was not served"
    )
    assert app.left_in(app.store.work) == [], "a reading left a picture in the work folder"


def pictured_page(*sources: str) -> bytes:
    figures = "".join(
        f"<figure><img src='{source}' alt='Rainfall, picture {number}'></figure>"
        for number, source in enumerate(sources)
    )
    return (
        "<html><head><title>Valley rainfall</title></head><body><article>"
        f"<h1>Valley rainfall</h1><p>{WORDS}</p>{figures}{FILLER}</article></body></html>"
    ).encode()


def test_a_page_read_from_its_address_keeps_the_pictures_of_its_figures_once_each(
    app: App,
) -> None:
    rain = drawing((40, 90, 200), kind="PNG")
    # A drawing in SVG under a name a picture of a kind kept would have, which its bytes belie.
    app.site.pictures |= {"rain.png": (rain, PNG), "chart.png": (SVG, "image/svg+xml")}
    sources = [f"{SITE}/picture/{name}" for name in ("rain.png", "chart.png", "gone.png")]
    item = app.read_page("rainfall", pictured_page(*sources, sources[0]))
    document = document_of(app, item)
    assert pictures_of(document) == [name_of(rain), None, None, name_of(rain)], (
        "the figures of a page do not carry the pictures downloaded for them"
    )
    assert app.site.hits["/picture/rain.png"] == 1, "a picture shown twice was fetched twice"
    asked = app.site.requests["/picture/rain.png"]
    assert asked.headers["referer"] == f"{SITE}/page/rainfall", (
        "a picture of the page's own origin was not asked for with the page as its referrer"
    )
    served = picture_answer(app, document["id"], name_of(rain))
    assert (served.content, served.headers["content-type"]) == (rain, PNG), (
        "a page's picture was not served as it was downloaded"
    )
    assert app.left_in(app.store.work) == [], "a reading left a picture in the work folder"


def test_an_uploaded_page_keeps_its_pictures_asked_for_without_a_referrer(app: App) -> None:
    rain = drawing((40, 90, 200), kind="PNG")
    app.site.pictures["rain.png"] = (rain, PNG)
    entry = app.settled(
        app.upload("rainfall.html", pictured_page(f"{SITE}/picture/rain.png")).json()["id"]
    )
    assert stand(entry) == "read/settled", f"the uploaded page was not read: {entry['reading']}"
    assert pictures_of(document_of(app, entry["id"])) == [name_of(rain)], (
        "the figure of an uploaded page does not carry its picture"
    )
    assert "referer" not in app.site.requests["/picture/rain.png"].headers, (
        "a picture of an uploaded page was asked for with a referrer"
    )


def test_a_page_whose_pictures_all_fail_is_read_with_its_figures_at_their_sources(
    app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"nothing answers {request.url}")

    monkeypatch.setattr(webpictures, "public_transport", lambda: httpx.MockTransport(failing))
    sources = [f"{SITE}/picture/{number}.png" for number in range(3)]
    item = app.read_page("unpictured", pictured_page(*sources))
    document = document_of(app, item)
    assert pictures_of(document) == [None, None, None], "a figure was given a picture"
    assert [block["source"] for block in document["blocks"] if block["kind"] == "figure"] == (
        sources
    ), "a figure whose picture failed lost its source"
