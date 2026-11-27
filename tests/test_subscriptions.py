import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from http import HTTPStatus
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from infonium.library import LOCAL_OWNER, item_identity
from infonium.subscriptions.feed import TITLE_CHARACTERS, Feed, Posting, UnreadableFeed
from infonium.subscriptions.subscription import ADMITTED_AT_FIRST, MOST_ADMITTED_A_POLL
from tests.feed_standin import (
    FEEDS_SITE,
    INTERVAL,
    RSSHUB_BASE_URL,
    FeedFunction,
    FeedStandin,
    held_reached,
)
from tests.notebook import NOWHERE_ITEM, instant, refusal
from tests.service import SETTLE_SECONDS, App, as_owner, new_owner
from tests.subscribing import (
    ADMITTED,
    AWAITED,
    READ,
    SETTLED,
    SUBSCRIBED,
    UNADMITTED,
    UNDERWAY,
    admission_of,
    admissions_ended,
    answered,
    every_posting,
    feed_address,
    is_underway,
    listed,
    missing_posting,
    page_address,
    page_posting,
    poll_by_hand,
    polled,
    postings_page,
    served_feed,
    subscribe,
    subscribed,
    subscription_path,
    viewed,
    waited_for,
    watched,
)

# The judgments of S1, S2 and S10 to S13 of the subscriptions' brief, through the service's
# endpoints: subscribing, the views of subscriptions, the pages of their postings, items seen,
# unsubscribing, and each owner's own.
EXAMPLES = settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
# How far the clock is moved between two things a test tells apart by their times.
STEP = timedelta(seconds=1)
POSTINGS_LISTED = 50
MOST_POSTINGS_LISTED = 200
titles = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=["Cs"]),
    min_size=1,
    max_size=TITLE_CHARACTERS,
)
publication_times = st.none() | st.datetimes(
    min_value=datetime(1990, 1, 1),
    max_value=datetime(2100, 1, 1),
    timezones=st.sampled_from(
        [UTC, timezone(timedelta(hours=8)), timezone(-timedelta(hours=5, minutes=30))]
    ),
)
# Addresses written without a scheme, as a reader may paste a site's.
schemeless_addresses = st.from_regex(
    r"[a-z0-9]{1,12}(\.[a-z0-9]{1,12}){1,3}(/[a-z0-9._~-]{1,12}){0,3}", fullmatch=True
)
# Addresses of schemes that are neither the web's nor RSSHub's, and web addresses of no host.
OTHER_SCHEMES = ("ftp", "file", "gopher", "ws", "wss", "javascript", "chrome", "feed", "itpc")
unsubscribable_addresses = st.one_of(
    st.sampled_from(("https://", "http://")),
    st.tuples(
        st.sampled_from(OTHER_SCHEMES), st.from_regex(r"[a-z0-9./_-]{0,20}", fullmatch=True)
    ).map(lambda parts: f"{parts[0]}://{parts[1]}"),
)


def made_postings(tag: str, listed: list[tuple[str, datetime | None]]) -> list[Posting]:
    # Postings of the titles and times given, in that order, each of a page of its own that no
    # test reads.
    return [
        Posting(
            key=f"{tag}-{number}",
            title=title,
            address=page_address(f"{tag}-{number}"),
            published=published,
        )
        for number, (title, published) in enumerate(listed)
    ]


def is_posting_of(shown: dict[str, Any], posting: Posting) -> bool:
    published = shown["published"]
    return (shown["title"], shown["address"]) == (posting.title, posting.address) and (
        (published is None and posting.published is None)
        or (
            published is not None
            and posting.published is not None
            and instant(published) == posting.published
        )
    )


def kept_nothing(app: App) -> None:
    assert listed(app) == [], "a subscription that was refused was kept"


# S1: subscribing.


