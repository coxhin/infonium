import gzip
import json
import sqlite3
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from infonium import speech
from infonium.document import HTML_MEDIA_TYPE, Paragraph, document_of
from infonium.library import LIBRARY_FILE, LOCAL_OWNER, Item, LibraryStore, Link, Read, Settled
from infonium.recording import AudioTrack
from infonium.speech import Recogniser
from infonium.transcript import Transcript, Utterance
from tests.notebook import (
    NOWHERE_ITEM,
    highlights_of,
    instant,
    marked_view,
    phrase_passage,
    refusal,
)
from tests.notebook import document_of as served_document
from tests.pdf_engine_standin import PDF_DATA
from tests.service import (
    FILLER,
    LEDGER,
    POLL_SECONDS,
    REVISED,
    SITE,
    App,
    article,
    as_owner,
    new_owner,
    stand,
)
from tests.test_api import PNG
from tests.test_highlights import PRE_M3_TABLES, kept_rows

# The judgments of brief 01's R1 to R5 on revisions and reading an item again, through the
# service's endpoints.
EXAMPLES = settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
TITLE = "Quillmoor ledger"
# Forms of one page, each read into a document of its own.
FORMS = (
    article(TITLE, LEDGER[:4]),
    article(TITLE, (*LEDGER[:2], REVISED[0], *LEDGER[2:4])),
    article(TITLE, (LEDGER[0], *LEDGER[2:4])),
    article(TITLE, (*LEDGER[:4], REVISED[0])),
)


class Door:
    # The two ways of reading an item's link again.
    READING = "reading"
    IMPORT = "import"


DOORS = (Door.READING, Door.IMPORT)
# A page without a title of its own, so that an upload of it goes by its name.
UNTITLED_PAGE = (
    "<html><body><article>"
    + "".join(f"<p>{paragraph}</p>" for paragraph in LEDGER[:4])
    + FILLER
    + "</article></body></html>"
).encode()
UPLOAD_NAME = "harbour notes.html"
PDF_SAMPLE = "trap"
PDF_WORDS = "612 millimetres"
PDF_WORDS_READ_AGAIN = "621 millimetres"
# How long a test watches that a reading waiting its turn does not begin.
WAITING_SECONDS = 0.5
RECORDING_SECONDS = 3.0
SOUND = f"sine=frequency=440:sample_rate=44100:duration={RECORDING_SECONDS}"
PICTURE = f"testsrc=size=64x48:rate=5:duration={RECORDING_SECONDS}"
HEARD = "Reading {number} heard the talk about the ferry."
LENGTH_TOLERANCE = 0.1
CLIENT_ERRORS = range(400, 500)
UNPROCESSABLE = 422
KEPT_EARLIER = datetime(2025, 3, 1, 9, 30, tzinfo=UTC)


def page_url(name: str) -> str:
    return f"{SITE}/page/{name}"


def revisions_of(app: App, item: str) -> dict[str, Any]:
    response = app.client.get(f"/api/items/{item}/revisions")
    assert response.status_code == 200, f"R3: an item's revisions were not listed: {response.text}"
    listed: dict[str, Any] = response.json()
    return listed


def revision_document(app: App, item: str, number: int) -> dict[str, Any]:
    response = app.client.get(f"/api/items/{item}/revisions/{number}/document")
    assert response.status_code == 200, (
        f"R4: the document of revision {number} was not served: {response.text}"
    )
    document: dict[str, Any] = response.json()
    return document


def read_again(app: App, item: str) -> httpx2.Response:
    return app.client.post(f"/api/items/{item}/reading")


def started_again(app: App, item: str) -> dict[str, Any]:
    response = read_again(app, item)
    assert response.status_code == 200, f"R5: an item was not read again: {response.text}"
    entry: dict[str, Any] = response.json()
    return entry


def settled_read(app: App, item: str) -> dict[str, Any]:
    entry = app.settled(item)
    assert stand(entry) == "read/settled", f"a reading the test needs did not settle: {entry}"
    return entry


