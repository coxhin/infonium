import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from infonium.document import PDF_MEDIA_TYPE
from infonium.library import (
    LOCAL_OWNER,
    Failed,
    Item,
    KeptSource,
    LibraryStore,
    Underway,
    Unread,
    Upload,
)
from tests.pdf_engine_standin import PDF_DATA
from tests.service import POLL_SECONDS, SETTLE_SECONDS, SITE, App, stand

PDF_SAMPLE = "trap"
PDF = (PDF_DATA / "samples" / f"{PDF_SAMPLE}.pdf").read_bytes()
PDF_SNAPSHOT = sha256(PDF).hexdigest()
ARRIVED = {"snapshot": PDF_SNAPSHOT, "media_type": PDF_MEDIA_TYPE}
REFUSAL = "the engine refuses this file"


@pytest.fixture
def gate(tmp_path: Path) -> Iterator[Path]:
    # Where a held engine waits until the test opens it; opened at the end in any case, so
    # that no engine is left waiting.
    opened = tmp_path / "gate"
    yield opened
    opened.touch()


def held_until(engine: Path, gate: Path) -> None:
    # The engine waits for the gate to open before it does anything.
    shebang, commands = engine.read_text().split("\n", 1)
    engine.write_text(f'{shebang}\nwhile [ ! -e "{gate}" ]; do sleep 0.05; done\n{commands}')


def refusing(engine: Path) -> None:
    engine.write_text(f'#!/bin/sh\necho "{REFUSAL}"\nexit 3\n')


def arrival(entry: dict[str, Any]) -> object:
    return entry["reading"]["attempt"].get("arrived")


def entry_of(app: App, item_id: str) -> dict[str, Any]:
    entry: dict[str, Any] = app.client.get(f"/api/items/{item_id}").json()
    return entry


def arrived_entry(app: App, item_id: str) -> dict[str, Any]:
    # The item once its source has arrived, or its reading has ended.
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        entry = entry_of(app, item_id)
        if arrival(entry) is not None or stand(entry).endswith(("/settled", "/failed")):
            return entry
        if time.monotonic() > deadline:
            pytest.fail(f"the source of {entry['origin']} did not arrive in time")
        time.sleep(POLL_SECONDS)


def snapshots(store: LibraryStore) -> list[str]:
    return sorted(kept.name for kept in store.snapshots.glob("*/*"))


def test_a_pdf_upload_is_shown_as_it_arrived_while_it_is_read(
    app: App, standin_pdf_engine: Callable[..., Path], gate: Path
) -> None:
    held_until(standin_pdf_engine(PDF_SAMPLE), gate)
    item_id = app.upload(f"{PDF_SAMPLE}.pdf", PDF).json()["id"]
    reading = entry_of(app, item_id)
    assert (stand(reading), arrival(reading)) == ("unread/underway", ARRIVED), (
        f"a PDF uploaded is not kept as it arrived while it waits to be read: {reading}"
    )
    source = app.client.get(f"/api/items/{item_id}/source")
    assert (source.content, source.headers["content-type"]) == (PDF, PDF_MEDIA_TYPE), (
        "a PDF being read is not served as the file uploaded"
    )
    assert source.headers["content-security-policy"] == "sandbox", "the file is not inert"
    assert app.client.get(f"/api/items/{item_id}/document").status_code == 404, (
        "an item being read for the first time has a document"
    )
    gate.touch()
    read = app.settled(item_id)
    assert stand(read) == "read/settled", f"the PDF was not read: {read['reading']}"
    assert app.client.get(f"/api/items/{item_id}/source").content == PDF, (
        "the PDF read is not served as the file uploaded"
    )
    assert snapshots(app.store) == [PDF_SNAPSHOT], (
        "the file kept as it arrived is not the one kept beside the document"
    )


def test_a_pdf_link_shown_as_it_arrived_is_let_go_when_its_reading_fails(
    app: App, standin_pdf_engine: Callable[..., Path], gate: Path
) -> None:
    engine = standin_pdf_engine(PDF_SAMPLE)
    refusing(engine)
    held_until(engine, gate)
    app.site.pages["paper"] = PDF
    item_id = app.link(f"{SITE}/page/paper").json()["id"]
    reading = arrived_entry(app, item_id)
    assert (stand(reading), arrival(reading)) == ("unread/underway", ARRIVED), (
        f"a PDF fetched is not kept as it arrived while it is read: {reading}"
    )
    assert app.client.get(f"/api/items/{item_id}/source").content == PDF, (
        "a PDF being read is not served as the file fetched"
    )
    gate.touch()
    failed = app.settled(item_id)
    assert stand(failed) == "unread/failed", f"a refused PDF did not fail: {failed['reading']}"
    assert app.client.get(f"/api/items/{item_id}/source").status_code == 404, (
        "an item whose only reading failed still serves a file"
    )
    assert snapshots(app.store) == [], "the file kept as it arrived outlived its failed reading"