@given(
    listed_postings=st.lists(st.tuples(titles, publication_times), max_size=14),
    admit=st.integers(0, MOST_ADMITTED_A_POLL),
)
@example(listed_postings=[], admit=ADMITTED_AT_FIRST)
@example(listed_postings=[("Only", None)] * 12, admit=MOST_ADMITTED_A_POLL)
@EXAMPLES
def test_subscribing_lists_every_posting_numbered_by_newness_and_awaits_the_newest_admitted(
    subscribing_app: App,
    feeds: FeedStandin,
    held_reading: str,
    listed_postings: list[tuple[str, datetime | None]],
    admit: int,
) -> None:
    app = subscribing_app
    assert is_underway(app, held_reading), "the reading that keeps admissions waiting has ended"
    tag = uuid4().hex
    postings = made_postings(tag, listed_postings)
    address = served_feed(feeds, tag, postings)
    validators = feeds.validators_of(address)
    at = feeds.clock.advance(STEP)
    view = subscribed(app, address, admit)
    count = len(postings)
    assert [
        (call.address, call.rsshub) for call in feeds.calls_of(FeedFunction.DISCOVERED, address)
    ] == [(address, RSSHUB_BASE_URL)], (
        "S1: subscribing did not find the feed by discovered_feed(address, rsshub) once"
    )
    assert feeds.polls_of(address) == 0, "S1: subscribing polled the feed it had just read"
    assert (view["address"], view["feed"], view["title"], view["site"]) == (
        address,
        address,
        f"Feed {tag}",
        f"{FEEDS_SITE}/{tag}",
    ), (
        "S1: a subscription does not keep the address given and the feed's address, title and "
        f"site: {view}"
    )
    assert (view["follows"], instant(view["added"])) == ("reads", at), (
        f"S1: a new subscription does not read its feed from the time it was added: {view}"
    )
    poll = view["poll"]
    assert (poll["kind"], instant(poll["at"]), poll["found"], instant(poll["due"])) == (
        SETTLED,
        at,
        count,
        at + INTERVAL,
    ), (
        "S1: a new subscription's poll is not settled at the time it was added, with as many "
        f"found as the feed lists and the next due an interval later: {poll}"
    )
    assert (view["postings"], view["unseen"]) == (count, 0), (
        f"S1: a new subscription does not count the postings of its feed, none unseen: {view}"
    )
    assert viewed(app, view["id"]) == view, (
        "S1: the subscription read after subscribing is not the one subscribing answered"
    )
    shown = every_posting(app, view["id"])
    assert [posting["number"] for posting in shown] == list(range(count, 0, -1)), (
        f"S1: a new subscription does not list its feed's {count} postings numbered from 1"
    )
    for posting, listed_posting in zip(shown, postings, strict=True):
        assert is_posting_of(posting, listed_posting), (
            "S1: the first posting a feed lists is not given the highest number, or a posting is "
            f"not listed as the feed gives it: {posting} for {listed_posting}"
        )
        expected = (
            {"kind": AWAITED, "since": at}
            if posting["number"] > count - admit
            else {"kind": UNADMITTED}
        )
        admission = posting["admission"]
        found = (
            {**admission, "since": instant(admission["since"])}
            if "since" in admission
            else admission
        )
        assert found == expected, (
            f"S1: of {count} postings, the newest {admit} are not awaited from the time of "
            f"subscribing and the others unadmitted: posting {posting['number']} is {admission}"
        )
    polled_at = feeds.clock.advance(STEP)
    poll_by_hand(app, view["id"])
    polled(app, view["id"], polled_at)
    assert [call.validators for call in feeds.calls_of(FeedFunction.FETCHED, address)] == [
        validators
    ], "S1: the first poll of a new subscription did not ask from the validators it was found with"


@given(address=schemeless_addresses)
@example(address="blog.test/feed.xml")
@EXAMPLES
def test_an_address_without_a_scheme_is_looked_for_as_https(
    subscribing_app: App, feeds: FeedStandin, address: str
) -> None:
    feed = served_feed(feeds, uuid4().hex, [])
    feeds.lead(f"https://{address}", feed)
    response = subscribe(subscribing_app, address, admit=0)
    assert response.status_code == HTTPStatus.OK, (
        f"S1: an address without a scheme was refused: {response.status_code} {response.text}"
    )
    assert feeds.calls_of(FeedFunction.DISCOVERED)[-1].address == f"https://{address}", (
        "S1: an address without a scheme was not looked for with https:// put before it"
    )


@given(address=unsubscribable_addresses)
@example(address="ftp://feeds.test/rss.xml")
@example(address="https://")
@EXAMPLES
def test_an_address_neither_of_the_web_nor_of_rsshub_is_refused_before_anything_is_fetched(
    subscribing_app: App, feeds: FeedStandin, address: str
) -> None:
    response = subscribe(subscribing_app, address)
    assert response.status_code in {HTTPStatus.BAD_REQUEST, HTTPStatus.UNPROCESSABLE_ENTITY}, (
        f"S1: an address that is neither a web address nor an rsshub:// route ({address!r}) was "
        f"not refused as invalid: {response.status_code} {response.text[:200]}"
    )
    assert feeds.calls_of(FeedFunction.DISCOVERED) == [], (
        f"S1: an address that is neither a web address nor an rsshub:// route ({address!r}) was "
        "looked for before it was refused"
    )
    kept_nothing(subscribing_app)


