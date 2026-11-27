import gzip
import random
import tempfile
import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import partial
from pathlib import Path
from threading import Thread

import httpx
import psutil
import pytest
from pydantic import BaseModel

from infonium.subscriptions import program
from infonium.subscriptions.discovery import discovered_feed
from infonium.subscriptions.feed import UnreadableFeed
from infonium.subscriptions.fetcher import Validators, fetched_feed
from infonium.subscriptions.program import discovered_apart, polled_apart
from tests.conftest import PROXY_VARIABLES
from tests.feed_server import (
    ScriptedHandler,
    ScriptedServer,
    broken_off,
    moved,
    poured,
    scripted_server,
    sent,
    trickled,
)
from tests.feed_writing import BOMBS, FeedForm, bomb_feed

# Judgments of polled_apart and discovered_apart, which poll and find a feed in a fetcher program
# of their own: over a server on a port of this machine that the program really connects to,
# they answer as the functions in the service's process do, end in UnreadableFeed and in nothing
# else, hold the program to its bounds of memory and time, and leave nothing behind.
pytestmark = [pytest.mark.apart, pytest.mark.enable_socket]

PORT = 9146
# A port of this machine on which nothing listens.
CLOSED_PORT = 9149
CLOSED = f"http://127.0.0.1:{CLOSED_PORT}"
RSS_KIND = "application/rss+xml"
# The time the program may take, lowered by the name the module gives it so that a trickle
# meets it in seconds; the program starts in a quarter of a second on a quiet machine.
LOWERED_FETCHER_SECONDS = 6.0
OVERRUN_SECONDS = 3.0
# How far above its bound of memory the program may be seen, sampled every few milliseconds.
SAMPLED_OVER_BYTES = 32 * 1024**2
# What the service's process may grow by over many polls apart, and its open files.
GROWTH_BYTES = 16 * 1024**2
GROWTH_FILES = 4
POLLS_FOR_GROWTH = 25
# More than a feed may hold in the fetcher program, which keeps its own bounds.
BEYOND_A_FEED_BYTES = 21 * 1024**2
FEED = (
    b'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>Night trains'
    b"</title><link>/</link><item><title>The north line</title><link>/p/1</link>"
    b'<guid isPermaLink="false">tag:nighttrains.test,2026:1</guid></item></channel></rss>'
)
FEED_OPENING = FEED.removesuffix(b"</channel></rss>")
ETAG = '"night-1"'
MODIFIED = "Wed, 25 Nov 2026 10:00:00 GMT"
PAGE = (
    b"<!doctype html><html><head><title>Night trains</title>"
    b'<link rel="alternate" type="application/rss+xml" href="/feed.xml"></head>'
    b"<body><p>The timetable of the night trains.</p></body></html>"
)
BARE_PAGE = (
    b"<!doctype html><html><head><title>Night</title></head><body><p>No feed.</p></body></html>"
)


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ScriptedServer]:
    # The server the program connects to, reached without a proxy; the program's files kept in
    # a folder of the test's own; its time lowered.
    for name in PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    monkeypatch.setattr(program, "FETCHER_LIMIT_SECONDS", LOWERED_FETCHER_SECONDS)
    with scripted_server(PORT) as scripted:
        yield scripted


def assert_left_nothing(what: str) -> None:
    running = psutil.Process().children(recursive=True)
    assert not running, f"{what} left {running} running"
    left = sorted(Path(str(tempfile.tempdir)).iterdir())
    assert not left, f"{what} left {left} behind"


def outcome(call: Callable[[], object], what: str) -> str:
    # How a poll or a discovery ended: what it gave, or the sentence it was refused with; any
    # other end fails the judgment.
    try:
        ended = f"gave {call()!r}"
    except UnreadableFeed as refused:
        ended = f"refused: {refused}"
    assert_left_nothing(what)
    return ended