def read_through(app: App, item: str, name: str, door: str) -> dict[str, Any]:
    # The item of a page read again, by asking for it to be read again or by importing its
    # link again.
    if door == Door.READING:
        started_again(app, item)
    else:
        assert app.link(page_url(name)).json()["id"] == item, (
            "a link imported again is another item"
        )
    return settled_read(app, item)


def numbers_of(listed: dict[str, Any]) -> list[int]:
    return [revision["number"] for revision in listed["revisions"]]


@given(
    forms=st.lists(st.integers(0, len(FORMS) - 1), min_size=1, max_size=6),
    doors=st.lists(st.sampled_from(DOORS), min_size=5, max_size=5),
)
@EXAMPLES
def test_each_reading_that_gives_another_document_is_numbered_once_counted_from_one(
    app: App, forms: list[int], doors: list[str]
) -> None:
    name = uuid4().hex
    app.site.pages[name] = FORMS[forms[0]]
    before = datetime.now(UTC)
    item = app.read(f"/page/{name}")
    readings = [(forms[0], before, datetime.now(UTC), app.client.get(f"/api/items/{item}").json())]
    for form, door in zip(forms[1:], doors, strict=False):
        app.site.pages[name] = FORMS[form]
        before = datetime.now(UTC)
        entry = read_through(app, item, name, door)
        readings.append((form, before, datetime.now(UTC), entry))
    first_read: dict[int, tuple[datetime, datetime, str]] = {}
    for form, started, ended, entry in readings:
        document = entry["reading"]["document"]
        first_read.setdefault(form, (started, ended, document))
        assert first_read[form][2] == document, (
            "one form of a page gave two documents: a test's fault"
        )
    assert len({document for _, _, document in first_read.values()}) == len(first_read), (
        "two forms of a page gave one document: a test's fault"
    )
    listed = revisions_of(app, item)
    order = list(first_read)
    assert numbers_of(listed) == list(range(1, len(order) + 1)), (
        f"R1: the revisions of an item read into {len(order)} documents are not numbered from 1, "
        f"one for each: {listed}"
    )
    assert [revision["document"] for revision in listed["revisions"]] == [
        first_read[form][2] for form in order
    ], f"R1: the revisions are not the documents in the order they were first read: {listed}"
    assert listed["current"] == order.index(forms[-1]) + 1, (
        f"R3: current is not the revision of the document the latest reading gave: {listed}"
    )
    for revision, form in zip(listed["revisions"], order, strict=True):
        started, ended, _ = first_read[form]
        assert started <= instant(revision["read"]) <= ended, (
            "R1: a revision is not kept with the time its document was first read: "
            f"{revision['read']} for a reading between {started} and {ended}"
        )


def test_reading_an_unchanged_source_again_gives_the_same_revision_its_number_and_time(
    app: App,
) -> None:
    name = uuid4().hex
    item = app.read_page(name, FORMS[0])
    first = revisions_of(app, item)
    hits = app.site.hits[f"/page/{name}"]
    entry = started_again(app, item)
    assert (entry["id"], entry["origin"]) == (item, {"kind": "link", "url": page_url(name)}), (
        f"R5: reading again does not answer the item's entry: {entry}"
    )
    assert entry["reading"]["attempt"]["kind"] == "underway", (
        f"R5: reading again does not answer the entry of a reading under way: {entry}"
    )
    settled_read(app, item)
    assert app.site.hits[f"/page/{name}"] == hits + 1, "R5: a link read again was not fetched again"
    again = revisions_of(app, item)
    assert again == first and len(first["revisions"]) == 1, (
        f"R1: reading an unchanged page again changed its revisions: {first} became {again}"
    )


