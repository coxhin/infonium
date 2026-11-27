import shutil
from datetime import timedelta
from http import HTTPStatus
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from infonium.document import Paragraph
from infonium.library import LIBRARY_FILE, LOCAL_OWNER
from tests.feed_standin import FeedStandin
from tests.library_before_digest_styles import DATA_FOLDER, WRITE_AHEAD_LOG
from tests.pdf_engine_standin import PDF_DATA
from tests.service import LEDGER, SITE, App, article, as_owner
from tests.subscribing import (
    admissions_ended,
    answered,
    every_posting,
    listed,
    missing_posting,
    page_posting,
    rested,
    served_feed,
    subscribed,
    subscription_path,
)
from tests.translation_standin import RECORDING, kept_document

# A library that a service of commit 3ddbaaa kept, the last before folders, for the judgments
# that open it in tests/test_folders_kept.py. Its folder holds the library's file alone, and in
# answers.json what that service answered about each owner's items and subscriptions. The
# snapshots are left out: no judgment of it asks for a source. The local owner has a
# subscription whose two newest postings were read, one of them seen; a paused one whose
# posting's reading failed; the item of a subscription removed since; a page added by its
# address and seen; a PDF uploaded; an address whose reading failed; and a recording. Another
# owner has a subscription to the same feed, with one posting read, and a page of their own.
# The folder was made by running this file alone, `uv run pytest tests/library_before_folders.py`,
# at that commit: it is not collected otherwise, for its name is not that of a test module.
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "library_before_folders"
KEPT_FILE = FIXTURE / LIBRARY_FILE
ANSWERS = FIXTURE / "answers.json"
# The owner beside the local one, by a name the judgments ask as.
OTHER_OWNER = "reader-before-folders"
STEP = timedelta(seconds=1)
PDF_SAMPLE = "trap"
RECORDING_PARAGRAPHS = (
    "The ferry's horn sounded twice before the night crossing left the harbour.",
    "Odalys Brenn read the departures aloud for the town's evening broadcast.",
)


class OwnerAnswers(BaseModel):
    # What the service answered one owner: the list of items, each item by its id, the media
    # type of the document each read item holds, the list of subscriptions, each subscription
    # by its id, and every posting of each.
    items: list[dict[str, Any]]
    entries: dict[str, dict[str, Any]]
    media_types: dict[str, str]
    subscriptions: list[dict[str, Any]]
    views: dict[str, dict[str, Any]]
    postings: dict[str, list[dict[str, Any]]]


class AnswersBeforeFolders(BaseModel):
    owners: dict[str, OwnerAnswers]


def answered_owner(app: App) -> OwnerAnswers:
    items = answered(app.client.get("/api/items"), "listing the items")
    views = listed(app)
    return OwnerAnswers(
        items=items,
        entries={
            entry["id"]: answered(app.client.get(f"/api/items/{entry['id']}"), "reading an item")
            for entry in items
        },
        media_types={
            entry["id"]: answered(
                app.client.get(f"/api/items/{entry['id']}/document"), "reading a document"
            )["media_type"]
            for entry in items
            if entry["reading"]["kind"] == "read"
        },
        subscriptions=views,
        views={
            view["id"]: answered(app.client.get(subscription_path(view["id"])), "a subscription")
            for view in views
        },
        postings={view["id"]: every_posting(app, view["id"]) for view in views},
    )


def test_make_the_library_kept_before_folders(
    subscribing_app: App, feeds: FeedStandin, tmp_path: Path
) -> None:
    app = subscribing_app
    reads = served_feed(
        feeds, "kept-reads", [page_posting(app, f"kept-reads-{number}") for number in range(3)]
    )
    feeds.clock.advance(STEP)
    reading = subscribed(app, reads, admit=2)
    admitted = [
        posting["admission"]["item"]["id"]
        for posting in admissions_ended(app, reading["id"])
        if posting["admission"]["kind"] == "admitted"
    ]
    assert len(admitted) == 2, "the two newest postings of the feed were not admitted"
    answered(app.client.put(f"/api/items/{admitted[0]}/seen"), "seeing an item")
    feeds.clock.advance(STEP)
    paused_feed = served_feed(
        feeds, "kept-paused", [missing_posting("kept-paused-0"), page_posting(app, "kept-paused-1")]
    )
    paused = subscribed(app, paused_feed, admit=1)
    admissions_ended(app, paused["id"])
    answered(
        app.client.put(f"{subscription_path(paused['id'])}/follows", json={"follows": "paused"}),
        "pausing a subscription",
    )
    feeds.clock.advance(STEP)
    gone = subscribed(app, served_feed(feeds, "kept-gone", [page_posting(app, "kept-gone-0")]), 1)
    admissions_ended(app, gone["id"])
    removed = app.client.delete(subscription_path(gone["id"]))
    assert removed.status_code == HTTPStatus.NO_CONTENT, "the subscription was not removed"
    linked = app.read_page("kept-link", article("Quillmoor ledger kept", LEDGER))
    answered(app.client.put(f"/api/items/{linked}/seen"), "seeing an item")
    pdf = (PDF_DATA / "samples" / f"{PDF_SAMPLE}.pdf").read_bytes()
    uploaded = app.settled(app.upload(f"{PDF_SAMPLE}.pdf", pdf).json()["id"])
    assert uploaded["reading"]["kind"] == "read", f"the PDF was not read: {uploaded['reading']}"
    failed = app.settled(app.link(f"{SITE}/no-such-page").json()["id"])
    assert failed["reading"]["attempt"]["kind"] == "failed", "the missing page did not fail"
    kept_document(
        app.store,
        "kept-recording",
        "The night crossing",
        [Paragraph(text=text, marks=[]) for text in RECORDING_PARAGRAPHS],
        media_type=RECORDING,
    )
    with as_owner(OTHER_OWNER):
        theirs = subscribed(app, reads, admit=1)
        admissions_ended(app, theirs["id"])
        app.read_page("kept-their-link", article("Quillmoor harbour kept", LEDGER[:2]))
        rested(app, feeds)
    rested(app, feeds)
    owners: dict[str, OwnerAnswers] = {}
    for owner in (LOCAL_OWNER, OTHER_OWNER):
        with as_owner(owner):
            owners[owner] = answered_owner(app)
    assert [len(owners[owner].subscriptions) for owner in owners] == [2, 1], (
        "the owners do not hold the subscriptions made for them"
    )
    app.stop()
    app.store.engine.dispose()
    data = tmp_path / DATA_FOLDER
    assert not (data / WRITE_AHEAD_LOG).exists(), "the library's log was left beside its file"
    FIXTURE.mkdir(parents=True)
    shutil.copyfile(data / LIBRARY_FILE, KEPT_FILE)
    ANSWERS.write_text(
        AnswersBeforeFolders(owners=owners).model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
