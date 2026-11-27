from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from infonium.api import Media
from infonium.document import HTML_MEDIA_TYPE, MEDIA_TYPE_PATTERN, PDF_MEDIA_TYPE
from infonium.library import (
    Failed,
    FirstUnderway,
    Item,
    KeptSource,
    Read,
    Settled,
    Underway,
    Unread,
)
from infonium.subscriptions.feed import Posting
from tests.feed_standin import FeedStandin
from tests.foldering import (
    ITEMS,
    SNAPSHOT,
    entries,
    item_of,
    keep_item,
    keep_read_item,
    link_origin,
    made_up_document,
    media_by_rule,
)
from tests.service import LEDGER, App, article, as_owner, new_owner, stand
from tests.subscribing import (
    ADMITTED,
    admission_of,
    admissions_ended,
    answered,
    every_posting,
    page_address,
    page_posting,
    served_feed,
    subscribed,
    subscription_path,
)
from tests.test_api import PNG
from tests.test_arrival import ARRIVAL, PDF, PDF_SAMPLE, arrived_entry, held_until, refusing

# The judgments of S6 of the service's brief of the sidebar, through the service's endpoints:
# the media that every answer tells of an item, by the media type of the document it holds, or,
# while it holds none, of the file its reading keeps as it arrived.
EXAMPLES = settings(max_examples=25, suppress_health_check=[HealthCheck.function_scoped_fixture])
# The media types of documents and of files kept as they arrived: those the rule names, others
# a reader meets, others that begin as those the rule names do, and any other written as media
# types are.
NAMED_MEDIA_TYPES = (
    HTML_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    "audio/mp4",
    "audio/mpeg",
    "video/mp4",
    "video/webm",
    "application/xhtml+xml",
    "text/plain",
    "image/png",
    "application/octet-stream",
    "text/html5",
    "application/pdf+zip",
    "audiobook/mp4",
    "videos/mp4",
)
media_types = st.one_of(
    st.sampled_from(NAMED_MEDIA_TYPES),
    st.from_regex(r"(audio|video)/[a-z0-9][a-z0-9.+-]{0,10}", fullmatch=True),
    st.from_regex(MEDIA_TYPE_PATTERN, fullmatch=True),
)
REASON = "the made-up source could not be read"
later_attempts = st.sampled_from([Settled(), Failed(reason=REASON), Underway(starts=1)])
first_attempts = st.one_of(
    st.just(Failed(reason=REASON)),
    st.builds(lambda starts: FirstUnderway(starts=starts), st.integers(0, 2)),
    st.builds(
        lambda starts, media_type: FirstUnderway(
            starts=starts, arrival=KeptSource(snapshot=SNAPSHOT, media_type=media_type)
        ),
        st.integers(0, 2),
        media_types,
    ),
)


@pytest.fixture
def gate(tmp_path: Path) -> Iterator[Path]:
    # Where a held engine waits until the judgment opens it; opened at the end in any case.
    opened = tmp_path / "media-gate"
    yield opened
    opened.touch()


def told_by_answer(entry: dict[str, Any], media_of_documents: dict[str, Media | None]) -> object:
    # What the rule tells an item is read as by the reading its entry shows: the media type of
    # the document it names, or of the file the reading keeps as it arrived, or none.
    reading = entry["reading"]
    if reading["kind"] == "read":
        return media_of_documents[reading["document"]]
    arrival = reading["attempt"].get("arrival")
    return None if arrival is None else media_by_rule(arrival["media_type"])


def media_told(app: App, item: str) -> list[object]:
    # What the list of items and the item's own entry tell of its media.
    listed = [entry["media"] for entry in entries(app) if entry["id"] == item]
    one = answered(app.client.get(f"{ITEMS}/{item}"), "reading an item")["media"]
    return [*listed, one]