def refused_apart(call: Callable[[], object], what: str) -> str:
    with pytest.raises(UnreadableFeed) as refused:
        made = call()
        pytest.fail(f"{what} gave {made}, not UnreadableFeed")
    assert_left_nothing(what)
    return str(refused.value)


class Peaks(BaseModel):
    # The most memory the programs this process started held, as sampled.
    rss: int = 0


@contextmanager
def programs_watched() -> Iterator[Peaks]:
    peaks = Peaks()
    watching = [True]

    def watch() -> None:
        me = psutil.Process()
        while watching[0]:
            for child in me.children(recursive=True):
                # A program may end between being listed and being measured.
                with suppress(psutil.NoSuchProcess):
                    peaks.rss = max(peaks.rss, child.memory_info().rss)
            time.sleep(0.005)

    watcher = Thread(target=watch)
    watcher.start()
    try:
        yield peaks
    finally:
        watching[0] = False
        watcher.join()


def conditional(content: bytes, etag: str) -> Callable[[ScriptedHandler], None]:
    # A feed that answers 304 to its own ETag.
    def script(handler: ScriptedHandler) -> None:
        if handler.headers.get("If-None-Match") == etag:
            handler.head(304, {"ETag": etag, "Content-Length": "0"})
            return
        sent(content, RSS_KIND, extra=(("ETag", etag), ("Last-Modified", MODIFIED)))(handler)

    return script


# Each case scripts the server and gives the call apart and the call in the service's process.
Calls = tuple[Callable[[], object], Callable[[], object]]


def a_feed(server: ScriptedServer) -> Calls:
    server.scripts["/feed.xml"] = conditional(FEED, ETAG)
    at = server.address("/feed.xml")
    return (
        lambda: polled_apart(at, Validators(), CLOSED),
        lambda: fetched_feed(at, Validators()),
    )


def a_feed_unchanged(server: ScriptedServer) -> Calls:
    server.scripts["/feed.xml"] = conditional(FEED, ETAG)
    at = server.address("/feed.xml")
    held = Validators(etag=ETAG, modified=MODIFIED)
    return lambda: polled_apart(at, held, CLOSED), lambda: fetched_feed(at, held)


def a_feed_moved(server: ScriptedServer) -> Calls:
    server.scripts["/old.xml"] = moved(server.address("/moved.xml"))
    server.scripts["/moved.xml"] = moved(server.address("/feed.xml"))
    server.scripts["/feed.xml"] = conditional(FEED, ETAG)
    at = server.address("/old.xml")
    return lambda: polled_apart(at, Validators(), CLOSED), lambda: fetched_feed(at, Validators())


def a_missing_feed(server: ScriptedServer) -> Calls:
    at = server.address("/missing.xml")
    return lambda: polled_apart(at, Validators(), CLOSED), lambda: fetched_feed(at, Validators())


def a_page_naming_its_feed(server: ScriptedServer) -> Calls:
    server.scripts["/"] = sent(PAGE, "text/html")
    server.scripts["/feed.xml"] = conditional(FEED, ETAG)
    at = server.address("/")
    return lambda: discovered_apart(at, CLOSED), lambda: discovered_feed(at, CLOSED)


def a_page_naming_none(server: ScriptedServer) -> Calls:
    server.scripts["/"] = sent(BARE_PAGE, "text/html")
    at = server.address("/")
    rsshub = server.address("")
    return lambda: discovered_apart(at, rsshub), lambda: discovered_feed(at, rsshub)


def a_route_of_rsshub(server: ScriptedServer) -> Calls:
    server.scripts["/nighttrains/line/north"] = conditional(FEED, ETAG)
    rsshub = server.address("")
    route = "rsshub://nighttrains/line/north"
    return (
        lambda: discovered_apart(route, rsshub),
        lambda: discovered_feed(route, rsshub),
    )


