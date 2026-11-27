from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.document import HTML_MEDIA_TYPE, Paragraph, document_of
from infonium.library import (
    LOCAL_OWNER,
    FirstUnderway,
    Item,
    LibraryStore,
    Read,
    Subscribed,
    Underway,
    Unread,
    item_identity,
)
from infonium.subscriptions.feed import Feed
from infonium.subscriptions.subscription import MOST_ADMITTED_A_POLL
from tests.feed_standin import FeedStandin
from tests.notebook import NOWHERE_ITEM
from tests.service import SITE, App, as_owner, new_owner, stand
from tests.subscribing import (
    ADMITTED,
    AWAITED,
    SUBSCRIBED,
    UNADMITTED,
    UNDERWAY,
    admission_of,
    admissions_ended,
    answered,
    every_posting,
    feed_address,
    missing_posting,
    page_address,
    page_posting,
    poll_by_hand,
    polled,
    posting_numbered,
    served_feed,
    subscribed,
    subscription_path,
    waited_for,
    watched,
)

# The judgments of S8, S9 and S17 of the subscriptions' brief: postings admitted one at a time,
# a posting read at the reader's word, and the intake that reads a subscribed address.
STEP = timedelta(seconds=1)
RENAMINGS = settings(max_examples=12, suppress_health_check=[HealthCheck.function_scoped_fixture])


def page_path(name: str) -> str:
    return f"/page/{name}"


def first_fetched(app: App, names: set[str]) -> list[str]:
    # The pages of the names given that the stand-in site was asked for, in the order it was
    # first asked for each.
    return [
        path.removeprefix("/page/")
        for path in app.site.hits
        if path.removeprefix("/page/") in names
    ]


def longer_than(count: int) -> Callable[[list[str]], bool]:
    return lambda fetched: len(fetched) > count


def reading_posting(app: App, subscription: str, number: int) -> dict[str, Any]:
    posting: dict[str, Any] = answered(
        app.client.post(f"{subscription_path(subscription)}/postings/{number}/reading"),
        "reading a posting at the reader's word",
    )
    return posting


def origin_of(posting: Any, subscription: str) -> dict[str, str]:
    return {
        "kind": SUBSCRIBED,
        "url": posting.address,
        "called": posting.title,
        "subscription": subscription,
    }


# S8: awaited postings are admitted one at a time.