def test_every_revision_keeps_its_document_whatever_the_item_holds_now(app: App) -> None:
    name = uuid4().hex
    item = app.read_page(name, FORMS[0])
    served = {1: served_document(app, item)}
    for number, form in ((2, 1), (3, 2)):
        app.site.pages[name] = FORMS[form]
        started_again(app, item)
        settled_read(app, item)
        served[number] = served_document(app, item)
    app.site.pages[name] = FORMS[0]
    started_again(app, item)
    settled_read(app, item)
    listed = revisions_of(app, item)
    assert (numbers_of(listed), listed["current"]) == ([1, 2, 3], 1), (
        f"R1, R3: a page read back into its first document is not that revision again: {listed}"
    )
    for number, document in served.items():
        assert revision_document(app, item, number) == document, (
            f"R2, R4: revision {number} does not serve the document it was read into"
        )
    assert served_document(app, item) == served[1], (
        "R3: the item does not hold the document of its current revision"
    )
    source = app.client.get(f"/api/items/{item}/source")
    assert source.content == FORMS[0], (
        "R2: the source served after a reading back into an earlier document is not what that "
        "reading read"
    )


def test_an_item_without_a_document_or_a_revision_it_lacks_is_not_found(app: App) -> None:
    unread = app.settled(app.link(f"{SITE}/no-such-page").json()["id"])
    assert stand(unread) == "unread/failed", "a missing page was read: a test's fault"
    item = app.read_page(uuid4().hex, FORMS[0])
    asked = {
        "the revisions of an item without a document": f"/api/items/{unread['id']}/revisions",
        "the revisions of an item that is not there": f"/api/items/{NOWHERE_ITEM}/revisions",
        "a revision of an item without a document": (
            f"/api/items/{unread['id']}/revisions/1/document"
        ),
        "a revision the item does not have": f"/api/items/{item}/revisions/2/document",
    }
    for what, path in asked.items():
        response = app.client.get(path)
        assert response.status_code == 404, (
            f"R3, R4: {what} was not refused as not found: {response}"
        )


def test_another_owners_revisions_are_not_found_as_if_there_were_none(app: App) -> None:
    item = app.read_page(uuid4().hex, FORMS[0])
    hits = sum(app.site.hits.values())
    with as_owner(new_owner()):
        compared = {
            "listing the revisions": [
                f"/api/items/{name}/revisions" for name in (item, NOWHERE_ITEM)
            ],
            "a revision's document": [
                f"/api/items/{name}/revisions/1/document" for name in (item, NOWHERE_ITEM)
            ],
        }
        answers = {
            operation: [
                refusal(app.client.get(path), name)
                for path, name in zip(paths, (item, NOWHERE_ITEM), strict=True)
            ]
            for operation, paths in compared.items()
        }
        answers["reading again"] = [
            refusal(read_again(app, name), name) for name in (item, NOWHERE_ITEM)
        ]
    for operation, (of_another, of_none) in answers.items():
        assert of_another[0] == 404, (
            f"R3, R4, R5: {operation} of another owner's item was not refused"
        )
        assert of_another == of_none, (
            f"R3, R4, R5: {operation} of another owner's item is told apart from one that is not "
            f"there: {of_another} against {of_none}"
        )
    assert sum(app.site.hits.values()) == hits, "R5: another owner's asking read an item again"


def test_a_reading_asked_for_while_one_is_under_way_starts_nothing_and_answers_that_one(
    app: App,
) -> None:
    path = f"/counted/{uuid4().hex}"
    item = app.read(path)
    with ThreadPoolExecutor(max_workers=16) as pool:
        answers = list(
            pool.map(
                lambda door: (
                    read_again(app, item) if door == Door.READING else app.link(f"{SITE}{path}")
                ).json(),
                [Door.READING] * 8 + [Door.IMPORT] * 4,
            )
        )
    settled_read(app, item)
    assert app.site.hits[path] == 2, (
        f"R5: asking to read an item again while a reading of it was under way read it again: "
        f"{app.site.hits[path] - 1} readings"
    )
    assert all(answer["id"] == item for answer in answers), (
        "R5: reading again answered another item"
    )
    assert all(answer["reading"]["attempt"]["kind"] == "underway" for answer in answers), (
        f"R5: asking while a reading is under way does not answer the one under way: {answers}"
    )