def test_a_failed_reading_leaves_a_document_its_file_of_the_same_bytes(
    app: App, standin_pdf_engine: Callable[..., Path]
) -> None:
    uploaded = app.settled(app.upload(f"{PDF_SAMPLE}.pdf", PDF).json()["id"])
    refusing(standin_pdf_engine(PDF_SAMPLE))
    app.site.pages["paper"] = PDF
    failed = app.settled(app.link(f"{SITE}/page/paper").json()["id"])
    assert stand(failed) == "unread/failed", f"a refused PDF did not fail: {failed['reading']}"
    assert snapshots(app.store) == [PDF_SNAPSHOT], (
        "a failed reading took away the file of another item's document"
    )
    assert app.client.get(f"/api/items/{uploaded['id']}/source").content == PDF, (
        "a document no longer serves its file after a reading of the same bytes failed"
    )


def test_a_file_kept_as_it_arrived_stays_while_another_reading_keeps_it(tmp_path: Path) -> None:
    store = LibraryStore(tmp_path / "data")
    arrived = KeptSource(snapshot=PDF_SNAPSHOT, media_type=PDF_MEDIA_TYPE)
    kept = store.snapshot(PDF_SNAPSHOT)
    kept.parent.mkdir(parents=True)
    kept.write_bytes(PDF)
    item = Item(
        id="0" * 64,
        origin=Upload(name="other.pdf"),
        added=datetime.now(UTC),
        reading=Unread(attempt=Underway(starts=1, arrived=arrived)),
    )
    with store.library(LOCAL_OWNER) as library:
        library.keep_item(item)
    store.release_arrival(arrived)
    assert kept.exists(), "a file another reading keeps as it arrived was taken away"
    with store.library(LOCAL_OWNER) as library:
        library.keep_item(item.model_copy(update={"reading": Unread(attempt=Failed(reason="x"))}))
    store.release_arrival(arrived)
    assert not kept.exists(), "a file that nothing keeps any more was left"


def test_a_reading_cut_off_shows_its_file_again_when_it_begins_again(
    start_app: Callable[..., App],
    standin_pdf_engine: Callable[..., Path],
    gate: Path,
    tmp_path: Path,
) -> None:
    store = LibraryStore(tmp_path / "data")
    item_id = store.upload_identity(LOCAL_OWNER, PDF_DATA / "samples" / f"{PDF_SAMPLE}.pdf")
    store.upload(item_id).write_bytes(PDF)
    kept = store.snapshot(PDF_SNAPSHOT)
    kept.parent.mkdir(parents=True)
    kept.write_bytes(PDF)
    arrived = KeptSource(snapshot=PDF_SNAPSHOT, media_type=PDF_MEDIA_TYPE)
    with store.library(LOCAL_OWNER) as library:
        library.keep_item(
            Item(
                id=item_id,
                origin=Upload(name=f"{PDF_SAMPLE}.pdf"),
                added=datetime.now(UTC),
                reading=Unread(attempt=Underway(starts=1, arrived=arrived)),
            )
        )
    store.lock.release()
    held_until(standin_pdf_engine(PDF_SAMPLE), gate)
    started = start_app()
    reading = entry_of(started, item_id)
    assert (stand(reading), arrival(reading)) == ("unread/underway", ARRIVED), (
        f"a reading begun again after the service stopped lost its file as it arrived: {reading}"
    )
    assert started.client.get(f"/api/items/{item_id}/source").content == PDF, (
        "a reading begun again does not serve its file as it arrived"
    )
    gate.touch()
    read = started.settled(item_id)
    assert stand(read) == "read/settled", f"the PDF was not read again: {read['reading']}"
    assert snapshots(started.store) == [PDF_SNAPSHOT], "the PDF's file is kept twice, or not"


def test_a_reading_of_an_item_that_holds_a_document_keeps_nothing_as_it_arrived(
    app: App, standin_pdf_engine: Callable[..., Path], gate: Path
) -> None:
    item_id = app.settled(app.upload(f"{PDF_SAMPLE}.pdf", PDF).json()["id"])["id"]
    held_until(standin_pdf_engine(PDF_SAMPLE), gate)
    app.client.post(f"/api/items/{item_id}/reading")
    reading = entry_of(app, item_id)
    assert (stand(reading), arrival(reading)) == ("read/underway", None), (
        f"a reading of an item that holds a document kept its file as it arrived: {reading}"
    )
    assert app.client.get(f"/api/items/{item_id}/source").content == PDF, (
        "an item read again does not serve the file beside its document"
    )
    gate.touch()
    assert stand(app.settled(item_id)) == "read/settled", "the PDF was not read again"
    assert snapshots(app.store) == [PDF_SNAPSHOT], "reading again kept another file"
