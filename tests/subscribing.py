import time
from collections.abc import Callable
from concurrent.futures import Future, wait
from datetime import datetime
from http import HTTPStatus
from threading import Event, Thread
from typing import Any, NoReturn

import httpx2
import pytest

from infonium.subscriptions.feed import Feed, Posting
from tests.feed_standin import FEEDS_SITE, FeedStandin
from tests.notebook import instant
from tests.service import LEDGER, POLL_SECONDS, SETTLE_SECONDS, SITE, App, article

# What a test does with subscriptions through the service's endpoints, and waits for.
SUBSCRIPTIONS = "/api/subscriptions"
# The most postings a page of them holds, which a test asks for to read them all.
MOST_POSTINGS_LISTED = 200
# How many times the poller reads its clock, and how many watches pass, before a test takes it
# that the poller has looked at what is due and what waits, and has done nothing.
WATCHES = 6
HELD_READING = "held-reading"
# A known fault of the part: a poll whose feed function raises at once can stop the poller for
# good, and the service then neither answers nor stops, nor lets the tests end. A judgment of
# such polls is not run until the part's mend is merged.
FAILS_AT_ONCE = pytest.mark.xfail(
    run=False,
    reason=(
        "S5, S6: a poll whose feed function raises at once can deadlock the poller, and the "
        "service then neither answers nor stops (known fault, being mended)"
    ),
)
UNDERWAY = "underway"
SETTLED = "settled"
FAILED = "failed"
UNADMITTED = "unadmitted"
AWAITED = "awaited"
ADMITTED = "admitted"
READ = "read"
SUBSCRIBED = "subscribed"


def subscription_path(subscription_id: str) -> str:
    return f"{SUBSCRIPTIONS}/{subscription_id}"


def feed_address(name: str) -> str:
    return f"{FEEDS_SITE}/{name}.xml"


def page_address(name: str) -> str:
    return f"{SITE}/page/{name}"


def page_posting(
    app: App, name: str, title: str | None = None, published: datetime | None = None
) -> Posting:
    # A posting whose address is a page the stand-in site serves, an article long enough to be
    # read as one, so that admitting the posting reads it. Its key is its name.
    called = title or f"Posting {name}"
    app.site.pages[name] = article(f"Page of {name}", LEDGER)
    return Posting(key=name, title=called, address=page_address(name), published=published)


def missing_posting(name: str, title: str | None = None) -> Posting:
    # A posting whose address the stand-in site answers with 404, so that its reading fails at
    # once.
    return Posting(
        key=name, title=title or f"Posting {name}", address=page_address(name), published=None
    )


def served_feed(
    feeds: FeedStandin, name: str, postings: list[Posting], title: str | None = None
) -> str:
    # A feed of the postings given, in the order given, served under its address.
    address = feed_address(name)
    feeds.serve(
        address,
        Feed(title=title or f"Feed {name}", site=f"{FEEDS_SITE}/{name}", postings=postings),
    )
    return address


def subscribe(app: App, address: str, admit: int | None = None) -> httpx2.Response:
    draft: dict[str, Any] = {"address": address}
    if admit is not None:
        draft["admit"] = admit
    return app.client.post(SUBSCRIPTIONS, json=draft)


def subscribed(app: App, address: str, admit: int | None = None) -> dict[str, Any]:
    response = subscribe(app, address, admit)
    assert response.status_code == HTTPStatus.OK, (
        f"subscribing to {address} that leads to a feed was refused: {response.status_code} "
        f"{response.text[:300]}"
    )
    view: dict[str, Any] = response.json()
    return view


def answered(response: httpx2.Response, what: str) -> Any:
    assert response.status_code == HTTPStatus.OK, (
        f"{what} failed: {response.status_code} {response.text[:300]}"
    )
    return response.json()


def viewed(app: App, subscription_id: str) -> dict[str, Any]:
    view: dict[str, Any] = answered(
        app.client.get(subscription_path(subscription_id)), "reading a subscription"
    )
    return view


def listed(app: App) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = answered(app.client.get(SUBSCRIPTIONS), "listing subscriptions")
    return views


def postings_page(
    app: App, subscription_id: str, before: int | None = None, limit: int | None = None
) -> httpx2.Response:
    query: dict[str, int] = {}
    if before is not None:
        query["before"] = before
    if limit is not None:
        query["limit"] = limit
    return app.client.get(f"{subscription_path(subscription_id)}/postings", params=query)


def every_posting(app: App, subscription_id: str) -> list[dict[str, Any]]:
    # Every posting the subscription lists, newest first, read page by page.
    everything: list[dict[str, Any]] = []
    before: int | None = None
    while True:
        page: list[dict[str, Any]] = answered(
            postings_page(app, subscription_id, before, MOST_POSTINGS_LISTED),
            "listing a subscription's postings",
        )
        everything.extend(page)
        if len(page) < MOST_POSTINGS_LISTED:
            return everything
        before = page[-1]["number"]


def posting_numbered(app: App, subscription_id: str, number: int) -> dict[str, Any]:
    page: list[dict[str, Any]] = answered(
        postings_page(app, subscription_id, number + 1, 1), "listing a subscription's postings"
    )
    assert page and page[0]["number"] == number, (
        f"the posting numbered {number} is not listed: {page}"
    )
    return page[0]


