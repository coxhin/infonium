import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import timedelta
from functools import partial
from pathlib import Path
from threading import Event, Lock, Thread, get_ident
from typing import Any

import httpx2
import pytest
import sqlalchemy
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from infonium import api
from infonium.library import LIBRARY_FILE, LOCAL_OWNER, LibraryStore, item_identity
from infonium.search import WORDS_TABLE, indexed_block_table, indexed_document_table
from tests.feed_standin import FeedFunction, FeedStandin, held_reached
from tests.library_before_digest_styles import ANSWERS, DATA_FOLDER, KEPT_DATA, LibraryAnswers
from tests.service import SETTLE_SECONDS, App, as_owner, keep_sources, new_owner
from tests.subscribing import (
    ADMITTED,
    AWAITED,
    READ,
    admission_of,
    admissions_ended,
    answered,
    every_posting,
    is_quiet,
    listed,
    missing_posting,
    page_posting,
    poll_by_hand,
    polled,
    postings_page,
    served_feed,
    subscribed,
    subscription_path,
    viewed,
    waited_for,
    watched,
)

# The judgments of S14 to S16 of the subscriptions' brief: what is kept over a restart and
# beside a library kept before subscriptions, what a service cut off anywhere leaves, and the
# shape of the work that the lists and a poll ask of the library.
STEP = timedelta(seconds=1)
KILLS = settings(max_examples=8, suppress_health_check=[HealthCheck.function_scoped_fixture])
# The tables of a library kept before subscriptions whose rows a later service rewrites on
# purpose: the digests, which took styles after that library was kept.
REWRITTEN_TABLES = frozenset({"digest"})
# The tables of the index of blocks, which brings itself up to date with the documents at every
# start: the documents it indexed, their blocks, and the words of the blocks with the tables
# that the words' table keeps beside it.
INDEX_TABLES = (indexed_document_table.name, indexed_block_table.name, WORDS_TABLE)
# Where SQLite keeps the last number it gave in each table that numbers its rows.
SEQUENCE_TABLE = "sqlite_sequence"
VIRTUAL_TABLE = "CREATE VIRTUAL TABLE"
STATEMENT_EVENT = "before_cursor_execute"
COMMIT_EVENT = "commit"
# The statements that change what the library holds.
WRITES = ("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "ALTER", "DROP")
WROTE = "wrote"
# How long a stop may take before the test takes it that it waits for what the test holds.
STOP_SECONDS = 5


def library_file(tmp_path: Path) -> Path:
    return tmp_path / DATA_FOLDER / LIBRARY_FILE


def tables_of(file: Path) -> dict[str, str]:
    # Each table of the file by its name, with the statement that made it.
    with closing(sqlite3.connect(file)) as connection:
        return dict(
            connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        )


def rows_of(file: Path, tables: list[str]) -> dict[str, list[tuple[Any, ...]]]:
    # The rows of the tables given, but the counters that SQLite keeps of the index's tables.
    with closing(sqlite3.connect(file)) as connection:
        return {
            table: sorted(
                (
                    row
                    for row in connection.execute(f'SELECT * FROM "{table}"').fetchall()
                    if table != SEQUENCE_TABLE or not str(row[0]).startswith(INDEX_TABLES)
                ),
                key=repr,
            )
            for table in tables
        }


def copied_library(source_file: Path, copy: Path) -> None:
    # What a library's file holds of every commit, as another connection reads it now, written
    # into another through SQLite, so that connections still open on that one see it whole.
    with closing(sqlite3.connect(source_file)) as source, closing(sqlite3.connect(copy)) as kept:
        source.backup(kept)


def started_on(
    start_app: Callable[..., App], feeds: FeedStandin, app: App, copy: Path, tmp_path: Path
) -> App:
    # The service stopped, and started again on the library as the copy holds it: what a service
    # killed when the copy was made leaves to the next start.
    app.stop()
    app.store.engine.dispose()
    copied_library(copy, library_file(tmp_path))
    return start_app(poller=feeds.poller)


def everything_shown(app: App, owner: str) -> dict[str, Any]:
    # What the service answers about the owner's subscriptions, their postings and the items.
    with as_owner(owner):
        views = listed(app)
        return {
            "subscriptions": views,
            "each subscription": [viewed(app, view["id"]) for view in views],
            "postings": [every_posting(app, view["id"]) for view in views],
            "items": answered(app.client.get("/api/items"), "listing the items"),
        }


def stopped_while_held(app: App) -> None:
    # Stops the service while what the stand-in site holds waits. A stop that waits for it ends
    # once it is let go. The library is let go of on this thread, which holds its lock.
    stopping = Thread(target=app.client.__exit__, args=(None, None, None), daemon=True)
    stopping.start()
    stopping.join(STOP_SECONDS)
    if stopping.is_alive():
        app.site.let_go()
        stopping.join(SETTLE_SECONDS)
    assert not stopping.is_alive(), "the service did not stop"
    app.stop()


# S14: kept over a restart, beside an older library.


def test_subscriptions_postings_and_what_was_seen_are_kept_over_a_restart(
    start_app: Callable[..., App], feeds: FeedStandin
) -> None:
    app = start_app(poller=feeds.poller)
    other = new_owner()
    postings = [page_posting(app, f"kept-{number}") for number in range(4)]
    feed = served_feed(feeds, "kept", postings)
    mine = subscribed(app, feed, admit=2)
    admitted = [
        posting["admission"]["item"]["id"]
        for posting in admissions_ended(app, mine["id"])
        if admission_of(posting) == ADMITTED
    ]
    assert len(admitted) == 2, "S8: the two newest postings were not admitted"
    answered(app.client.put(f"/api/items/{admitted[0]}/seen"), "seeing an item")
    feeds.clock.advance(STEP)
    paused = subscribed(app, served_feed(feeds, "kept-paused", [missing_posting("paused-0")]), 0)
    answered(
        app.client.put(f"{subscription_path(paused['id'])}/follows", json={"follows": "paused"}),
        "pausing",
    )
    feeds.serve(feed, feeds.served[feed].model_copy(update={"title": "Renamed"}))
    at = feeds.clock.advance(STEP)
    poll_by_hand(app, mine["id"])
    polled(app, mine["id"], at)
    with as_owner(other):
        subscribed(app, served_feed(feeds, "kept-theirs", [missing_posting("theirs-0")]), 0)
    before = {owner: everything_shown(app, owner) for owner in (LOCAL_OWNER, other)}
    polls = len(feeds.calls_of(FeedFunction.FETCHED))
    app.stop()
    again = start_app(poller=feeds.poller)
    watched(feeds)
    for owner in (LOCAL_OWNER, other):
        after = everything_shown(again, owner)
        for what, shown in before[owner].items():
            assert after[what] == shown, (
                f"S14: the {what} of an owner are not as they were before the service started again"
            )
    assert len(feeds.calls_of(FeedFunction.FETCHED)) == polls, (
        "S5, S14: starting again polled a subscription that was not due"
    )


def test_postings_awaited_when_the_service_stops_are_admitted_after_it_starts(
    start_app: Callable[..., App], feeds: FeedStandin
) -> None:
    app = start_app(poller=feeds.poller)
    postings = [page_posting(app, f"waiting-{number}") for number in range(3)]
    app.site.held[postings[0].key] = Event()
    view = subscribed(app, served_feed(feeds, "waiting", postings), admit=3)
    waited_for(
        lambda: app.site.hits[f"/page/{postings[0].key}"], lambda hits: hits >= 1, "the reading"
    )
    admissions = [admission_of(posting) for posting in every_posting(app, view["id"])]
    assert admissions == [ADMITTED, AWAITED, AWAITED], (
        f"S8: the newest posting was not admitted first and the others left waiting: {admissions}"
    )
    stopped_while_held(app)
    again = start_app(poller=feeds.poller)
    shown = admissions_ended(again, view["id"])
    assert [posting["admission"]["item"]["reading"]["kind"] for posting in shown] == [READ] * 3, (
        "S14, S15: postings admitted or awaited when the service stopped were not read after it "
        f"started: {shown}"
    )
    items = answered(again.client.get("/api/items"), "listing the items")
    assert sorted(entry["id"] for entry in items) == sorted(
        item_identity(LOCAL_OWNER, posting.address) for posting in postings
    ), f"S15: the postings did not lead to one item each over a restart: {items}"
    # A reading that the stopped service's intake still holds ends here, not in a later test.
    again.site.let_go()
    watched(feeds)


def test_a_library_kept_before_subscriptions_opens_with_what_it_held_unchanged(
    start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path
) -> None:
    answers = LibraryAnswers.model_validate_json(ANSWERS.read_text(encoding="utf-8"))
    shutil.copytree(KEPT_DATA, tmp_path / DATA_FOLDER)
    file = library_file(tmp_path)
    tables = tables_of(file)
    compared = [
        table
        for table, made in tables.items()
        if table not in REWRITTEN_TABLES
        and not table.startswith(INDEX_TABLES)
        and not made.startswith(VIRTUAL_TABLE)
    ]
    held = rows_of(file, compared)
    assert held["item"], "the library kept before subscriptions holds no items"
    app = start_app(language=answers.language, poller=feeds.poller)
    assert listed(app) == [], "S14: a library kept before subscriptions lists subscriptions"
    items = answered(app.client.get("/api/items"), "listing the items")
    assert items and all(entry["seen"] is None for entry in items), (
        "S11, S14: an item of a library kept before subscriptions is said to have been seen"
    )
    view = subscribed(app, served_feed(feeds, "older", [missing_posting("older-0")]), admit=0)
    assert view["postings"] == 1, "S14: a library kept before subscriptions cannot subscribe"
    app.stop()
    app.store.engine.dispose()
    now = tables_of(file)
    for table in compared:
        assert now[table] == tables[table], (
            f"S14: the service changed the table {table} of a library kept before subscriptions"
        )
    after = rows_of(file, compared)
    for table in compared:
        assert after[table] == held[table], (
            f"S14: the service changed the rows of {table} in a library kept before subscriptions"
        )


# S15: cut off anywhere, and nothing is lost or doubled.


class KilledAt:
    # The library as a service left it before each commit that changed it, while it subscribed
    # to a feed, admitted postings, polled the feed and admitted what the poll found: each the
    # library that a kill at that moment leaves. And the service that runs now.
    def __init__(self, start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path) -> None:
        self.start_app = start_app
        self.feeds = feeds
        self.tmp_path = tmp_path
        self.app = start_app(poller=feeds.poller)
        self.copies: list[Path] = []
        self.lock = Lock()
        first = [page_posting(self.app, f"killed-{number}") for number in range(3)]
        self.feed = served_feed(feeds, "killed", first)
        self.later = [page_posting(self.app, f"killed-later-{number}") for number in range(2)]
        self.addresses = {posting.address for posting in [*first, *self.later]}
        engine = self.app.store.engine
        sqlalchemy.event.listen(engine, STATEMENT_EVENT, self.heard)
        sqlalchemy.event.listen(engine, COMMIT_EVENT, self.committing)
        view = subscribed(self.app, self.feed, admit=2)
        admissions_ended(self.app, view["id"])
        feeds.serve(
            self.feed,
            feeds.served[self.feed].model_copy(update={"postings": [*self.later, *first]}),
        )
        at = feeds.clock.advance(STEP)
        poll_by_hand(self.app, view["id"])
        polled(self.app, view["id"], at)
        admissions_ended(self.app, view["id"])
        sqlalchemy.event.remove(engine, STATEMENT_EVENT, self.heard)
        sqlalchemy.event.remove(engine, COMMIT_EVENT, self.committing)

    def heard(
        self,
        connection: sqlalchemy.Connection,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith(WRITES):
            connection.info[WROTE] = True

    def committing(self, connection: sqlalchemy.Connection) -> None:
        if connection.info.pop(WROTE, False):
            with self.lock:
                copy = self.tmp_path / f"killed-{len(self.copies)}.sqlite"
                copied_library(library_file(self.tmp_path), copy)
                self.copies.append(copy)

    def started_on(self, number: int) -> App:
        self.app = started_on(
            self.start_app, self.feeds, self.app, self.copies[number], self.tmp_path
        )
        return self.app


@pytest.fixture
def killed_at(start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path) -> KilledAt:
    return KilledAt(start_app, feeds, tmp_path)


@given(moment=st.floats(0, 1, exclude_max=True))
@KILLS
def test_a_service_killed_at_any_commit_starts_again_with_nothing_lost_or_doubled(
    killed_at: KilledAt, moment: float
) -> None:
    copies = len(killed_at.copies)
    assert copies >= 10, f"the service's work committed only {copies} changes to the library"
    app = killed_at.started_on(int(moment * copies))
    views = listed(app)
    assert len(views) <= 1, f"S15: one feed was subscribed to twice: {views}"
    if not views:
        assert answered(app.client.get("/api/items"), "listing the items") == [], (
            "S15: a kill before a subscription was kept left items"
        )
        return
    subscription = views[0]["id"]
    waiting = {
        posting["number"]
        for posting in every_posting(app, subscription)
        if admission_of(posting) != "unadmitted"
    }
    shown = admissions_ended(app, subscription)
    numbers = [posting["number"] for posting in shown]
    assert numbers == list(range(len(shown), 0, -1)) and len(shown) in {3, 5}, (
        f"S15: after a kill, the postings are not listed once each, numbered from 1: {numbers}"
    )
    addresses = [posting["address"] for posting in shown]
    assert len(set(addresses)) == len(addresses) and set(addresses) <= killed_at.addresses, (
        "S15: after a kill, a posting is listed twice or one is listed that no feed lists"
    )
    admitted = {
        posting["number"]: posting["admission"]["item"]
        for posting in shown
        if admission_of(posting) == ADMITTED
    }
    assert waiting <= set(admitted), (
        f"S15: after a kill, postings that were awaited were forgotten: {waiting - set(admitted)}"
    )
    for number, item in admitted.items():
        address = next(posting["address"] for posting in shown if posting["number"] == number)
        assert (item["id"], item["reading"]["kind"]) == (
            item_identity(LOCAL_OWNER, address),
            READ,
        ), (
            f"S15: after a kill, a posting was not admitted as the one item of its address, read: "
            f"{item}"
        )
    items = answered(app.client.get("/api/items"), "listing the items")
    assert len({entry["id"] for entry in items}) == len(items) == len(admitted), (
        f"S15: after a kill, the postings admitted do not lead to one item each: {items}"
    )


def test_a_poll_cut_off_by_a_kill_is_begun_again_when_the_service_starts(
    start_app: Callable[..., App], feeds: FeedStandin, tmp_path: Path
) -> None:
    app = start_app(poller=feeds.poller)
    first = [missing_posting(f"cut-poll-{number}") for number in range(2)]
    feed = served_feed(feeds, "cut-poll", first)
    view = subscribed(app, feed, admit=0)
    found = [missing_posting(f"cut-poll-new-{number}") for number in range(3)]
    feeds.serve(feed, feeds.served[feed].model_copy(update={"postings": [*found, *first]}))
    hold = feeds.hold_poll(feed)
    poll_by_hand(app, view["id"])
    held_reached(hold, "the poll by hand")
    copy = tmp_path / "cut-poll.sqlite"
    copied_library(library_file(tmp_path), copy)
    hold.release()
    waited_for(feeds.running_now, lambda running: running == 0, "the end of the poll")
    admissions_ended(app, view["id"])
    again = started_on(start_app, feeds, app, copy, tmp_path)
    shown = waited_for(
        lambda: every_posting(again, view["id"]),
        lambda postings: len(postings) == 5,
        "the poll cut off by a kill, begun again",
    )
    assert feeds.polls_of(feed) == 2, (
        "S5: a poll cut off by the service's stopping was not begun again when it started"
    )
    assert [posting["number"] for posting in shown] == [5, 4, 3, 2, 1], (
        "S15: a poll begun again after a kill did not list what it found once"
    )


# S16: the work the lists and a poll ask of the library does not grow with what they list.


@contextmanager
def statements_heard(store: LibraryStore) -> Iterator[list[int]]:
    # The threads of the statements sent to the library meanwhile, one for each statement.
    heard: list[int] = []

    def hear(*sent: Any) -> None:
        heard.append(get_ident())

    sqlalchemy.event.listen(store.engine, STATEMENT_EVENT, hear)
    try:
        yield heard
    finally:
        sqlalchemy.event.remove(store.engine, STATEMENT_EVENT, hear)


class Requests:
    # Counts the statements that the service sends to the library to answer one request: those
    # sent on the thread that opened the library for it.
    def __init__(self, app: App, monkeypatch: pytest.MonkeyPatch) -> None:
        self.app = app
        self.threads: set[int] = set()
        monkeypatch.setattr(api, "library_store", self.opened)

    def opened(self) -> LibraryStore:
        self.threads.add(get_ident())
        return self.app.store

    def statements(self, ask: Callable[[], httpx2.Response]) -> int:
        self.threads.clear()
        with statements_heard(self.app.store) as heard:
            answered(ask(), "a request whose statements are counted")
        assert self.threads, "the request did not open the library as the test counts it"
        return sum(thread in self.threads for thread in heard)


def subscribed_with_items(app: App, feeds: FeedStandin, name: str, count: int, admit: int) -> str:
    # A subscription of the postings given, the newest of them admitted and their readings
    # ended.
    postings = [missing_posting(f"{name}-{number}") for number in range(count)]
    view = subscribed(app, served_feed(feeds, name, postings), admit=admit)
    waited_for(
        lambda: answered(postings_page(app, view["id"], limit=admit), "the newest postings"),
        is_quiet,
        f"the admissions of the newest {admit} postings",
    )
    return str(view["id"])


def test_listing_subscriptions_asks_the_library_the_same_however_many_there_are(
    subscribing_app: App, feeds: FeedStandin, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = subscribing_app
    requests = Requests(app, monkeypatch)
    for number in range(3):
        subscribed_with_items(app, feeds, f"few-{number}", 3, admit=1)
    few = requests.statements(lambda: app.client.get("/api/subscriptions"))
    for number in range(40):
        subscribed(app, served_feed(feeds, f"many-{number}", [missing_posting(f"m-{number}")]), 0)
    many = requests.statements(lambda: app.client.get("/api/subscriptions"))
    assert many == few, (
        f"S2, S16: listing 43 subscriptions with their counts sent {many} statements to the "
        f"library, and listing 3 sent {few}: the list is not read in one query"
    )


def test_a_page_of_postings_asks_the_library_the_same_however_many_it_holds_or_admitted(
    subscribing_app: App, feeds: FeedStandin, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = subscribing_app
    requests = Requests(app, monkeypatch)
    small = subscribed_with_items(app, feeds, "small", 60, admit=1)
    large = subscribed_with_items(app, feeds, "large", 2000, admit=10)
    counted = {
        name: requests.statements(partial(postings_page, app, subscription, None, limit))
        for name, subscription, limit in (
            ("5 of 60 postings, 1 admitted", small, 5),
            ("50 of 2,000 postings, 10 admitted", large, 50),
        )
    }
    assert len(set(counted.values())) == 1, (
        "S10, S16: a page of postings sent statements to the library by the postings it holds, "
        f"those listed or the items admitted: {counted}"
    )


def test_a_poll_that_finds_nothing_new_asks_the_library_the_same_however_long_the_feed(
    subscribing_app: App, feeds: FeedStandin
) -> None:
    app = subscribing_app
    counted: dict[int, int] = {}
    for count in (20, 2000):
        name = f"long-{count}"
        postings = [missing_posting(f"{name}-{number}") for number in range(count)]
        feed = served_feed(feeds, name, postings)
        view = subscribed(app, feed, admit=0)
        at = feeds.clock.advance(STEP)
        with statements_heard(app.store) as heard:
            poll_by_hand(app, view["id"])
            assert polled(app, view["id"], at)["poll"]["found"] == 0, (
                "S3: a poll of a feed whose postings are all listed found some"
            )
        thread = feeds.calls_of(FeedFunction.FETCHED, feed)[-1].thread
        counted[count] = heard.count(thread)
        assert counted[count] > 0, (
            "the poll sent nothing to the library from the thread that fetched its feed, where "
            "the test counts its work"
        )
    assert counted[2000] == counted[20], (
        f"S16: a poll that found nothing new sent the library {counted[2000]} statements for a "
        f"feed of 2,000 postings and {counted[20]} for one of 20"
    )


def test_listing_items_asks_the_library_the_same_however_many_there_are_and_were_seen(
    subscribing_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = subscribing_app
    requests = Requests(app, monkeypatch)
    counted: list[int] = []
    for count in (2, 40):
        kept = keep_sources(
            app.store, LOCAL_OWNER, [(f"Kept {n}", ["Text."]) for n in range(count)]
        )
        for source in kept[: count // 2]:
            answered(app.client.put(f"/api/items/{source.item}/seen"), "seeing an item")
        counted.append(requests.statements(lambda: app.client.get("/api/items")))
    assert counted[0] == counted[1], (
        f"S11, S16: listing the items with when each was seen sent {counted[1]} statements for "
        f"42 items and {counted[0]} for 2: they are not read in one query"
    )