def test_an_rsshub_route_is_subscribed_polled_and_listed_like_any_feed(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    route = "rsshub://bilibili/user/video/2267573"
    first = [missing_posting("route-0")]
    feeds.serve(route, Feed(title="Route", site=None, postings=first))
    view = subscribed(app, route, admit=0)
    assert [(call.address, call.rsshub) for call in feeds.calls_of(FeedFunction.DISCOVERED)] == [
        (route, RSSHUB_BASE_URL)
    ], "S1: an rsshub:// route was not looked for as it was given, at the RSSHub instance"
    assert (view["address"], view["feed"], view["postings"]) == (route, route, 1), (
        f"S1: a subscription to a route does not keep the route as its feed's address: {view}"
    )
    feeds.serve(
        route, Feed(title="Route", site=None, postings=[missing_posting("route-1"), *first])
    )
    at = feeds.clock.advance(STEP)
    poll_by_hand(app, view["id"])
    assert polled(app, view["id"], at)["poll"]["found"] == 1, (
        "S3: a poll of a route did not find its new posting"
    )
    assert [call.address for call in feeds.calls_of(FeedFunction.FETCHED)] == [route], (
        "S3: a route was not polled by its address"
    )
    assert [posting["number"] for posting in every_posting(app, view["id"])] == [2, 1], (
        "S3: a route's postings are not listed as any feed's are"
    )
    assert subscribed(app, route, admit=0)["id"] == view["id"], (
        "S1: subscribing again to a route is not answered as its subscription"
    )


def test_a_feed_subscribed_to_already_is_answered_as_that_subscription_unchanged_unpolled(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    first_postings = [missing_posting(f"again-{number}") for number in range(3)]
    feed = served_feed(feeds, "again", first_postings)
    page = "https://blog.test/again"
    feeds.lead(page, feed)
    first = subscribed(app, feed, admit=0)
    before = every_posting(app, first["id"])
    feeds.serve(
        feed,
        feeds.served[feed].model_copy(
            update={"postings": [missing_posting("again-new"), *first_postings], "title": "Renamed"}
        ),
    )
    feeds.clock.advance(STEP)
    for address in (feed, page, f"  {feed}  "):
        again = subscribe(app, address, admit=MOST_ADMITTED_A_POLL)
        assert again.status_code == HTTPStatus.OK, (
            f"S1: subscribing again to a feed subscribed to was refused: {again.text}"
        )
        assert again.json() == first, (
            "S1: subscribing again to a feed subscribed to, by the address "
            f"{address!r}, is not answered as that subscription, unchanged: {again.json()}"
        )
    assert listed(app) == [first], "S1: subscribing again to a feed kept a second subscription"
    assert every_posting(app, first["id"]) == before, (
        "S1: subscribing again to a feed changed the postings of its subscription"
    )
    assert feeds.polls_of(feed) == 0, "S1: subscribing again to a feed polled it"


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        ("the address leads to no feed", "https://blog.test is a web page that names no feed"),
        ("the feed cannot be read", "the feed at https://blog.test/feed.xml is not XML"),
        (
            "the address cannot be reached",
            "https://blog.test/feed.xml did not answer: [Errno -2] Name or service not known",
        ),
    ],
)
def test_a_feed_that_cannot_be_read_or_reached_is_refused_with_the_reason_and_nothing_kept(
    subscribing_app: App, feeds: FeedStandin, failure: str, said: str
) -> None:
    error = UnreadableFeed(said)
    app = subscribing_app
    feed = served_feed(feeds, "unreachable", [page_posting(app, "unreachable-0")])
    feeds.fail_discovery(feed, error)
    response = subscribe(app, feed)
    assert (response.status_code, response.json().get("detail")) == (
        HTTPStatus.BAD_REQUEST,
        str(error),
    ), f"S1: subscribing where {failure} is not refused with the reason: {response.text}"
    kept_nothing(app)
    watched(feeds)
    assert answered(app.client.get("/api/items"), "listing the items") == [], (
        f"S1: subscribing where {failure} admitted a posting"
    )
    view = subscribed(app, feed, admit=0)
    assert (view["postings"], len(every_posting(app, view["id"]))) == (1, 1), (
        f"S1: a refused subscription left something behind: subscribing again lists {view}"
    )