def a_route_polled(server: ScriptedServer) -> Calls:
    server.scripts["/nighttrains/line/north"] = conditional(FEED, ETAG)
    rsshub = server.address("")
    route = "rsshub://nighttrains/line/north"
    served = server.address("/nighttrains/line/north")
    return (
        lambda: polled_apart(route, Validators(), rsshub),
        lambda: fetched_feed(served, Validators()),
    )


ALIKE = {
    "a poll of a feed": a_feed,
    "a poll of a feed unchanged since": a_feed_unchanged,
    "a poll of a feed moved twice": a_feed_moved,
    "a poll of a feed that is missing": a_missing_feed,
    "a poll of a route of RSSHub": a_route_polled,
    "a discovery of a page that names its feed": a_page_naming_its_feed,
    "a discovery of a page that names none": a_page_naming_none,
    "a discovery of a route of RSSHub": a_route_of_rsshub,
}


@pytest.mark.parametrize("case", sorted(ALIKE))
def test_apart_a_poll_or_a_discovery_ends_as_in_the_services_process(
    server: ScriptedServer, case: str
) -> None:
    apart, in_process = ALIKE[case](server)
    ended_apart = outcome(apart, f"{case} apart")
    ended_in_process = outcome(in_process, f"{case} in the service's process")
    assert ended_apart == ended_in_process, (
        f"{case} apart {ended_apart}, where in the service's process it {ended_in_process}"
    )


# Answers that give no feed, each scripted on the server at /feed.xml.
GARBAGE = random.Random(9146).randbytes(64 * 1024)
REFUSED_ANSWERS: dict[str, Callable[[ScriptedServer], None]] = {
    "a connection broken in the middle": lambda s: s.scripts.update(
        {"/feed.xml": broken_off(FEED_OPENING, RSS_KIND, 64 * 1024)}
    ),
    "an answer too large": lambda s: s.scripts.update(
        {"/feed.xml": poured(BEYOND_A_FEED_BYTES, RSS_KIND)}
    ),
    "a body that is garbage": lambda s: s.scripts.update({"/feed.xml": sent(GARBAGE, RSS_KIND)}),
    "a gzip bomb": lambda s: s.scripts.update(
        {
            "/feed.xml": sent(
                gzip.compress(b" " * BEYOND_A_FEED_BYTES),
                RSS_KIND,
                extra=(("Content-Encoding", "gzip"),),
            )
        }
    ),
    "a deflate bomb": lambda s: s.scripts.update(
        {
            "/feed.xml": sent(
                zlib.compress(b" " * BEYOND_A_FEED_BYTES),
                RSS_KIND,
                extra=(("Content-Encoding", "deflate"),),
            )
        }
    ),
}
OPERATIONS: dict[str, Callable[[str], object]] = {
    "poll": lambda at: polled_apart(at, Validators(), CLOSED),
    "discovery": lambda at: discovered_apart(at, CLOSED),
}


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
@pytest.mark.parametrize("answer", sorted(REFUSED_ANSWERS))
def test_apart_an_answer_that_gives_no_feed_ends_in_unreadable_feed_alone(
    server: ScriptedServer, answer: str, operation: str
) -> None:
    REFUSED_ANSWERS[answer](server)
    at = server.address("/feed.xml")
    told = refused_apart(lambda: OPERATIONS[operation](at), f"a {operation} apart of {answer}")
    assert at in told, f"a {operation} apart of {answer} was refused as {told!r}, naming no address"


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_apart_a_closed_port_ends_in_unreadable_feed_alone(
    server: ScriptedServer, operation: str
) -> None:
    at = f"{CLOSED}/feed.xml"
    with pytest.raises(httpx.ConnectError):
        httpx.get(at)
    told = refused_apart(lambda: OPERATIONS[operation](at), f"a {operation} apart of a closed port")
    assert at in told, f"a {operation} apart of a closed port was refused as {told!r}"


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_apart_a_trickle_ends_in_unreadable_feed_within_the_time_the_program_may_take(
    server: ScriptedServer, operation: str
) -> None:
    server.scripts["/feed.xml"] = trickled(FEED_OPENING, RSS_KIND, 10 * LOWERED_FETCHER_SECONDS)
    at = server.address("/feed.xml")
    began = time.monotonic()
    refused_apart(lambda: OPERATIONS[operation](at), f"a {operation} apart of a trickle")
    took = time.monotonic() - began
    assert took <= LOWERED_FETCHER_SECONDS + OVERRUN_SECONDS, (
        f"a {operation} apart of a trickle was given up after {took:.1f} seconds, beyond the "
        f"{LOWERED_FETCHER_SECONDS} the program may take"
    )