def seen_in_time[Seen](look: Callable[[], Seen], done: Callable[[Seen], bool]) -> list[Seen]:
    # What is seen once it is done, or nothing where it is not done in time.
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        seen = look()
        if done(seen):
            return [seen]
        if time.monotonic() > deadline:
            return []
        time.sleep(POLL_SECONDS)


def waited_for[Seen](look: Callable[[], Seen], done: Callable[[Seen], bool], what: str) -> Seen:
    seen = seen_in_time(look, done)
    if not seen:
        pytest.fail(f"{what} did not happen in {SETTLE_SECONDS} seconds: {look()}")
    return seen[0]


def is_polled_at(view: dict[str, Any], at: datetime) -> bool:
    poll = view["poll"]
    return poll["kind"] != UNDERWAY and instant(poll["at"]) == at


def polled(app: App, subscription_id: str, at: datetime) -> dict[str, Any]:
    # The subscription once a poll of it has settled or failed at the time given.
    return waited_for(
        lambda: viewed(app, subscription_id),
        lambda view: is_polled_at(view, at),
        f"a poll of subscription {subscription_id} at {at}",
    )


def left_standing(app: App, what: str) -> NoReturn:
    # The judgment fails where the poller stopped going on, and the service is left as it
    # stands: stopping it would wait for the poller for ever.
    app.running = False
    pytest.fail(f"S5, S6: the poller did not go on: {what} in {SETTLE_SECONDS} seconds")


def polled_going_on(app: App, subscription_id: str, at: datetime) -> dict[str, Any]:
    # The subscription once a poll of it at the time given has settled or failed, where a feed
    # function that fails at once may have stopped the poller.
    seen = seen_in_time(lambda: viewed(app, subscription_id), lambda view: is_polled_at(view, at))
    if not seen:
        left_standing(app, f"no poll of subscription {subscription_id} at {at} ended")
    return seen[0]


def answered_going_on(app: App, ask: Callable[[], httpx2.Response], what: str) -> Any:
    # What the service answers to a request that a poller that stopped going on may never
    # answer. The request is sent from a thread that does not keep the tests from ending.
    answer: Future[httpx2.Response] = Future()

    def asking() -> None:
        answer.set_running_or_notify_cancel()
        try:
            answer.set_result(ask())
        except BaseException as error:
            answer.set_exception(error)

    Thread(target=asking, daemon=True).start()
    if wait([answer], timeout=SETTLE_SECONDS).not_done:
        left_standing(app, f"{what} was not answered")
    return answered(answer.result(), what)


def stopped_going_on(app: App) -> None:
    # The service stopped, where a poller that stopped going on may keep it from stopping. The
    # library is let go of on this thread, which holds its lock.
    stopping = Thread(target=app.client.__exit__, args=(None, None, None), daemon=True)
    stopping.start()
    stopping.join(SETTLE_SECONDS)
    if stopping.is_alive():
        left_standing(app, "the service did not stop")
    app.stop()


def poll_by_hand(app: App, subscription_id: str) -> dict[str, Any]:
    view: dict[str, Any] = answered_going_on(
        app,
        lambda: app.client.post(f"{subscription_path(subscription_id)}/poll"),
        "a poll by hand",
    )
    return view


def is_quiet(postings: list[dict[str, Any]]) -> bool:
    # Nothing of the postings waits, and no reading of an admitted one is under way.
    return all(
        posting["admission"]["kind"] != AWAITED
        and (
            posting["admission"]["kind"] != ADMITTED
            or posting["admission"]["item"]["reading"]["attempt"]["kind"] != UNDERWAY
        )
        for posting in postings
    )


def admissions_ended(app: App, subscription_id: str) -> list[dict[str, Any]]:
    # The postings of a subscription once none waits and no reading of one is under way.
    return waited_for(
        lambda: every_posting(app, subscription_id),
        is_quiet,
        f"the admissions of subscription {subscription_id}",
    )


def watched(feeds: FeedStandin) -> None:
    # Returns once the poller has read its clock WATCHES times more, and as many watches have
    # passed, so that what it would do by now it has begun.
    reads = feeds.clock.read_count()
    began = time.monotonic()
    waited_for(
        lambda: (feeds.clock.read_count() - reads, time.monotonic() - began),
        lambda counted: (
            counted[0] >= WATCHES and counted[1] >= WATCHES * feeds.watch.total_seconds()
        ),
        "the poller's looks at its clock",
    )


def reading_held(app: App, feeds: FeedStandin) -> str:
    # The address of the page of a posting that a subscription admitted at once, and whose
    # reading waits at the fetch of that page until the test lets it go. An item that a
    # subscription brought is being read meanwhile, so no other posting is handed over to the
    # library, and those that wait stay as they were listed.
    name = HELD_READING
    app.site.held[name] = Event()
    address = served_feed(feeds, name, [page_posting(app, name)])
    subscribed(app, address, admit=1)
    waited_for(
        lambda: app.site.hits[f"/page/{name}"],
        lambda hits: hits >= 1,
        "the reading of a posting admitted at once",
    )
    return page_address(name)


def is_underway(app: App, address: str) -> bool:
    entries: list[dict[str, Any]] = answered(app.client.get("/api/items"), "listing the items")
    return any(
        entry["origin"].get("url") == address and entry["reading"]["attempt"]["kind"] == UNDERWAY
        for entry in entries
    )


def admission_of(posting: dict[str, Any]) -> str:
    return str(posting["admission"]["kind"])


def by_number(postings: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {posting["number"]: posting for posting in postings}
