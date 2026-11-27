from collections import Counter, defaultdict, deque
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Event, Lock, get_ident

import httpx
import pytest
from pydantic import BaseModel

from infonium import api
from infonium.subscriptions.discovery import Discovered
from infonium.subscriptions.feed import Feed, UnreadableFeed
from infonium.subscriptions.fetcher import Changed, Fetched, Unchanged, Validators
from infonium.subscriptions.poller import Poller
from tests.service import SETTLE_SECONDS

# The functions by which the poller finds and polls feeds, answered in the test's process from
# feeds a test builds and changes between polls, and the clock the poller reads, which stands
# still until the test moves it. The poller is made with them as the service's own provider
# makes it with the real ones.
FEEDS_SITE = "https://feeds.test"
RSSHUB_BASE_URL = "http://rsshub.test"
# Where the clock stands when a test begins, and how long the poller waits between polls and
# between two looks at what is due. The watch is short, so that what is due is done as soon as
# the service can; a test waits for what it judges, never for the watch.
STARTING_TIME = datetime(2026, 11, 27, 8, 0, tzinfo=UTC)
INTERVAL = timedelta(hours=1)
WATCH = timedelta(seconds=0.02)
# What a feed function says that finds no feed at an address.
NO_FEED = "no feed is served at {address}"
# What a call that the test held says when the test ends without letting it go: the connection
# was cut.
CUT_AT_THE_END = "the test ended while the feed was asked for"


class Clock:
    # The poller's clock. It stands still until the test moves it, and counts how often it is
    # read, so that a test can wait until the poller has looked at the time again. Once the
    # test fails it, every read of the poller's raises the error given.
    def __init__(self, now: datetime) -> None:
        self.lock = Lock()
        self.now = now
        self.reads = 0
        self.failures: list[Exception] = []

    def __call__(self) -> datetime:
        with self.lock:
            self.reads += 1
            if self.failures:
                raise self.failures[0]
            return self.now

    def fail(self, error: Exception) -> None:
        with self.lock:
            self.failures.append(error)

    def read_count(self) -> int:
        with self.lock:
            return self.reads

    def time(self) -> datetime:
        # The time the clock shows, without counting a read of the poller's.
        with self.lock:
            return self.now

    def advance(self, by: timedelta) -> datetime:
        with self.lock:
            self.now += by
            return self.now

    def move_to(self, instant: datetime) -> datetime:
        with self.lock:
            if instant < self.now:
                raise ValueError(f"the test's clock stands at {self.now}, after {instant}")
            self.now = instant
            return self.now


class Hold:
    # The next poll of a feed waits here until the test lets it go. One that the test never
    # lets go fails as a cut connection when the test ends.
    def __init__(self) -> None:
        self.reached = Event()
        self.released = Event()
        self.cut = False

    def wait(self) -> None:
        self.reached.set()
        self.released.wait(SETTLE_SECONDS)
        if self.cut or not self.released.is_set():
            raise httpx.ConnectError(CUT_AT_THE_END)

    def is_reached(self) -> bool:
        return self.reached.wait(SETTLE_SECONDS)

    def release(self) -> None:
        self.released.set()

    def cut_off(self) -> None:
        self.cut = True
        self.released.set()


class FeedFunction(StrEnum):
    DISCOVERED = "discovered"
    FETCHED = "fetched"


class FeedCall(BaseModel):
    # One call of a feed function: which, with what, from which thread.
    function: FeedFunction
    address: str
    validators: Validators | None
    rsshub: str | None
    thread: int


# What a test plans for the next poll of a feed instead of serving it: the server says that it
# has not changed, or the poll fails with an error.
Planned = Unchanged | BaseException