def test_a_reading_again_waits_its_turn_and_survives_a_restart(
    start_app: Callable[..., App],
) -> None:
    app = start_app()
    name = uuid4().hex
    item = app.read_page(name, FORMS[0])
    app.site.pages[name] = FORMS[1]
    holders = [uuid4().hex for _ in range(2)]
    for holder in holders:
        app.site.pages[holder] = FORMS[0]
        app.site.held[holder] = Event()
        app.link(page_url(holder))
    deadline = time.monotonic() + WAITING_SECONDS * 10
    while not all(app.site.hits[f"/page/{holder}"] for holder in holders):
        assert time.monotonic() < deadline, "the readings that hold both readers did not begin"
        time.sleep(POLL_SECONDS)
    asked = started_again(app, item)
    time.sleep(WAITING_SECONDS)
    waiting = app.client.get(f"/api/items/{item}").json()
    assert app.site.hits[f"/page/{name}"] == 1, (
        "R5: a reading again began while every reader was busy, without waiting its turn"
    )
    assert (
        waiting["reading"]["attempt"]
        == {"kind": "underway", "starts": 0, "arrived": None}
        == asked["reading"]["attempt"]
    ), f"R5: a reading again waiting its turn is not under way and not begun: {waiting}"
    app.stop()
    again = start_app()
    entry = settled_read(again, item)
    listed = revisions_of(again, item)
    assert (numbers_of(listed), listed["current"]) == ([1, 2], 2), (
        f"R5: a reading again that was waiting when the service stopped was not read after it "
        f"started again: {entry}, {listed}"
    )


def test_a_reading_again_that_fails_keeps_the_document_and_everything_kept_for_it(
    app: App,
) -> None:
    name = uuid4().hex
    item = app.read_page(name, FORMS[0])
    document = served_document(app, item)
    digest = app.client.put(f"/api/items/{item}/digest").json()
    marked = marked_view(app, item, phrase_passage(app, item, [(LEDGER[2], "forty-one nights")]))
    listed = revisions_of(app, item)
    del app.site.pages[name]
    started_again(app, item)
    failed = app.settled(item)
    assert stand(failed) == "read/failed", f"a reading of a missing page did not fail: {failed}"
    assert failed["reading"]["document"] == document["id"], (
        "R5: a reading again that failed took the item's document"
    )
    assert served_document(app, item) == document, (
        "R5: a failed reading changed the item's document"
    )
    assert revisions_of(app, item) == listed, "R1, R5: a failed reading changed the revisions"
    kept = app.client.put(f"/api/items/{item}/digest").json()
    assert (kept, len(app.model.asked())) == (digest, 1), "R5: a failed reading lost the digest"
    assert highlights_of(app, item) == [marked], "R5: a failed reading changed a highlight"


def test_an_upload_is_read_again_from_its_kept_file_under_its_name(app: App) -> None:
    uploaded = app.upload(UPLOAD_NAME, UNTITLED_PAGE).json()
    first = settled_read(app, uploaded["id"])
    assert first["reading"]["title"] == UPLOAD_NAME, (
        f"an untitled page uploaded is not called by its name: {first['reading']}: a test's fault"
    )
    listed = revisions_of(app, uploaded["id"])
    entry = started_again(app, uploaded["id"])
    assert (entry["id"], entry["origin"]) == (uploaded["id"], uploaded["origin"]), (
        f"R5: reading an upload again does not answer its entry: {entry}"
    )
    again = settled_read(app, uploaded["id"])
    assert again["reading"] == first["reading"], (
        f"R5: an upload read again from its kept file is not read as it was: {again['reading']}"
    )
    assert revisions_of(app, uploaded["id"]) == listed, (
        "R1: an upload read again into the same document is not the same revision"
    )
    assert app.left_in(app.store.uploads) == [], "R5: reading an upload again left a waiting copy"