# A title of a million inline elements, which the reading parses as HTML: it costs more memory
# than the program may hold.
COSTLY = (
    b'<rss version="2.0"><channel><title>Costly</title><item><title>'
    + b"&lt;b&gt;a&lt;/b&gt;" * 1_000_000
    + b"</title><link>https://feeds.test/p/1</link></item></channel></rss>"
)
HEAVY_ANSWERS = {
    "a title of a million elements": COSTLY,
    **{
        f"{bomb} in {form}": bomb_feed(*BOMBS[bomb], form).encode()
        for bomb in BOMBS
        for form in (FeedForm.RSS, FeedForm.ATOM)
    },
}


@pytest.mark.parametrize("heavy", sorted(HEAVY_ANSWERS))
def test_apart_a_reading_that_costs_much_ends_within_the_programs_bounds(
    server: ScriptedServer, heavy: str
) -> None:
    server.scripts["/feed.xml"] = sent(HEAVY_ANSWERS[heavy], RSS_KIND)
    at = server.address("/feed.xml")
    began = time.monotonic()
    with programs_watched() as peaks:
        ended = outcome(lambda: polled_apart(at, Validators(), CLOSED), f"a poll apart of {heavy}")
    took = time.monotonic() - began
    assert took <= LOWERED_FETCHER_SECONDS + OVERRUN_SECONDS, (
        f"a poll apart of {heavy} ended after {took:.1f} seconds"
    )
    assert peaks.rss <= program.FETCHER_MEMORY_BYTES + SAMPLED_OVER_BYTES, (
        f"a poll apart of {heavy} held {peaks.rss / 1024**2:.0f} MiB, beyond the "
        f"{program.FETCHER_MEMORY_BYTES / 1024**2:.0f} MiB the program may hold; it {ended}"
    )
    after = outcome(lambda: polled_apart(at, Validators(), CLOSED), "a poll apart after it")
    assert after == ended, f"a poll apart of {heavy} {ended}, and the next {after}"


def test_apart_polls_leave_the_services_memory_and_open_files_as_they_were(
    server: ScriptedServer,
) -> None:
    server.scripts["/feed.xml"] = conditional(FEED, ETAG)
    server.scripts["/broken.xml"] = broken_off(FEED_OPENING, RSS_KIND, 64 * 1024)
    me = psutil.Process()
    polls = [server.address("/feed.xml"), server.address("/broken.xml")]
    for at in polls:
        outcome(partial(polled_apart, at, Validators(), CLOSED), "a poll apart")
    before, files_before = me.memory_info().rss, me.num_fds()
    for number in range(POLLS_FOR_GROWTH):
        at = polls[number % len(polls)]
        outcome(partial(polled_apart, at, Validators(), CLOSED), "a poll apart")
    grown, files_grown = me.memory_info().rss - before, me.num_fds() - files_before
    assert grown <= GROWTH_BYTES, (
        f"{POLLS_FOR_GROWTH} polls apart grew the service's process by {grown / 1024**2:.1f} MiB"
    )
    assert files_grown <= GROWTH_FILES, (
        f"{POLLS_FOR_GROWTH} polls apart left the service's process with {files_grown} more "
        "open files"
    )