class FeedStandin:
    # The feeds served now, each under its address with the validators its server gives it,
    # which change whenever the test serves the feed anew; the addresses that lead to a feed
    # elsewhere; and what the test planned for the next calls. Every call is recorded, and how
    # many polls run at once, of each feed and of all.
    def __init__(self, interval: timedelta) -> None:
        self.lock = Lock()
        self.clock = Clock(STARTING_TIME)
        self.interval = interval
        self.watch = WATCH
        self.served: dict[str, Feed] = {}
        self.versions: Counter[str] = Counter()
        self.leads: dict[str, str] = {}
        self.planned_polls: defaultdict[str, deque[Planned]] = defaultdict(deque)
        self.planned_discoveries: defaultdict[str, deque[BaseException]] = defaultdict(deque)
        self.poll_holds: dict[str, Hold] = {}
        self.held: list[Hold] = []
        self.calls: list[FeedCall] = []
        self.running: Counter[str] = Counter()
        self.most_running = 0
        self.most_running_of: Counter[str] = Counter()

    def poller(self) -> Poller:
        # The poller as the service's provider makes it, on the library and intake of the
        # service being started, with these feed functions, this clock, this interval and this
        # watch.
        return Poller(
            store=api.library_store(),
            intake=api.intake(),
            discovered=self.discovered,
            fetched=self.fetched,
            rsshub=RSSHUB_BASE_URL,
            clock=self.clock,
            interval=self.interval,
            watch=self.watch,
        )

    def serve(self, address: str, feed: Feed) -> None:
        with self.lock:
            self.served[address] = feed
            self.versions[address] += 1

    def lead(self, address: str, feed: str) -> None:
        # The address is a page, a show or a channel whose feed is served elsewhere.
        with self.lock:
            self.leads[address] = feed

    def validators_of(self, address: str) -> Validators:
        with self.lock:
            return Validators(etag=f'"{self.versions[address]}"', modified=None)

    def plan_polls(self, address: str, *planned: Planned) -> None:
        with self.lock:
            self.planned_polls[address].extend(planned)

    def fail_discovery(self, address: str, error: BaseException) -> None:
        with self.lock:
            self.planned_discoveries[address].append(error)

    def hold_poll(self, address: str) -> Hold:
        hold = Hold()
        with self.lock:
            self.poll_holds[address] = hold
            self.held.append(hold)
        return hold

    def let_go(self) -> None:
        for hold in self.held:
            hold.cut_off()

    def calls_of(self, function: FeedFunction, address: str | None = None) -> list[FeedCall]:
        with self.lock:
            return [
                call
                for call in self.calls
                if call.function == function and address in (None, call.address)
            ]

    def polls_of(self, address: str) -> int:
        return len(self.calls_of(FeedFunction.FETCHED, address))

    def running_now(self) -> int:
        with self.lock:
            return sum(self.running.values())

    def discovered(self, address: str, rsshub: str) -> Discovered:
        with self.lock:
            self.calls.append(
                FeedCall(
                    function=FeedFunction.DISCOVERED,
                    address=address,
                    validators=None,
                    rsshub=rsshub,
                    thread=get_ident(),
                )
            )
            planned = self.planned_discoveries[address]
            failure = planned.popleft() if planned else None
        if failure is not None:
            raise failure
        with self.lock:
            feed_address = self.leads.get(address, address)
            if feed_address not in self.served:
                raise UnreadableFeed(NO_FEED.format(address=address))
            feed = self.served[feed_address]
        # A feed's address may be a route of RSSHub, as the feeds part was ruled to give it,
        # which the contract's Discovered, of a web address, does not take.
        return Discovered.model_construct(
            address=feed_address,
            fetched=Changed(feed=feed, validators=self.validators_of(feed_address)),
        )

    def fetched(self, address: str, validators: Validators) -> Fetched:
        with self.lock:
            self.calls.append(
                FeedCall(
                    function=FeedFunction.FETCHED,
                    address=address,
                    validators=validators,
                    rsshub=None,
                    thread=get_ident(),
                )
            )
            self.running[address] += 1
            self.most_running = max(self.most_running, sum(self.running.values()))
            self.most_running_of[address] = max(
                self.most_running_of[address], self.running[address]
            )
            planned = self.planned_polls[address]
            answer = planned.popleft() if planned else None
            hold = self.poll_holds.pop(address, None)
        try:
            if hold is not None:
                hold.wait()
            match answer:
                case BaseException():
                    raise answer
                case Unchanged():
                    return answer
                case _:
                    with self.lock:
                        feed = self.served[address]
                    return Changed(feed=feed, validators=self.validators_of(address))
        finally:
            with self.lock:
                self.running[address] -= 1


def held_reached(hold: Hold, what: str) -> None:
    if not hold.is_reached():
        pytest.fail(f"{what} was not asked for in {SETTLE_SECONDS} seconds")