def test_an_address_that_leads_to_no_feed_is_refused_and_nothing_kept(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    response = subscribe(subscribing_app, "https://blog.test/no-feed-here")
    assert response.status_code == HTTPStatus.BAD_REQUEST, (
        f"S1: an address that leads to no feed was not refused: {response.status_code}"
    )
    assert "https://blog.test/no-feed-here" in response.json()["detail"], (
        f"S1: an address that leads to no feed is refused without the reason: {response.text}"
    )
    kept_nothing(subscribing_app)


def test_a_subscription_is_known_by_its_owner_and_its_feed(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, "known", [missing_posting("known-0")])
    other = served_feed(feeds, "other", [missing_posting("other-0")])
    page = "https://blog.test/known"
    feeds.lead(page, feed)
    first = subscribed(app, feed, admit=0)["id"]
    assert app.client.delete(subscription_path(first)).status_code == HTTPStatus.NO_CONTENT, (
        "S12: unsubscribing failed"
    )
    again = subscribed(app, page, admit=0)["id"]
    another_feed = subscribed(app, other, admit=0)["id"]
    with as_owner(new_owner()):
        another_owner = subscribed(app, feed, admit=0)["id"]
    assert again == first, (
        "S1: subscribing again to the same feed, by another address, is known by another id"
    )
    assert len({first, another_feed, another_owner}) == 3, (
        "S1: subscriptions of other feeds or of other owners share an id"
    )


# S2: the views of subscriptions.


def test_subscriptions_are_listed_the_one_added_last_first_as_each_is_read(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    ids: list[str] = []
    for number in range(4):
        feeds.clock.advance(STEP)
        postings = [missing_posting(f"order-{number}-{posting}") for posting in range(number)]
        ids.append(subscribed(app, served_feed(feeds, f"order-{number}", postings), admit=0)["id"])
    views = listed(app)
    assert [view["id"] for view in views] == ids[::-1], (
        "S2: subscriptions are not listed the one added last first"
    )
    assert [view["postings"] for view in views] == [3, 2, 1, 0], (
        "S2: a listed subscription does not count the postings it lists"
    )
    for view in views:
        assert viewed(app, view["id"]) == view, (
            "S2: a subscription read alone is not as the list shows it"
        )
    assert app.client.get(subscription_path(NOWHERE_ITEM)).status_code == HTTPStatus.NOT_FOUND, (
        "S2: a subscription that is not there was found"
    )


def test_unseen_counts_the_admitted_items_a_subscription_brought_that_are_read_and_not_seen(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    linked = page_posting(app, "unseen-linked")
    linked_item = app.read(f"/page/{linked.key}")
    postings = [
        page_posting(app, "unseen-a"),
        page_posting(app, "unseen-b"),
        page_posting(app, "unseen-seen"),
        missing_posting("unseen-missing"),
        linked,
        page_posting(app, "unseen-unadmitted"),
    ]
    view = subscribed(app, served_feed(feeds, "unseen", postings), admit=5)
    shown = admissions_ended(app, view["id"])
    entries = {
        posting["address"]: posting["admission"]["item"]
        for posting in shown
        if admission_of(posting) == ADMITTED
    }
    assert len(entries) == 5, f"S8: the five newest postings were not all admitted: {shown}"
    assert entries[linked.address]["id"] == linked_item, (
        "S8: a posting whose address the library held was not admitted as that item"
    )
    stands = [
        (entry["origin"]["kind"], entry["reading"]["kind"], entry["seen"])
        for entry in entries.values()
    ]
    assert stands.count((SUBSCRIBED, READ, None)) == 3, (
        f"S2: the postings admitted are not read as the test needs them: {stands}"
    )
    assert viewed(app, view["id"])["unseen"] == 3, (
        "S2: unseen does not count the admitted postings whose items a subscription brought, "
        "read and not seen, and those alone"
    )
    seen = app.client.put(f"/api/items/{entries[page_address('unseen-seen')]['id']}/seen")
    assert seen.status_code == HTTPStatus.OK, f"S11: seeing an item failed: {seen.text}"
    assert viewed(app, view["id"])["unseen"] == 2, (
        "S2: an item that was seen is still counted unseen by its subscription"
    )
    assert next(found for found in listed(app) if found["id"] == view["id"])["unseen"] == 2, (
        "S2: the list of subscriptions does not count the unseen as the subscription does"
    )


# S10: a subscription's postings.


@given(
    before=st.none() | st.integers(1, 140),
    limit=st.none() | st.integers(1, MOST_POSTINGS_LISTED),
)
@example(before=None, limit=None)
@example(before=1, limit=None)
@example(before=131, limit=MOST_POSTINGS_LISTED)
@EXAMPLES
def test_a_page_of_postings_holds_the_newest_below_before_and_no_more_than_the_limit(
    subscribing_app: App, feeds: FeedStandin, before: int | None, limit: int | None
) -> None:
    app = subscribing_app
    count = 130
    feed = feed_address("paged")
    if feed not in feeds.served:
        postings = [missing_posting(f"paged-{number}") for number in range(count)]
        served_feed(feeds, "paged", postings)
        subscribed(app, feed, admit=0)
    subscription = listed(app)[0]["id"]
    page = answered(postings_page(app, subscription, before, limit), "a page of postings")
    numbers = [number for number in range(count, 0, -1) if before is None or number < before]
    assert [posting["number"] for posting in page] == numbers[: limit or POSTINGS_LISTED], (
        f"S10: a page of postings before {before}, of {limit} at most, does not hold the newest "
        "of those below before, newest first, as many as the limit"
    )
    for posting in page:
        assert posting["title"] == f"Posting paged-{count - posting['number']}", (
            f"S10: posting {posting['number']} is listed as another posting: {posting}"
        )
        assert posting["admission"] == {"kind": UNADMITTED}, (
            f"S10: a posting is not listed with its admission: {posting}"
        )


@pytest.mark.parametrize(
    "query",
    [{"limit": 0}, {"limit": MOST_POSTINGS_LISTED + 1}, {"before": 0}, {"before": "newest"}],
)
def test_a_page_of_postings_out_of_bounds_is_refused(
    subscribing_app: App, feeds: FeedStandin, query: dict[str, Any]
) -> None:
    view = subscribed(subscribing_app, served_feed(feeds, "bounds", [missing_posting("b")]))
    response = subscribing_app.client.get(f"{subscription_path(view['id'])}/postings", params=query)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, (
        f"S10: a page of postings asked with {query} was not refused: {response.status_code}"
    )


# S11: seen.


def seen_now(app: App, item: str) -> tuple[datetime, datetime, dict[str, Any]]:
    before = datetime.now(UTC)
    entry: dict[str, Any] = answered(app.client.put(f"/api/items/{item}/seen"), "seeing an item")
    return before, datetime.now(UTC), entry


def test_an_item_is_seen_the_first_time_it_is_asked_and_that_time_is_kept(
    subscribing_app: App,
) -> None:
    app = subscribing_app
    page_posting(app, "seen-once")
    item = app.read("/page/seen-once")
    assert answered(app.client.get(f"/api/items/{item}"), "reading an item")["seen"] is None, (
        "S11: an item never seen is said to have been seen"
    )
    with as_owner(new_owner()):
        of_another = refusal(app.client.put(f"/api/items/{item}/seen"), item)
        of_none = refusal(app.client.put(f"/api/items/{NOWHERE_ITEM}/seen"), NOWHERE_ITEM)
    assert of_another[0] == HTTPStatus.NOT_FOUND and of_another == of_none, (
        "S11, S13: seeing another owner's item was not refused as one that is not there: "
        f"{of_another} against {of_none}"
    )
    assert answered(app.client.get(f"/api/items/{item}"), "reading an item")["seen"] is None, (
        "S11, S13: another owner's asking took an item for seen"
    )
    before, after, entry = seen_now(app, item)
    assert entry["seen"] is not None and before <= instant(entry["seen"]) <= after, (
        f"S11: seeing an item did not keep the time it was seen: {entry['seen']}"
    )
    time.sleep(0.01)
    _, _, again = seen_now(app, item)
    assert again == entry, "S11: seeing an item again changed when it was seen"
    assert answered(app.client.get(f"/api/items/{item}"), "reading an item") == entry, (
        "S11: an item's entry does not tell when it was first seen"
    )


def test_every_entry_tells_when_its_item_was_seen_and_none_where_it_was_not(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    postings = [page_posting(app, "entry-seen"), page_posting(app, "entry-unseen")]
    view = subscribed(app, served_feed(feeds, "entries", postings), admit=2)
    admitted = {
        posting["address"]: posting["admission"]["item"]["id"]
        for posting in admissions_ended(app, view["id"])
    }
    assert len(admitted) == 2, "S8: the two postings were not both admitted"
    seen_item, unseen_item = admitted[postings[0].address], admitted[postings[1].address]
    seen = answered(app.client.put(f"/api/items/{seen_item}/seen"), "seeing an item")["seen"]
    expected = {seen_item: seen, unseen_item: None}
    told: dict[str, list[dict[str, Any]]] = {
        "the list of items": answered(app.client.get("/api/items"), "listing the items"),
        "one item": [
            answered(app.client.get(f"/api/items/{item}"), "reading an item") for item in expected
        ],
        "the entries inside postings": [
            posting["admission"]["item"] for posting in every_posting(app, view["id"])
        ],
        "an import": [
            answered(app.client.post("/api/items", json={"url": posting.address}), "an import")
            for posting in postings
        ],
    }
    for posting in postings:
        app.settled(admitted[posting.address])
    told["a reading again"] = [
        answered(app.client.post(f"/api/items/{item}/reading"), "a reading again")
        for item in expected
    ]
    for where, entries in told.items():
        found = {entry["id"]: entry["seen"] for entry in entries if entry["id"] in expected}
        assert found == expected, (
            f"S11: {where} does not tell when each item was seen, and none where it was not: "
            f"{found}"
        )
    for item in expected:
        app.settled(item)


def test_seeing_a_subscription_sees_the_read_items_of_its_postings_not_seen_yet(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    postings = [
        page_posting(app, "see-all-seen"),
        page_posting(app, "see-all-a"),
        page_posting(app, "see-all-b"),
        missing_posting("see-all-missing"),
        page_posting(app, "see-all-unadmitted"),
    ]
    view = subscribed(app, served_feed(feeds, "see-all", postings), admit=4)
    items = {
        posting["address"]: posting["admission"]["item"]["id"]
        for posting in admissions_ended(app, view["id"])
        if admission_of(posting) == ADMITTED
    }
    assert len(items) == 4, "S8: the four newest postings were not admitted"
    early = answered(
        app.client.put(f"/api/items/{items[postings[0].address]}/seen"), "seeing an item"
    )["seen"]
    before = datetime.now(UTC)
    answer = answered(app.client.put(f"{subscription_path(view['id'])}/seen"), "seeing all")
    after = datetime.now(UTC)
    assert answer["unseen"] == 0, f"S11: a subscription seen still counts unseen: {answer}"
    seen = {
        address: answered(app.client.get(f"/api/items/{item}"), "reading an item")["seen"]
        for address, item in items.items()
    }
    assert seen[postings[0].address] == early, (
        "S11: seeing a subscription changed when an item seen before was seen"
    )
    for posting in postings[1:3]:
        found = seen[posting.address]
        assert found is not None and before <= instant(found) <= after, (
            "S11: seeing a subscription did not take a read item of its postings for seen now"
        )
    assert seen[postings[3].address] is None, (
        "S11: seeing a subscription took an item that is not read for seen"
    )
    assert (
        app.client.put(f"{subscription_path(NOWHERE_ITEM)}/seen").status_code
        == HTTPStatus.NOT_FOUND
    ), "S11: seeing a subscription that is not there was not refused"


# S12: unsubscribing.


def test_unsubscribing_removes_the_subscription_and_its_postings_and_keeps_its_items(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    postings = [page_posting(app, f"gone-{number}") for number in range(4)]
    view = subscribed(app, served_feed(feeds, "gone", postings), admit=2)
    admitted = [
        posting["admission"]["item"]
        for posting in admissions_ended(app, view["id"])
        if admission_of(posting) == ADMITTED
    ]
    assert len(admitted) == 2, "S8: the two newest postings were not admitted"
    response = app.client.delete(subscription_path(view["id"]))
    assert response.status_code == HTTPStatus.NO_CONTENT, (
        f"S12: unsubscribing failed: {response.status_code} {response.text}"
    )
    assert listed(app) == [], "S12: a subscription removed is still listed"
    for path in (subscription_path(view["id"]), f"{subscription_path(view['id'])}/postings"):
        assert app.client.get(path).status_code == HTTPStatus.NOT_FOUND, (
            f"S12: a subscription removed is still found at {path}"
        )
    for entry in admitted:
        kept = answered(app.client.get(f"/api/items/{entry['id']}"), "reading an item")
        assert kept == entry, (
            "S12: an item that a removed subscription brought did not stay with its origin"
        )
    assert app.client.delete(subscription_path(view["id"])).status_code == HTTPStatus.NOT_FOUND, (
        "S12: removing a subscription that is not there was not refused"
    )


def test_postings_awaited_by_a_removed_subscription_are_not_admitted(
    subscribing_app: App, feeds: FeedStandin, held_reading: str
) -> None:
    app = subscribing_app
    postings = [page_posting(app, f"dropped-{number}") for number in range(3)]
    view = subscribed(app, served_feed(feeds, "dropped", postings), admit=3)
    awaited = [admission_of(posting) for posting in every_posting(app, view["id"])]
    assert awaited == [AWAITED] * 3, f"S1: the newest postings are not awaited: {awaited}"
    assert app.client.delete(subscription_path(view["id"])).status_code == HTTPStatus.NO_CONTENT
    app.site.let_go()
    app.settled(item_identity(LOCAL_OWNER, held_reading))
    watched(feeds)
    brought = [
        entry
        for entry in answered(app.client.get("/api/items"), "listing the items")
        if entry["origin"].get("url") in {posting.address for posting in postings}
    ]
    assert brought == [], "S12: a posting awaited by a removed subscription was admitted"


def test_a_poll_under_way_when_its_subscription_is_removed_keeps_nothing_and_logs_no_fault(
    subscribing_app: App, feeds: FeedStandin, caplog: pytest.LogCaptureFixture
) -> None:
    app = subscribing_app
    caplog.set_level(logging.INFO)
    first = [missing_posting(f"cut-{number}") for number in range(2)]
    feed = served_feed(feeds, "cut", first)
    view = subscribed(app, feed, admit=0)
    hold = feeds.hold_poll(feed)
    assert poll_by_hand(app, view["id"])["poll"]["kind"] == UNDERWAY
    held_reached(hold, "the poll by hand")
    found = [page_posting(app, f"cut-new-{number}") for number in range(3)]
    feeds.serve(feed, feeds.served[feed].model_copy(update={"postings": [*found, *first]}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        removal = pool.submit(app.client.delete, subscription_path(view["id"]))
        removed = removal.result(timeout=SETTLE_SECONDS)
    assert removed.status_code == HTTPStatus.NO_CONTENT, (
        f"S12: unsubscribing during a poll failed: {removed.status_code} {removed.text}"
    )
    hold.release()
    waited_for(feeds.running_now, lambda running: running == 0, "the end of the poll")
    watched(feeds)
    assert listed(app) == [], "S12: a poll of a removed subscription kept its subscription"
    addresses = {posting.address for posting in found}
    assert not [
        entry
        for entry in answered(app.client.get("/api/items"), "listing the items")
        if entry["origin"].get("url") in addresses
    ], "S12: a poll of a removed subscription admitted what it found"
    faults = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR or record.exc_info is not None
    ]
    assert faults == [], (
        "S12: a poll of a removed subscription wrote an error in the log: "
        f"{[record.getMessage() for record in faults]}"
    )


def test_a_poll_of_a_removed_subscription_keeps_nothing_in_one_subscribed_anew(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    first = [missing_posting(f"anew-{number}") for number in range(2)]
    feed = served_feed(feeds, "anew", first)
    view = subscribed(app, feed, admit=0)
    hold = feeds.hold_poll(feed)
    poll_by_hand(app, view["id"])
    held_reached(hold, "the poll by hand")
    assert app.client.delete(subscription_path(view["id"])).status_code == HTTPStatus.NO_CONTENT
    second = [missing_posting(f"anew-second-{number}") for number in range(2)]
    feeds.serve(feed, feeds.served[feed].model_copy(update={"postings": [*second, *first]}))
    at = feeds.clock.advance(STEP)
    anew = subscribed(app, feed, admit=0)
    feeds.serve(
        feed,
        feeds.served[feed].model_copy(
            update={"postings": [missing_posting("anew-late"), *second, *first]}
        ),
    )
    hold.release()
    waited_for(feeds.running_now, lambda running: running == 0, "the end of the poll")
    watched(feeds)
    now = viewed(app, anew["id"])
    assert (now["postings"], now["poll"]["found"], instant(now["poll"]["at"])) == (4, 4, at), (
        "S12: the poll of a removed subscription kept what it found in the subscription made "
        f"anew to its feed: {now}"
    )
    assert [posting["number"] for posting in every_posting(app, anew["id"])] == [4, 3, 2, 1], (
        "S12: the poll of a removed subscription listed postings in the one made anew"
    )


# S13: each owner's own.


SUBSCRIPTION_OPERATIONS: dict[str, Callable[[App, str], httpx2.Response]] = {
    "reading it": lambda app, found: app.client.get(subscription_path(found)),
    "setting how it follows": lambda app, found: app.client.put(
        f"{subscription_path(found)}/follows", json={"follows": "paused"}
    ),
    "polling it by hand": lambda app, found: app.client.post(f"{subscription_path(found)}/poll"),
    "seeing it": lambda app, found: app.client.put(f"{subscription_path(found)}/seen"),
    "listing its postings": lambda app, found: app.client.get(
        f"{subscription_path(found)}/postings"
    ),
    "reading a posting": lambda app, found: app.client.post(
        f"{subscription_path(found)}/postings/1/reading"
    ),
    "unsubscribing": lambda app, found: app.client.delete(subscription_path(found)),
}


def test_another_owners_subscription_is_not_found_nor_told_apart_from_none(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    feed = served_feed(feeds, "owned", [page_posting(app, "owned-0")])
    view = subscribed(app, feed, admit=0)
    postings = every_posting(app, view["id"])
    with as_owner(new_owner()):
        assert listed(app) == [], "S13: another owner's subscription is listed"
        for operation, asked in SUBSCRIPTION_OPERATIONS.items():
            of_another = refusal(asked(app, view["id"]), view["id"])
            of_none = refusal(asked(app, NOWHERE_ITEM), NOWHERE_ITEM)
            assert of_another[0] == HTTPStatus.NOT_FOUND, (
                f"S13: {operation} of another owner's subscription was not refused"
            )
            assert of_another == of_none, (
                f"S13: {operation} of another owner's subscription is told apart from one that "
                f"is not there: {of_another} against {of_none}"
            )
    watched(feeds)
    assert viewed(app, view["id"]) == view, "S13: another owner's asking changed a subscription"
    assert every_posting(app, view["id"]) == postings, (
        "S13: another owner's asking changed a subscription's postings"
    )
    assert feeds.polls_of(feed) == 0, "S13: another owner's asking polled a subscription"


def test_owners_of_subscriptions_to_one_feed_each_have_their_own_postings_items_and_counts(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    postings = [page_posting(app, f"shared-{number}") for number in range(3)]
    feed = served_feed(feeds, "shared", postings)
    mine = subscribed(app, feed, admit=1)
    admissions_ended(app, mine["id"])
    other = new_owner()
    with as_owner(other):
        theirs = subscribed(app, feed, admit=2)
        their_postings = admissions_ended(app, theirs["id"])
        their_items = answered(app.client.get("/api/items"), "listing the items")
        assert [view["id"] for view in listed(app)] == [theirs["id"]], (
            "S13: an owner's list holds another owner's subscription"
        )
        assert viewed(app, theirs["id"])["unseen"] == 2, (
            "S13: an owner's subscription does not count its own unseen items alone"
        )
    my_items = answered(app.client.get("/api/items"), "listing the items")
    assert theirs["id"] != mine["id"], "S13: two owners' subscriptions to one feed are one"
    assert len(my_items) == 1 and len(their_items) == 2, (
        "S13: the items a subscription brought are not each in its owner's library alone: "
        f"{len(my_items)} and {len(their_items)}"
    )
    assert {entry["id"] for entry in my_items}.isdisjoint(entry["id"] for entry in their_items), (
        "S13: two owners share an item that their subscriptions brought"
    )
    assert [admission_of(posting) for posting in their_postings] == [
        ADMITTED,
        ADMITTED,
        UNADMITTED,
    ], "S13: an owner's postings are admitted by another owner's subscription"
    assert viewed(app, mine["id"])["unseen"] == 1, (
        "S13: a subscription counts another owner's items as unseen"
    )