def test_awaited_postings_are_admitted_one_at_a_time_the_longest_waiting_first(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    names = {
        "first": ["first-old", "first-new"],
        "theirs": ["theirs-old", "theirs-new"],
        "last": ["last-old", "last-new"],
    }
    for name in (page for pages in names.values() for page in pages):
        app.site.held[name] = Event()
    other = new_owner()
    views: dict[str, dict[str, Any]] = {}
    postings: dict[str, list[Any]] = {}
    for name, (old, new) in names.items():
        feeds.clock.advance(STEP)
        postings[name] = [page_posting(app, new), page_posting(app, old)]
        address = served_feed(feeds, name, postings[name])
        if name == "theirs":
            with as_owner(other):
                views[name] = subscribed(app, address, admit=2)
        else:
            views[name] = subscribed(app, address, admit=2)
    expected = ["first-new", "first-old", "theirs-new", "theirs-old", "last-new", "last-old"]
    for place, name in enumerate(expected):
        waited_for(
            lambda: first_fetched(app, set(expected)),
            longer_than(place),
            f"the admission of posting {name}",
        )
        watched(feeds)
        fetched = first_fetched(app, set(expected))
        assert fetched == expected[: place + 1], (
            "S8: postings were not handed over one at a time, the one that waited longest first "
            f"and of those that waited as long the newer: {fetched} for {expected}"
        )
        app.site.held[name].set()
        owner = other if name.startswith("theirs") else LOCAL_OWNER
        with as_owner(owner):
            app.settled(item_identity(owner, page_address(name)))
    for name, view in views.items():
        with as_owner(other if name == "theirs" else LOCAL_OWNER):
            shown = admissions_ended(app, view["id"])
        assert [posting["admission"]["item"]["origin"] for posting in shown] == [
            origin_of(posting, view["id"]) for posting in postings[name]
        ], f"S8: a posting was not admitted as an item of its address, called as it is: {shown}"


class Renamed(BaseModel):
    # Feeds that list postings by keys and pages chosen from a few, so that a feed that gives a
    # page a new key lists it again as a new posting, and the newest posting the subscription
    # admits at first.
    feeds: list[list[tuple[int, int]]]
    admit: int


renamings = st.builds(
    Renamed,
    feeds=st.lists(
        st.lists(st.tuples(st.integers(0, 11), st.integers(0, 5)), max_size=14),
        min_size=1,
        max_size=4,
    ),
    admit=st.integers(0, MOST_ADMITTED_A_POLL),
)


@given(renamed=renamings)
@example(renamed=Renamed(feeds=[[(0, 0)], [(1, 0), (0, 0)], [(2, 0), (3, 0)]], admit=1))
@RENAMINGS
def test_whatever_feeds_a_subscription_polls_what_it_admits_is_one_item_an_address(
    subscribing_app: App, feeds: FeedStandin, renamed: Renamed
) -> None:
    app = subscribing_app
    tag = uuid4().hex

    def feed_of(listed: list[tuple[int, int]]) -> Feed:
        return Feed(
            title=f"Feed {tag}",
            site=None,
            postings=[
                missing_posting(f"{tag}-{page}").model_copy(update={"key": f"{tag}-key-{key}"})
                for key, page in listed
            ],
        )

    address = feed_address(tag)
    feeds.serve(address, feed_of(renamed.feeds[0]))
    subscription = subscribed(app, address, renamed.admit)["id"]
    admitted: set[int] = set()
    known: list[tuple[int, int]] = []
    for number, listed in enumerate(renamed.feeds):
        new = [
            posting
            for place, posting in enumerate(listed)
            if posting[0] not in {key for key, _ in [*known, *listed[:place]]}
        ]
        admits = renamed.admit if number == 0 else MOST_ADMITTED_A_POLL
        admitted |= {len(known) + len(new) - place for place in range(min(admits, len(new)))}
        known.extend(reversed(new))
        if number > 0:
            feeds.serve(address, feed_of(listed))
            at = feeds.clock.advance(STEP)
            poll_by_hand(app, subscription)
            polled(app, subscription, at)
    shown = admissions_ended(app, subscription)
    assert [posting["address"] for posting in reversed(shown)] == [
        page_address(f"{tag}-{page}") for _, page in known
    ], "S3: the postings are not each key listed once, numbered by the order it was first listed"
    taken = {posting["number"] for posting in shown if admission_of(posting) != UNADMITTED}
    assert taken == admitted, (
        "S3: the postings awaited or admitted are not the newest of each poll's new ones, "
        f"MOST_ADMITTED_A_POLL at most: {sorted(taken)} for {sorted(admitted)}"
    )
    items = {
        posting["address"]: posting["admission"]["item"]["id"]
        for posting in shown
        if admission_of(posting) == ADMITTED
    }
    for page in items:
        assert {
            posting["admission"]["item"]["id"]
            for posting in shown
            if posting["address"] == page and admission_of(posting) == ADMITTED
        } == {item_identity(LOCAL_OWNER, page)}, (
            f"S8: the postings of the address {page} were not admitted as its one item"
        )
    brought = [
        entry
        for entry in answered(app.client.get("/api/items"), "listing the items")
        if entry["origin"].get("url") in items
    ]
    assert len(brought) == len(items), (
        f"S8: the postings admitted did not lead to one item an address: {len(brought)} items "
        f"for {len(items)} addresses"
    )


def test_a_link_added_by_hand_is_read_at_once_beside_postings_that_wait(
    subscribing_app: App, feeds: FeedStandin, held_reading: str
) -> None:
    app = subscribing_app
    postings = [page_posting(app, f"burst-{number}") for number in range(5)]
    view = subscribed(app, served_feed(feeds, "burst", postings), admit=5)
    page_posting(app, "by-hand")
    linked = app.link(f"{SITE}{page_path('by-hand')}").json()
    ended = app.settled(linked["id"])
    assert stand(ended) == "read/settled", (
        "S8: a link added by hand was not read while the postings a subscription brought waited: "
        f"{ended['reading']}"
    )
    admissions = [admission_of(posting) for posting in every_posting(app, view["id"])]
    assert admissions == [AWAITED] * 5, (
        "S8: a posting was handed over while an item a subscription brought was being read: "
        f"{admissions}"
    )
    app.site.let_go()
    assert [admission_of(posting) for posting in admissions_ended(app, view["id"])] == [
        ADMITTED
    ] * 5, "S8: awaited postings were not admitted once the reading before them ended"


def test_a_posting_whose_address_the_library_holds_is_admitted_as_that_item_as_it_stands(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    held = page_posting(app, "holds-reading")
    app.site.held[held.key] = Event()
    read = page_posting(app, "holds-read")
    read_item = app.read(page_path(read.key))
    reading_item = app.link(held.address).json()["id"]
    waited_for(
        lambda: app.site.hits[page_path(held.key)], lambda hits: hits >= 1, "the held reading"
    )
    before = {
        item: answered(app.client.get(f"/api/items/{item}"), "an item")
        for item in (read_item, reading_item)
    }
    other = new_owner()
    with as_owner(other):
        theirs = subscribed(app, served_feed(feeds, "holds-theirs", [read]), admit=1)
    view = subscribed(app, served_feed(feeds, "holds", [held, read]), admit=2)
    shown = waited_for(
        lambda: every_posting(app, view["id"]),
        lambda postings: all(admission_of(posting) == ADMITTED for posting in postings),
        "the admission of postings whose addresses the library holds",
    )
    items = [posting["admission"]["item"] for posting in shown]
    assert [item["id"] for item in items] == [reading_item, read_item], (
        f"S8: a posting whose address the library holds was not admitted as that item: {items}"
    )
    assert [items[1]] == [before[read_item]], (
        "S8: admitting a posting as an item the library holds changed its origin or reading"
    )
    assert items[0]["origin"] == before[reading_item]["origin"], (
        "S8: admitting a posting as an item being read changed its origin"
    )
    # The other owner's posting waits its turn and is then read for them; this owner's postings
    # are admitted without any reading, so their admission says nothing of when that one is.
    waited_for(
        lambda: app.site.hits[page_path(read.key)],
        lambda hits: hits >= 2,
        "the reading of the other owner's posting",
    )
    assert app.site.hits[page_path(read.key)] == 2, (
        "S8: a posting whose address the library holds was read again, or another owner's was "
        "not read for them"
    )
    with as_owner(other):
        their_item = admissions_ended(app, theirs["id"])[0]["admission"]["item"]
    assert their_item["id"] == item_identity(other, read.address) != read_item, (
        "S8: another owner's posting was admitted as an item of this owner's library"
    )
    app.site.let_go()
    app.settled(reading_item)


# S9: a posting read at the reader's word.


def test_a_posting_read_at_the_readers_word_is_admitted_now_without_waiting_its_turn(
    subscribing_app: App, feeds: FeedStandin, held_reading: str
) -> None:
    app = subscribing_app
    postings = [page_posting(app, f"word-{number}") for number in range(3)]
    view = subscribed(app, served_feed(feeds, "word", postings), admit=1)
    assert [admission_of(posting) for posting in every_posting(app, view["id"])] == [
        AWAITED,
        UNADMITTED,
        UNADMITTED,
    ], "S1: the newest posting is not awaited and the others unadmitted"
    for number, posting in ((3, postings[0]), (2, postings[1])):
        answer = reading_posting(app, view["id"], number)
        assert answer["admission"]["kind"] == ADMITTED, (
            f"S9: a posting {AWAITED if number == 3 else UNADMITTED} read at the reader's word "
            f"is not admitted now: {answer}"
        )
        item = answer["admission"]["item"]
        assert item["origin"] == origin_of(posting, view["id"]), (
            f"S9: a posting read at the reader's word is admitted with another origin: {item}"
        )
        ended = app.settled(item["id"])
        assert stand(ended) == "read/settled", (
            "S9: a posting read at the reader's word waited behind the postings a subscription "
            f"brought: {ended['reading']}"
        )
    app.site.let_go()
    admissions_ended(app, view["id"])
    watched(feeds)
    assert [app.site.hits[page_path(posting.key)] for posting in postings[:2]] == [1, 1], (
        "S9: a posting read at the reader's word was read again when its turn came"
    )
    assert posting_numbered(app, view["id"], 1)["admission"] == {"kind": UNADMITTED}, (
        "S9: reading a posting at the reader's word admitted another"
    )


def test_a_posting_whose_reading_failed_is_read_again_with_the_origin_its_item_has(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    postings = [missing_posting("retried"), missing_posting("retried-link")]
    linked = app.settled(app.link(postings[1].address).json()["id"])
    assert stand(linked) == "unread/failed", "a missing page was read"
    view = subscribed(app, served_feed(feeds, "retried", postings), admit=2)
    failed = admissions_ended(app, view["id"])
    assert [stand(posting["admission"]["item"]) for posting in failed] == ["unread/failed"] * 2, (
        f"S8: the readings of missing pages did not fail: {failed}"
    )
    for posting in postings:
        page_posting(app, posting.key)
    for number, posting in ((2, postings[0]), (1, postings[1])):
        answer = reading_posting(app, view["id"], number)
        item = answer["admission"]["item"]
        assert item["reading"]["attempt"]["kind"] == UNDERWAY, (
            f"S9: a posting whose item's reading failed was not read again: {answer}"
        )
        ended = app.settled(item["id"])
        assert stand(ended) == "read/settled", f"S9: reading again failed: {ended['reading']}"
        expected = (
            origin_of(posting, view["id"])
            if number == 2
            else {"kind": "link", "url": posting.address}
        )
        assert ended["origin"] == expected, (
            f"S9: a posting read again was not read with the origin its item has: {ended}"
        )


def test_a_posting_being_read_or_read_is_answered_as_it_stands(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    posting = page_posting(app, "standing")
    app.site.held[posting.key] = Event()
    view = subscribed(app, served_feed(feeds, "standing", [posting]), admit=1)
    waited_for(lambda: app.site.hits[page_path(posting.key)], lambda hits: hits >= 1, "the reading")
    underway = reading_posting(app, view["id"], 1)
    assert stand(underway["admission"]["item"]) == "unread/underway", (
        f"S9: a posting being read is not answered as it stands: {underway}"
    )
    app.site.let_go()
    item = underway["admission"]["item"]["id"]
    app.settled(item)
    read = reading_posting(app, view["id"], 1)
    assert stand(read["admission"]["item"]) == "read/settled", (
        f"S9: a posting read is not answered as it stands: {read}"
    )
    watched(feeds)
    assert app.site.hits[page_path(posting.key)] == 1, (
        "S9: a posting being read or read was read again at the reader's word"
    )
    for number, status in ((2, HTTPStatus.NOT_FOUND), (0, HTTPStatus.UNPROCESSABLE_ENTITY)):
        response = app.client.post(f"{subscription_path(view['id'])}/postings/{number}/reading")
        assert response.status_code == status, (
            f"S9: reading posting {number} of a subscription that lists one was not refused"
        )
    response = app.client.post(f"{subscription_path(NOWHERE_ITEM)}/postings/1/reading")
    assert response.status_code == HTTPStatus.NOT_FOUND, (
        "S9: reading a posting of a subscription that is not there was not refused"
    )


# S17: the intake reads a subscribed address as it reads a link.


def test_a_subscribed_item_goes_by_its_postings_title_when_read_failed_and_read_again(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    posting = page_posting(app, "called", title="What the feed calls it")
    del app.site.pages[posting.key]
    view = subscribed(app, served_feed(feeds, "called", [posting]), admit=1)
    item = admissions_ended(app, view["id"])[0]["admission"]["item"]
    assert (stand(item), item["reading"]["attempt"]["reason"]) == (
        "unread/failed",
        f"{posting.address} answered 404 Not Found",
    ), f"S17: a subscribed address that fails is not failed with the reason: {item['reading']}"
    page_posting(app, "called", title="What the feed calls it")
    again = answered(app.client.post(f"/api/items/{item['id']}/reading"), "a reading again")
    assert again["origin"] == item["origin"], (
        "S17: a subscribed item read again was not read with its origin"
    )
    read = app.settled(item["id"])
    assert (stand(read), read["reading"]["title"]) == ("read/settled", posting.title), (
        f"S17: a subscribed item is not called what its posting is called: {read['reading']}"
    )
    answered(app.client.post(f"/api/items/{item['id']}/reading"), "a reading again")
    reread = app.settled(item["id"])
    assert (reread["origin"], reread["reading"]["title"]) == (item["origin"], posting.title), (
        f"S17: a subscribed item read again lost its origin or its posting's title: {reread}"
    )


def test_a_subscribed_reading_cut_off_by_a_restart_is_read_again_with_its_origin(
    start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path
) -> None:
    store = LibraryStore(tmp_path / "data")
    now = datetime.now(UTC)
    first = Subscribed(url=f"{SITE}/counted/cut-first", called="First, as listed", subscription="s")
    again = Subscribed(url=f"{SITE}/counted/cut-again", called="Again, as listed", subscription="s")
    again_id = item_identity(LOCAL_OWNER, again.url)
    document = document_of(
        item_id=again_id,
        extractor="test",
        snapshot="0" * 64,
        media_type=HTML_MEDIA_TYPE,
        blocks=[Paragraph(text="Read before the service stopped.", marks=[])],
    )
    with store.library(LOCAL_OWNER) as library:
        library.keep_item(
            Item(
                id=item_identity(LOCAL_OWNER, first.url),
                origin=first,
                added=now,
                reading=Unread(attempt=FirstUnderway(starts=1)),
            )
        )
        library.keep_item(
            Item(
                id=again_id,
                origin=again,
                added=now,
                reading=Read(title=again.called, document=document.id, attempt=Underway(starts=1)),
            )
        )
        library.keep_document(document, now)
    store.lock.release()
    store.engine.dispose()
    app = start_app(poller=feeds.poller)
    for origin in (first, again):
        ended = app.settled(item_identity(LOCAL_OWNER, origin.url))
        assert (stand(ended), ended["origin"], ended["reading"]["title"]) == (
            "read/settled",
            origin.model_dump(mode="json"),
            origin.called,
        ), (
            "S17: a subscribed reading cut off by the service's stopping was not read again with "
            f"its origin and called what its posting is called: {ended}"
        )