def test_an_upload_read_again_by_an_upgraded_engine_is_a_new_revision(
    app: App, standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    pdf = (PDF_DATA / "samples" / f"{PDF_SAMPLE}.pdf").read_bytes()
    uploaded = app.upload(f"{PDF_SAMPLE}.pdf", pdf).json()
    first = settled_read(app, uploaded["id"])
    first_document = served_document(app, uploaded["id"])
    assert any(PDF_WORDS in block["text"] for block in first_document["blocks"]), (
        "the PDF sample does not say what the test changes: a test's fault"
    )
    # The engine upgraded: the command the service runs gives results that read some words
    # otherwise, and refuses any file but the one uploaded.
    stored = (PDF_DATA / "engine" / f"{PDF_SAMPLE}.json.gz").read_bytes()
    changed = tmp_path / "upgraded.json.gz"
    changed.write_bytes(
        gzip.compress(
            gzip.decompress(stored).decode().replace(PDF_WORDS, PDF_WORDS_READ_AGAIN).encode()
        )
    )
    upgraded = standin_pdf_engine(PDF_SAMPLE, changed)
    engine = standin_pdf_engine(PDF_SAMPLE)
    engine.write_text(upgraded.read_text())
    started_again(app, uploaded["id"])
    again = settled_read(app, uploaded["id"])
    listed = revisions_of(app, uploaded["id"])
    assert (numbers_of(listed), listed["current"]) == ([1, 2], 2), (
        f"R1, R5: a PDF read again into another document is not a new revision: {listed}"
    )
    assert any(
        PDF_WORDS_READ_AGAIN in block["text"]
        for block in revision_document(app, uploaded["id"], 2)["blocks"]
    ), "R5: an upload read again was not read by the engine as it is now"
    assert again["reading"]["title"] == first["reading"]["title"], (
        f"R5: an upload read again is not read under its name: {again['reading']['title']}"
    )
    assert revision_document(app, uploaded["id"], 1) == first_document, (
        "R2: the first revision of an upload read again lost its document"
    )
    source = app.client.get(f"/api/items/{uploaded['id']}/source")
    assert source.content == pdf, "R5: the source of an upload read again is not the file uploaded"


def test_an_upload_without_a_document_is_refused_reading_again_and_left_as_it_is(
    app: App,
) -> None:
    failed = app.settled(app.upload("picture.png", PNG).json()["id"])
    assert stand(failed) == "unread/failed", "a picture was read: a test's fault"
    response = read_again(app, failed["id"])
    assert response.status_code in CLIENT_ERRORS and response.status_code != UNPROCESSABLE, (
        f"R5: an upload with no file left to read was not refused: {response.status_code}"
    )
    reason = str(response.json().get("detail", ""))
    assert reason.strip() and not reason.startswith(("[", "{")), (
        f"R5: an upload refused reading again is not given a reason a reader understands: "
        f"{reason!r}"
    )
    assert app.client.get(f"/api/items/{failed['id']}").json() == failed, (
        "R5: an upload refused reading again was changed"
    )
    assert app.left_in(app.store.uploads) == [], "R5: a refused reading left a waiting copy"


def test_a_library_made_before_revisions_numbers_its_documents_and_changes_none(
    start_app: Callable[..., App], tmp_path: Path
) -> None:
    store = LibraryStore(tmp_path / "data")
    url = f"{SITE}/page/{uuid4().hex}"
    texts = [[*LEDGER[:2]], [*LEDGER[:3]], [LEDGER[0], REVISED[0]]]
    item_id = "c0" + uuid4().hex + "0" * 30
    documents = [
        document_of(
            item_id=item_id,
            extractor="test",
            snapshot="0" * 64,
            media_type=HTML_MEDIA_TYPE,
            blocks=[Paragraph(text=text, marks=[]) for text in paragraphs],
        )
        for paragraphs in texts
    ]
    # Kept in the order opposite to their ids, so that numbering them by id is seen to be wrong.
    documents.sort(key=lambda document: document.id, reverse=True)
    kept_at = [KEPT_EARLIER + timedelta(days=number) for number in range(len(documents))]
    with store.library(LOCAL_OWNER) as library:
        reading = Read(title=TITLE, document=documents[1].id, attempt=Settled())
        library.keep_item(
            Item(id=item_id, origin=Link(url=url), added=KEPT_EARLIER, reading=reading)
        )
        for document, kept in zip(documents, kept_at, strict=True):
            library.keep_document(document, kept)
    store.lock.release()
    store.engine.dispose()
    with closing(sqlite3.connect(tmp_path / "data" / LIBRARY_FILE)) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        newer = [name for (name,) in tables if name not in PRE_M3_TABLES]
        assert newer, "a library of today holds no table that one made before M3 lacks"
        for name in newer:
            connection.execute(f'DROP TABLE "{name}"')
        connection.commit()
        held = kept_rows(connection)
    app = start_app()
    listed = revisions_of(app, item_id)
    app.stop()
    numbered = [
        (revision["number"], revision["document"], instant(revision["read"]))
        for revision in listed["revisions"]
    ]
    assert numbered == [
        (number, document.id, kept)
        for number, (document, kept) in enumerate(zip(documents, kept_at, strict=True), start=1)
    ], (
        f"R1: the documents of an item of a library made before M3 are not numbered in the order "
        f"they were kept, each with the time it was kept: {listed}"
    )
    assert listed["current"] == 2, (
        f"R1, R3: an item of a library made before M3 is not current at its document: {listed}"
    )
    with closing(sqlite3.connect(tmp_path / "data" / LIBRARY_FILE)) as connection:
        assert kept_rows(connection) == held, (
            "R1: numbering a library made before M3 changed what it held"
        )


@pytest.mark.ffmpeg
def test_a_recording_is_read_again_from_the_sound_that_was_kept(
    app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "talk.mkv"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", SOUND, "-f", "lavfi", "-i",
            PICTURE, "-c:a", "aac", "-c:v", "mpeg4", "-shortest", str(video),
        ],
        check=True,
    )  # fmt: skip
    heard: list[float] = []

    def transcribe(audio: AudioTrack, recogniser: Recogniser) -> Transcript:
        # A recogniser that hears the sound it is given, and another thing each time.
        heard.append(kept_length(audio.file))
        text = HEARD.format(number=len(heard))
        return Transcript(utterances=[Utterance(text=text, moment=0.0, duration=1.0)])

    monkeypatch.setattr(speech, "transcribe", transcribe)
    uploaded = app.upload(video.name, video.read_bytes()).json()
    first = settled_read(app, uploaded["id"])
    kept = app.client.get(f"/api/items/{uploaded['id']}/source")
    started_again(app, uploaded["id"])
    again = settled_read(app, uploaded["id"])
    listed = revisions_of(app, uploaded["id"])
    assert (numbers_of(listed), listed["current"]) == ([1, 2], 2), (
        f"R1, R5: a recording heard otherwise when read again is not a new revision: {listed}"
    )
    assert again["reading"]["title"] == first["reading"]["title"], (
        "R5: a recording read again is not read under its name"
    )
    sound = tmp_path / "kept-sound"
    sound.write_bytes(kept.content)
    lengths = [kept_length(sound), *heard[1:]]
    assert len(heard) == 2 and all(
        abs(length - lengths[0]) < LENGTH_TOLERANCE for length in lengths
    ), f"R5: a recording read again was not read from the sound kept of it: {lengths}"
    assert kept.headers["content-type"].startswith("audio/"), "the kept sound is not a sound"


def kept_length(file: Path) -> float:
    shown = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-of",
            "json",
            "-show_entries",
            "format=duration:stream=codec_type",
            str(file),
        ],
        check=True,
        capture_output=True,
    )
    report = json.loads(shown.stdout)
    assert [stream["codec_type"] for stream in report["streams"]] == ["audio"], (
        f"R5: a recording was read again from a file that holds more than its sound: {report}"
    )
    return float(report["format"]["duration"])
