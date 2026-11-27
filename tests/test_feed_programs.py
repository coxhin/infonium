import gzip
import random
import sys
import tempfile
import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import partial
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

import httpx
import psutil
import pytest
from pydantic import BaseModel, SecretStr

from infonium.subscriptions import program
from infonium.subscriptions.discovery import discovered_feed
from infonium.subscriptions.feed import UnreadableFeed
from infonium.subscriptions.fetcher import Validators, fetched_feed
from infonium.subscriptions.program import discovered_apart, polled_apart
from tests.conftest import PROXY_VARIABLES
from tests.feed_server import (
    HOST,
    KEY_PARAMETER,
    RSSHUB_ERROR,
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
RSSHUB_SETTING = "INFONIUM_RSSHUB_BASE_URL"
RSSHUB_ENGINE = "engines/rsshub"
KEY_SETTING = "INFONIUM_RSSHUB_ACCESS_KEY"
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


# N7 and N12: an instance that does not answer a poll of a route.


def test_apart_an_rsshub_that_does_not_answer_a_poll_of_a_route_is_told_as_such(
    server: ScriptedServer,
) -> None:
    told = refused_apart(
        lambda: polled_apart("rsshub://night/trains", Validators(), CLOSED),
        "a poll apart of a route of a silent RSSHub",
    )
    assert all(said in told for said in (CLOSED, "RSSHub", RSSHUB_SETTING, RSSHUB_ENGINE)), (
        f"a poll apart of a route of a silent RSSHub was told as {told!r}, not as RSSHub not "
        f"answering at {CLOSED}, with {RSSHUB_SETTING} and {RSSHUB_ENGINE}"
    )
    assert "rsshub://" not in told, (
        f"a poll apart of a route of a silent RSSHub was told as {told!r}, which advises a route"
    )


# N2: a fetcher that ends without an answer.

ENDINGS_WITHOUT_AN_ANSWER = {
    "with status 0": [sys.executable, "-c", "pass"],
    "with status 3": [sys.executable, "-c", "import sys; sys.exit(3)"],
}


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
@pytest.mark.parametrize("ending", sorted(ENDINGS_WITHOUT_AN_ANSWER))
def test_apart_a_fetcher_that_ends_without_an_answer_ends_in_unreadable_feed_alone(
    server: ScriptedServer, monkeypatch: pytest.MonkeyPatch, ending: str, operation: str
) -> None:
    server.scripts["/feed.xml"] = conditional(FEED, ETAG)
    monkeypatch.setattr(program, "FETCHER", ENDINGS_WITHOUT_AN_ANSWER[ending])
    at = server.address("/feed.xml")
    refused_apart(lambda: OPERATIONS[operation](at), f"a {operation} apart by a fetcher {ending}")


# N8: a key for RSSHub. The key is a word that stands nowhere else, so that finding it tells it
# leaked; the instance holds every request to it.
KEY = "quorvex-zindlap-9146"
OTHER_KEY = "mertusk-plovane-9147"
SITE_PORT = 9147
ROUTE = "/night/trains"


@pytest.fixture
def site(server: ScriptedServer) -> Iterator[ScriptedServer]:
    # A site of its own, beside the instance of RSSHub that the server plays.
    with scripted_server(SITE_PORT) as scripted:
        yield scripted


def assert_key_nowhere(capfd: pytest.CaptureFixture[str], what: str, *said: str) -> None:
    out, err = capfd.readouterr()
    for text in (*said, out, err):
        assert KEY not in text, f"{what} let the key out: {text[:400]!r}"


def test_apart_every_request_to_rsshub_carries_its_key_and_none_to_another_host_does(
    server: ScriptedServer, site: ScriptedServer, capfd: pytest.CaptureFixture[str]
) -> None:
    server.key = KEY
    server.scripts[ROUTE] = conditional(FEED, ETAG)
    server.scripts["/api/radar/rules"] = sent(b"{}", "application/json")
    site.scripts["/"] = sent(BARE_PAGE, "text/html")
    instance, key = server.address(""), SecretStr(KEY)
    found = discovered_apart(f"rsshub:/{ROUTE}", instance, key)
    polled = polled_apart(f"rsshub:/{ROUTE}", Validators(), instance, key)
    told = refused_apart(
        lambda: discovered_apart(site.address("/"), instance, key), "a discovery of the site"
    )
    keyed = [heard for heard in server.heard if f"key={KEY}" in heard]
    assert keyed == server.heard and len(keyed) >= 3, (
        f"RSSHub heard {server.heard}, not every request with its key: the route at a discovery, "
        "at a poll, and the rules"
    )
    assert any(heard.startswith("/api/radar/rules") for heard in keyed), (
        f"RSSHub heard {server.heard}, without a request for its rules"
    )
    assert site.heard and not any(KEY in heard for heard in site.heard), (
        f"the site heard {site.heard}: the key went to another host"
    )
    assert_key_nowhere(
        capfd, "a discovery and a poll apart with a key", found.address, repr(polled), told
    )


def answer_echoing_the_request(handler: ScriptedHandler) -> None:
    # RSSHub's page of an error that quotes the address it was asked, key and all, as a route's
    # error may.
    page = RSSHUB_ERROR.format(message=f"FetchError: {handler.path} failed", route=ROUTE)
    sent(page.encode(), "text/html; charset=utf-8", 503)(handler)


KEYED_FAILURES: dict[str, Callable[[ScriptedServer], tuple[str, SecretStr | None]]] = {
    "a silent instance": lambda s: (CLOSED, SecretStr(KEY)),
    "a route whose error quotes the request": lambda s: (
        s.scripts.update({ROUTE: answer_echoing_the_request}) or s.address(""),
        SecretStr(KEY),
    ),
    "a route broken off": lambda s: (
        s.scripts.update({ROUTE: broken_off(FEED_OPENING, RSS_KIND, 64 * 1024)}) or s.address(""),
        SecretStr(KEY),
    ),
    "a key the instance refuses": lambda s: (s.address(""), SecretStr(OTHER_KEY)),
    "no key where the instance asks for one": lambda s: (s.address(""), None),
}
KEY_STEPS: dict[str, Callable[[str, SecretStr | None], object]] = {
    "discovery": lambda instance, key: discovered_apart(f"rsshub:/{ROUTE}", instance, key),
    "poll": lambda instance, key: polled_apart(f"rsshub:/{ROUTE}", Validators(), instance, key),
}


@pytest.mark.parametrize("step", sorted(KEY_STEPS))
@pytest.mark.parametrize("failure", sorted(KEYED_FAILURES))
def test_apart_the_key_for_rsshub_is_in_no_sentence_whatever_fails(
    server: ScriptedServer, capfd: pytest.CaptureFixture[str], failure: str, step: str
) -> None:
    server.key = KEY
    instance, key = KEYED_FAILURES[failure](server)
    what = f"a {step} apart of a route with {failure}"
    told = refused_apart(lambda: KEY_STEPS[step](instance, key), what)
    assert_key_nowhere(capfd, what, told)
    if failure == "a key the instance refuses":
        assert KEY_SETTING in told and "refus" in told, (
            f"{what} was told as {told!r}, not as RSSHub refusing the key {KEY_SETTING} gives it"
        )
    if failure == "no key where the instance asks for one":
        assert KEY_SETTING in told and "key" in told, (
            f"{what} was told as {told!r}, not as RSSHub asking for a key {KEY_SETTING} gives none"
        )


# T4: the key goes to the instance alone, the server on PORT; the site on SITE_PORT plays another
# server. A key with characters a query escapes.
ESCAPED_KEY = "q+uorvex/zindlap-ü9146"
SITE = f"http://{HOST}:{SITE_PORT}"


def written_escaped_keys() -> tuple[str, ...]:
    # The key as it is, as an address writes it as it is (its letter outside ASCII escaped, as an
    # address must), escaped once, twice, and as a form escapes it.
    once = quote(ESCAPED_KEY, safe="")
    return (
        ESCAPED_KEY,
        quote(ESCAPED_KEY, safe="+/"),
        once,
        quote(once, safe=""),
        quote_plus(ESCAPED_KEY),
    )


def redirected_to(
    location_of: Callable[[ScriptedHandler], str],
) -> Callable[[ScriptedHandler], None]:
    def script(handler: ScriptedHandler) -> None:
        handler.head(302, {"Location": location_of(handler), "Content-Length": "0"})

    return script


# Addresses of the site that hold the key: under key or another name, as it is, escaped once or
# twice, or inside the address asked, as a login wall writes it.
KEY_LOCATIONS: dict[str, Callable[[ScriptedHandler], str]] = {
    "under key, escaped once": lambda h: f"{SITE}/sso?key={quote(ESCAPED_KEY, safe='')}",
    "under another name": lambda h: f"{SITE}/sso?token={quote(ESCAPED_KEY, safe='')}",
    "written as it is": lambda h: f"{SITE}/sso?key={quote(ESCAPED_KEY, safe='+/')}",
    "escaped twice": lambda h: f"{SITE}/sso?key={quote(quote(ESCAPED_KEY, safe=''), safe='')}",
    "inside the address asked": lambda h: (
        f"{SITE}/sso?rd={quote(f'http://{HOST}:{PORT}{h.path}', safe='')}"
    ),
}


def keyed_step(step: str, server: ScriptedServer) -> Callable[[], object]:
    return lambda: KEY_STEPS[step](server.address(""), SecretStr(ESCAPED_KEY))


@pytest.mark.parametrize("step", sorted(KEY_STEPS))
@pytest.mark.parametrize("location", sorted(KEY_LOCATIONS))
def test_apart_a_route_redirected_elsewhere_by_an_address_that_holds_the_key_is_not_followed(
    server: ScriptedServer,
    site: ScriptedServer,
    capfd: pytest.CaptureFixture[str],
    location: str,
    step: str,
) -> None:
    server.key = ESCAPED_KEY
    server.scripts[ROUTE] = redirected_to(KEY_LOCATIONS[location])
    what = f"a {step} apart of a route that RSSHub redirects to the site with the key {location}"
    told = refused_apart(keyed_step(step, server), what)
    assert server.heard and not site.heard, f"{what} asked the site {site.heard}"
    assert SITE in told and f"{SITE}/" not in told and f"{SITE}?" not in told, (
        f"{what} was told as {told!r}, which does not name the site by its scheme, host and port "
        "alone"
    )
    assert ROUTE.lstrip("/") in told and "instance" in told and RSSHUB_SETTING in told, (
        f"{what} was told as {told!r}, which does not say that the address asked redirects there, "
        f"that the key is for the instance alone, and that {RSSHUB_SETTING} names it"
    )
    assert "rsshub://" not in told, f"{what} was told as {told!r}, which advises a route"
    out, err = capfd.readouterr()
    for text in (told, out, err):
        assert not any(key in text for key in written_escaped_keys()), (
            f"{what} let the key out: {text[:400]!r}"
        )


@pytest.mark.parametrize("step", sorted(KEY_STEPS))
def test_apart_a_route_redirected_to_another_server_without_the_key_is_followed_without_it(
    server: ScriptedServer, site: ScriptedServer, step: str
) -> None:
    server.key = ESCAPED_KEY
    server.scripts[ROUTE] = redirected_to(lambda h: f"{SITE}/feeds/night.xml")
    site.scripts["/feeds/night.xml"] = sent(FEED, RSS_KIND)
    what = f"a {step} apart of a route that RSSHub redirects to the site without the key"
    ended = outcome(keyed_step(step, server), what)
    assert site.heard and all(heard == "/feeds/night.xml" for heard in site.heard), (
        f"{what} asked the site {site.heard}: not followed, or followed with the key"
    )
    assert ended.startswith("gave"), f"{what} ended {ended}"


@pytest.mark.parametrize("step", sorted(KEY_STEPS))
def test_apart_a_route_redirected_within_the_instance_carries_the_key(
    server: ScriptedServer, step: str
) -> None:
    server.key = ESCAPED_KEY
    server.scripts[ROUTE] = redirected_to(lambda h: server.address(f"{ROUTE}/moved"))
    server.scripts[f"{ROUTE}/moved"] = sent(FEED, RSS_KIND)
    what = f"a {step} apart of a route that RSSHub redirects within itself"
    ended = outcome(keyed_step(step, server), what)
    moved_to = [heard for heard in server.heard if urlsplit(heard).path == f"{ROUTE}/moved"]
    assert moved_to and all(
        (KEY_PARAMETER, ESCAPED_KEY) in parse_qsl(urlsplit(heard).query) for heard in moved_to
    ), f"{what} asked {moved_to}, not with the key"
    assert ended.startswith("gave"), f"{what} ended {ended}"