@given(media_type=media_types, earlier=st.none() | media_types, attempt=later_attempts)
@example(media_type=HTML_MEDIA_TYPE, earlier=PDF_MEDIA_TYPE, attempt=Settled())
@example(media_type="text/html5", earlier=None, attempt=Settled())
@example(media_type="audiobook/mp4", earlier=HTML_MEDIA_TYPE, attempt=Underway(starts=1))
@EXAMPLES
def test_a_read_items_media_is_told_by_the_media_type_of_the_document_it_holds(
    app: App,
    media_type: str,
    earlier: str | None,
    attempt: Settled | Failed | Underway,
) -> None:
    """S6: an item that holds a made-up document of a drawn media type, after an earlier
    revision of another or none, its latest attempt settled, failed or under way, is told in
    the list and by itself the media the rule gives that document's media type."""
    owner = new_owner()
    origin = link_origin()
    item_id = item_of(owner, origin)
    documents = [made_up_document(item_id, kind) for kind in (earlier, media_type) if kind]
    reading = Read(title="Made up", document=documents[-1].id, attempt=attempt)
    with app.store.library(owner) as library:
        library.keep_item(Item(id=item_id, origin=origin, added=datetime.now(UTC), reading=reading))
        for document in documents:
            library.keep_document(document, datetime.now(UTC))
    with as_owner(owner):
        told = media_told(app, item_id)
    assert told == [media_by_rule(media_type)] * 2, (
        f"S6: an item that holds a document of {media_type}, after one of {earlier}, its latest "
        f"attempt {attempt.kind}, is not told the media of that document: {told}"
    )


@given(attempt=first_attempts)
@example(
    attempt=FirstUnderway(starts=0, arrival=KeptSource(snapshot=SNAPSHOT, media_type="video/mp4"))
)
@example(attempt=FirstUnderway(starts=0))
@EXAMPLES
def test_an_unread_items_media_is_told_by_the_file_kept_as_it_arrived_and_none_without_it(
    app: App, attempt: FirstUnderway | Failed
) -> None:
    """S6: an item that holds no document is told, in the list and by itself, the media the
    rule gives the media type of the file its first reading keeps as it arrived, and none
    where it keeps none or where its reading failed."""
    owner = new_owner()
    item_id = keep_item(app.store, owner, link_origin(), Unread(attempt=attempt))
    arrival = attempt.arrival if isinstance(attempt, FirstUnderway) else None
    with as_owner(owner):
        told = media_told(app, item_id)
    expected = None if arrival is None else media_by_rule(arrival.media_type)
    assert told == [expected] * 2, (
        f"S6: an item that holds no document, its reading {attempt.kind} with "
        f"{'no file' if arrival is None else arrival.media_type} kept as it arrived, is not told "
        f"{expected}: {told}"
    )


def test_every_answer_that_carries_an_entry_tells_the_media_its_item_is_read_as(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    """S6: the list of items, an item, seeing an item, the postings, a posting read at the
    reader's word, an upload, an import and a reading again each tell, of every item they
    carry, the media the rule gives the document its reading names; over a page, PDFs
    uploaded, fetched and posted, a posted page, and made-up recordings of sound and film."""
    app = subscribing_app
    owner = new_owner()
    with as_owner(owner):
        tag = uuid4().hex
        page = app.read_page(f"{tag}-page", article(f"Page {tag}", LEDGER))
        uploaded = app.settled(app.upload(f"{PDF_SAMPLE}.pdf", PDF).json()["id"])["id"]
        app.site.pages[f"{tag}-paper"] = PDF
        fetched = app.settled(app.link(page_address(f"{tag}-paper")).json()["id"])["id"]
        sound = keep_read_item(app.store, owner, link_origin(), "audio/mp4")
        film = keep_read_item(app.store, owner, link_origin(), "video/webm")
        app.site.pages[f"{tag}-posted-paper"] = PDF
        postings = [
            page_posting(app, f"{tag}-posted"),
            Posting(
                key=f"{tag}-posted-paper",
                title="A posted paper",
                address=page_address(f"{tag}-posted-paper"),
                published=None,
            ),
        ]
        subscription = subscribed(app, served_feed(feeds, tag, postings), admit=2)["id"]
        brought = {
            posting["address"]: posting["admission"]["item"]["id"]
            for posting in admissions_ended(app, subscription)
            if admission_of(posting) == ADMITTED
        }
        assert len(brought) == 2, "the two postings of a subscription were not admitted"
        kinds = {
            page: Media.WEB,
            uploaded: Media.PDF,
            fetched: Media.PDF,
            sound: Media.RECORDING,
            film: Media.RECORDING,
            brought[postings[0].address]: Media.WEB,
            brought[postings[1].address]: Media.PDF,
        }
        media_of_documents: dict[str, Media | None] = {
            answered(app.client.get(f"{ITEMS}/{item}/document"), "a document")["id"]: media
            for item, media in kinds.items()
        }
        told: dict[str, list[dict[str, Any]]] = {
            "the list of items": entries(app),
            "one item": [
                answered(app.client.get(f"{ITEMS}/{item}"), "reading an item") for item in kinds
            ],
            "seeing an item": [
                answered(app.client.put(f"{ITEMS}/{item}/seen"), "seeing an item")
                for item in (page, uploaded, sound, brought[postings[1].address])
            ],
            "the entries inside postings": [
                posting["admission"]["item"] for posting in every_posting(app, subscription)
            ],
            "a posting read at the reader's word": [
                answered(
                    app.client.post(
                        f"{subscription_path(subscription)}/postings/{posting['number']}/reading"
                    ),
                    "reading a posting",
                )["admission"]["item"]
                for posting in every_posting(app, subscription)
            ],
            "an upload": [answered(app.upload(f"{PDF_SAMPLE}.pdf", PDF), "an upload")],
        }
        app.settled(uploaded)
        told["an import"] = [
            answered(app.client.post(ITEMS, json={"url": page_address(f"{tag}-page")}), "import")
        ]
        app.settled(page)
        told["a reading again"] = [
            answered(app.client.post(f"{ITEMS}/{item}/reading"), "a reading again")
            for item in (page, fetched)
        ]
        for item in (page, fetched):
            app.settled(item)
        assert {entry["id"] for entry in told["the list of items"]} == set(kinds), (
            "the list of items does not hold the items the judgment read"
        )
        for where, entries_told in told.items():
            found = {entry["id"]: entry["media"] for entry in entries_told}
            expected = {
                entry["id"]: told_by_answer(entry, media_of_documents) for entry in entries_told
            }
            assert found and found == expected, (
                f"S6: {where} does not tell the media each item is read as: {found} against "
                f"{expected}"
            )


def test_an_item_whose_first_reading_is_under_way_or_failed_is_told_the_media_of_what_arrived(
    app: App, standin_pdf_engine: Callable[..., Path], gate: Path
) -> None:
    """S6: an item whose first reading waits for its page is told no media, in the answer that
    began it, the list and by itself, and web once read; one whose PDF arrived and waits for
    its engine is told pdf, and one whose only reading failed, of a PDF refused, a page not
    there or a picture, none, each in the list and by itself."""
    owner = new_owner()
    engine = standin_pdf_engine(PDF_SAMPLE)
    with as_owner(owner):
        tag = uuid4().hex
        app.site.held[tag] = Event()
        app.site.pages[tag] = article(f"Held {tag}", LEDGER)
        held = app.link(page_address(tag)).json()
        assert stand(held) == "unread/underway", f"a page held is not being read: {held}"
        assert [held["media"], *media_told(app, held["id"])] == [None] * 3, (
            "S6: an item whose first reading waits for its page is told a media"
        )
        app.site.held[tag].set()
        assert stand(app.settled(held["id"])) == "read/settled", "the page held was not read"
        assert media_told(app, held["id"]) == [Media.WEB] * 2, "S6: a page read is not web"
        held_until(engine, gate)
        uploaded = app.upload(f"{PDF_SAMPLE}.pdf", PDF).json()
        arrived = arrived_entry(app, uploaded["id"], ARRIVAL)
        assert stand(arrived) == "unread/underway", (
            f"the PDF did not wait for its engine: {arrived}"
        )
        assert [arrived["media"], *media_told(app, uploaded["id"])] == [Media.PDF] * 3, (
            "S6: a PDF being read for the first time, kept as it arrived, is not told pdf"
        )
        gate.touch()
        assert stand(app.settled(uploaded["id"])) == "read/settled", "the PDF was not read"
        refusing(engine)
        app.site.pages[f"{tag}-refused"] = PDF
        refused = app.settled(app.link(page_address(f"{tag}-refused")).json()["id"])
        missing = app.settled(app.link(page_address(f"{tag}-missing")).json()["id"])
        unreadable = app.settled(app.upload("picture.png", PNG).json()["id"])
        for entry, what in (
            (refused, "a PDF the engine refused"),
            (missing, "a page that is not there"),
            (unreadable, "a picture"),
        ):
            assert stand(entry) == "unread/failed", f"the reading of {what} did not fail: {entry}"
            assert [entry["media"], *media_told(app, entry["id"])] == [None] * 3, (
                f"S6: an item whose only reading failed, of {what}, is told a media"
            )
