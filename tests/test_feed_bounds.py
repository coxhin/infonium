import json
import subprocess
import sys
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from types import FrameType
from typing import Any

import httpx
import pytest
from hypothesis import given
from pydantic import BaseModel

from infonium.subscriptions.feed import (
    UnreadableFeed,
    read_feed,
)
from tests.feed_reading_bound import PEAK_BYTES, SECONDS
from tests.feed_writing import (
    ADDRESS,
    ATOM_NAMESPACE,
    JSON_FEED_VERSIONS,
    RSS_KIND,
    XML_FORMS,
    FeedForm,
    SaidFeed,
    ServedFeed,
    plain_posting,
    read,
    read_or_refused,
    served_feeds,
    unmarked_text,
    written_feed,
)

# Judgments of read_feed: the bounds of a reading, a feed as another site's text, and a
# reading that touches nothing but its arguments (F8 to F10).


# F8: bounds.

MOST_POSTINGS = 2000
# The most postings a reading need look at: twice those it keeps, as F8 bounds the items of a
# JSON Feed that are read.
MOST_POSTINGS_READ = 2 * MOST_POSTINGS


@pytest.mark.parametrize("form", [FeedForm.RSS, FeedForm.ATOM, FeedForm.JSON_1_1])
@pytest.mark.parametrize("unaddressed", [0, 300, MOST_POSTINGS + 100])
def test_of_a_feed_of_more_than_2000_postings_the_first_2000_with_an_address_are_kept(
    form: FeedForm, unaddressed: int
) -> None:
    # Of postings beyond the first 4,000 none need be kept: where more than 2,000 without an
    # address come first, those with one among the first 4,000 are enough.
    title = unmarked_text("A posting")
    postings = [plain_posting(number, title, None) for number in range(unaddressed)] + [
        plain_posting(number, title, f"https://feeds.test/p/{number}")
        for number in range(unaddressed, unaddressed + MOST_POSTINGS + 100)
    ]
    said = SaidFeed(title=title, site=None, base=None, postings=postings)
    keys = [posting.key for posting in read_feed(written_feed(said, form), "", ADDRESS).postings]
    expected = [
        f"tag:feeds.test,2026:{number}"
        for number in range(unaddressed, unaddressed + MOST_POSTINGS)
    ]
    fewest = min(MOST_POSTINGS, MOST_POSTINGS_READ - unaddressed)
    assert keys == expected[: len(keys)] and len(keys) >= fewest, (
        f"of a {form} feed of {unaddressed} postings without an address followed by "
        f"{MOST_POSTINGS + 100} with one, {len(keys)} were kept, from {keys[:1]} to {keys[-1:]}, "
        f"not the first {MOST_POSTINGS} with an address, or at least the {fewest} with one among "
        f"the first {MOST_POSTINGS_READ}"
    )


def traced_peak(reading: Callable[[], object]) -> int:
    # The most memory that Python's objects held at once while the reading ran, beyond what
    # they held before it: a count of what the reading made, which the clock does not sway.
    tracemalloc.start()
    try:
        reading()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def calls_made(content: bytes) -> int:
    # The calls of functions, Python's and its built-ins', that reading the bytes makes.
    calls = [0]

    def heard(frame: FrameType, event: str, argument: object) -> None:
        if event in CALL_EVENTS:
            calls[0] += 1

    sys.setprofile(heard)
    try:
        read_feed(content, "", ADDRESS)
    finally:
        sys.setprofile(None)
    return calls[0]


CALL_EVENTS = frozenset({"call", "c_call"})
# Reading what is sent beyond a bound may hold more than parsing its JSON holds more by half at
# most, and make a few more calls: holding a copy of its text, say. Reading each item or
# attachment as a model holds three times as much, as a stand-in of pydantic models measured.
MORE_HELD_THAN_PARSED = 1.5
MORE_CALLS = 1000


def assert_read_in_proportion_to_what_is_kept(sent: bytes, bounded: bytes, what: str) -> None:
    more_calls = calls_made(sent) - calls_made(bounded)
    assert more_calls <= MORE_CALLS, (
        f"reading {what} made {more_calls} calls more than reading the bounded feed: what is "
        "sent beyond the bound is read"
    )
    read_more = traced_peak(lambda: read_feed(sent, "", ADDRESS)) - traced_peak(
        lambda: read_feed(bounded, "", ADDRESS)
    )
    parsed_more = traced_peak(lambda: json.loads(sent)) - traced_peak(lambda: json.loads(bounded))
    assert read_more <= MORE_HELD_THAN_PARSED * parsed_more, (
        f"reading {what} held {read_more / 1024**2:.2f} MiB more than reading the bounded feed, "
        f"where parsing its JSON holds {parsed_more / 1024**2:.2f} MiB more: what is sent beyond "
        "the bound is read"
    )


def json_feed_of(items: list[dict[str, Any]]) -> bytes:
    return json.dumps({"version": JSON_FEED_VERSIONS[FeedForm.JSON_1_1], "items": items}).encode()


def items_beyond(extra: int) -> bytes:
    # A feed of 2,000 items with an address, and after them small items without one.
    items: list[dict[str, Any]] = [
        {"id": str(number), "url": f"https://feeds.test/p/{number}"}
        for number in range(MOST_POSTINGS)
    ]
    items += [{"id": str(number)} for number in range(MOST_POSTINGS, MOST_POSTINGS + extra)]
    return json_feed_of(items)


SENT_ITEMS = 10_000
FAULT_OF_BOUNDS = (
    "F8 as amended, not yet built: every item of a JSON Feed, and every attachment of an item, "
    "is read, whatever the bound"
)


@pytest.mark.xfail(strict=True, reason=FAULT_OF_BOUNDS)
def test_of_a_json_feed_no_more_items_are_read_than_twice_those_kept() -> None:
    sent = items_beyond(SENT_ITEMS - MOST_POSTINGS)
    bounded = items_beyond(MOST_POSTINGS_READ - MOST_POSTINGS)
    kept = len(read_feed(sent, "", ADDRESS).postings)
    assert kept == MOST_POSTINGS, f"of {SENT_ITEMS} items of a JSON Feed {kept} were kept"
    assert_read_in_proportion_to_what_is_kept(sent, bounded, f"a JSON Feed of {SENT_ITEMS} items")


def attached(count: int) -> bytes:
    # A feed of one item with a recording, and after it small attachments.
    attachments: list[dict[str, Any]] = [
        {"url": "https://feeds.test/a/0.mp3", "mime_type": "audio/mpeg"}
    ]
    attachments += [{"url": "a"} for _ in range(count - 1)]
    return json_feed_of([{"id": "1", "url": "https://feeds.test/p/1", "attachments": attachments}])


SENT_ATTACHMENTS = 20_000
BOUNDED_ATTACHMENTS = 1_000


@pytest.mark.xfail(strict=True, reason=FAULT_OF_BOUNDS)
def test_of_one_json_feed_item_the_attachments_read_are_bounded() -> None:
    sent, bounded = attached(SENT_ATTACHMENTS), attached(BOUNDED_ATTACHMENTS)
    addresses = [posting.address for posting in read_feed(sent, "", ADDRESS).postings]
    assert addresses == ["https://feeds.test/a/0.mp3"], (
        f"an item of {SENT_ATTACHMENTS} attachments, the first of sound, was read at {addresses}"
    )
    assert_read_in_proportion_to_what_is_kept(
        sent, bounded, f"a JSON Feed item of {SENT_ATTACHMENTS} attachments"
    )


# F9 and F10: a feed is another site's text, and reading it touches nothing else.

# Events of Python's audit that tell a file opened, a name looked up, a connection made, a
# request sent or a program started. A module imported opens its file too, and is no file a
# feed made the reading open.
WATCHED_EVENTS = frozenset(
    {
        "open",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "urllib.Request",
        "http.client.connect",
        "subprocess.Popen",
        "os.system",
        "os.posix_spawn",
        "os.exec",
        "os.fork",
    }
)
IMPORTED_SUFFIXES = (".py", ".pyc", ".so", ".pth")


class Touches:
    # What the process touches beyond itself while a call is watched, as its audit events tell.
    def __init__(self) -> None:
        self.watching = False
        self.touched: list[str] = []

    def hear(self, event: str, arguments: tuple[Any, ...]) -> None:
        imported = event == "open" and str(arguments[0]).endswith(IMPORTED_SUFFIXES)
        if self.watching and event in WATCHED_EVENTS and not imported:
            self.touched.append(f"{event} {arguments!r}"[:300])

    @contextmanager
    def watched(self) -> Iterator[list[str]]:
        self.touched = []
        self.watching = True
        try:
            yield self.touched
        finally:
            self.watching = False


@cache
def touches() -> Touches:
    # An audit hook stays for the life of the process; one is added, and listens only while a
    # call is watched.
    hearing = Touches()
    sys.addaudithook(hearing.hear)
    return hearing


@given(served=served_feeds())
def test_reading_a_feed_reads_its_arguments_and_nothing_else(served: ServedFeed) -> None:
    with touches().watched() as touched:
        feed = read(served)
    assert not touched, f"reading a {served.form} feed touched what is not its arguments: {touched}"
    assert read(served) == feed, f"a {served.form} feed read twice was read as two feeds"


# Words that only a file of this machine holds, so that finding them in a feed tells that the
# file was read into it.
SECRET = "quorvex zindlap mertusk"
REFERRING_ITEM = (
    "<item><title>&secret;</title><link>https://feeds.test/p/1</link>"
    "<description>&secret;</description></item>"
)
XINCLUDE = ' xmlns:xi="http://www.w3.org/2001/XInclude"'


def rss_with(doctype: str, root: str = "", item: str = REFERRING_ITEM) -> str:
    return (
        f'<?xml version="1.0"?>{doctype}<rss version="2.0"{root}><channel><title>&secret;</title>'
        f"{item}</channel></rss>"
    )


def included(address: str) -> str:
    return (
        "<item><title>Included</title><link>https://feeds.test/p/1</link><description>"
        f'<xi:include href="{address}" parse="text"/></description></item>'
    )


# Feeds that refer, by an entity, a DTD or an inclusion, to a file of this machine: the file
# that holds the secret, or one that declares it as an entity.
LOCAL_SAMPLES = {
    "a general entity naming a file by its URL": rss_with(
        '<!DOCTYPE rss [<!ENTITY secret SYSTEM "{secret_url}">]>'
    ),
    "a general entity naming a file by its path": rss_with(
        '<!DOCTYPE rss [<!ENTITY secret SYSTEM "{secret_path}">]>'
    ),
    "a parameter entity naming a file of declarations": rss_with(
        '<!DOCTYPE rss [<!ENTITY % outside SYSTEM "{declarations_url}"> %outside;]>'
    ),
    "an external DTD": rss_with('<!DOCTYPE rss SYSTEM "{declarations_url}">'),
    "an XInclude": rss_with("", XINCLUDE, included("{secret_url}")),
}
# Feeds that refer to what a server elsewhere holds.
REMOTE_SAMPLES = {
    "an external DTD on the web": rss_with('<!DOCTYPE rss SYSTEM "http://dtd.test/rss.dtd">'),
    "an entity on the web": rss_with(
        '<!DOCTYPE rss [<!ENTITY secret SYSTEM "http://entities.test/secret.txt">]>'
    ),
    "a parameter entity on the web": rss_with(
        '<!DOCTYPE rss [<!ENTITY % outside SYSTEM "http://entities.test/outside.dtd"> %outside;]>'
    ),
    "a stylesheet on the web": rss_with(
        '<?xml-stylesheet type="text/xsl" href="http://styles.test/feed.xsl"?>'
        '<!DOCTYPE rss [<!ENTITY secret "kept">]>'
    ),
    "an XInclude on the web": rss_with("", XINCLUDE, included("http://include.test/part.xml")),
}


@pytest.mark.parametrize("sample", sorted(LOCAL_SAMPLES))
def test_a_feed_that_refers_to_a_file_of_this_machine_reads_no_file(
    sample: str, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET)
    declarations = tmp_path / "outside.dtd"
    declarations.write_text(f'<!ENTITY secret "{SECRET}">')
    content = LOCAL_SAMPLES[sample].format(
        secret_url=secret.as_uri(), secret_path=secret, declarations_url=declarations.as_uri()
    )
    with touches().watched() as touched:
        read_feeds = read_or_refused(content.encode(), RSS_KIND, ADDRESS)
    assert not touched, f"reading a feed with {sample} touched {touched}"
    told = [feed.model_dump_json() for feed in read_feeds]
    assert not any(word in text for text in told for word in SECRET.split()), (
        f"a feed with {sample} was read with the file's words in it: {told}"
    )


def no_fetch(method: str, url: str, **options: Any) -> Any:
    pytest.fail(f"reading a feed fetched {url}")


@pytest.mark.parametrize("sample", sorted(REMOTE_SAMPLES))
def test_a_feed_that_refers_to_what_a_server_holds_fetches_nothing(
    sample: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "stream", no_fetch)
    with touches().watched() as touched:
        read_or_refused(REMOTE_SAMPLES[sample].encode(), RSS_KIND, ADDRESS)
    assert not touched, f"reading a feed with {sample} touched {touched}"


def bomb_feed(declarations: str, reference: str, form: FeedForm) -> str:
    # A feed whose titles are an entity that its declarations make vast.
    if form is FeedForm.RSS:
        return (
            f'<?xml version="1.0"?><!DOCTYPE rss [{declarations}]><rss version="2.0"><channel>'
            f"<title>{reference}</title><item><title>{reference}</title>"
            "<link>https://feeds.test/p/1</link></item></channel></rss>"
        )
    return (
        f'<?xml version="1.0"?><!DOCTYPE feed [{declarations}]><feed xmlns="{ATOM_NAMESPACE}">'
        f"<title>{reference}</title><entry><id>tag:feeds.test,2026:1</id>"
        f'<title>{reference}</title><link href="https://feeds.test/p/1"/></entry></feed>'
    )


BOMBS = {
    # Ten entities each of ten of the one before: a thousand million laughs.
    "the billion laughs": (
        '<!ENTITY lol0 "lol">'
        + "".join(f'<!ENTITY lol{level} "{f"&lol{level - 1};" * 10}">' for level in range(1, 10)),
        "&lol9;",
    ),
    # One entity of fifty thousand letters said fifty thousand times: 2.5 GB.
    "a quadratic blow-up": (f'<!ENTITY a "{"a" * 50_000}">', "&a;" * 50_000),
}
# What reading a feed may take, whatever it holds, as the requirements bound the reading a poll
# and a discovery make. The process that reads it may hold no more than CHILD_MEMORY_BYTES, so
# that a reading that runs away ends there; each sample is waited for a time of its own.
BOMB_SECONDS = 30.0
BOMB_BYTES = 512 * 1024**2
CHILD_MEMORY_BYTES = 4 * 1024**3
BOMB_WAIT_SECONDS = 120.0
REPOSITORY = Path(__file__).resolve().parents[1]
REFUSED = f"{UnreadableFeed.__module__}.{UnreadableFeed.__qualname__}"


class BoundedReading(BaseModel):
    # How a reading in a process of its own ended: its status, the last line it wrote of an
    # error, and the last line it wrote of what it measured.
    status: int
    ending: str
    measured: str


def bounded_reading(content: bytes, folder: Path, wait_seconds: float, what: str) -> BoundedReading:
    sample = folder / "sample.xml"
    sample.write_bytes(content)
    command = [
        sys.executable,
        "-m",
        "tests.feed_reading_bound",
        str(sample),
        ADDRESS,
        str(CHILD_MEMORY_BYTES),
    ]
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=REPOSITORY
    ) as child:
        try:
            said, failed = child.communicate(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            pytest.fail(f"reading {what} went on for more than {wait_seconds:.0f} seconds")
    return BoundedReading(
        status=child.returncode,
        ending=(failed.strip().splitlines() or [""])[-1],
        measured=(said.strip().splitlines() or ["{}"])[-1],
    )


def assert_ended_within(reading: BoundedReading, what: str, seconds: float, most: int) -> None:
    assert reading.status == 0 or reading.ending.startswith(REFUSED), (
        f"reading {what} ended with the status {reading.status} and {reading.ending!r}, not in a "
        "feed or in UnreadableFeed"
    )
    measured = json.loads(reading.measured)
    assert measured[SECONDS] <= seconds, f"reading {what} took {measured[SECONDS]:.1f} seconds"
    assert measured[PEAK_BYTES] <= most, (
        f"reading {what} held {measured[PEAK_BYTES] / 1024**2:.0f} MiB at most"
    )


@pytest.mark.parametrize("form", XML_FORMS)
@pytest.mark.parametrize("bomb", sorted(BOMBS))
def test_an_entity_bomb_ends_in_a_feed_or_unreadable_feed_within_bounds(
    bomb: str, form: FeedForm, tmp_path: Path
) -> None:
    what = f"{bomb} in {form}"
    reading = bounded_reading(
        bomb_feed(*BOMBS[bomb], form).encode(), tmp_path, BOMB_WAIT_SECONDS, what
    )
    assert_ended_within(reading, what, BOMB_SECONDS, BOMB_BYTES)


ORDINARY_FEED = (
    '<rss version="2.0"><channel><title>Behind comments</title><item><title>A posting</title>'
    "<link>https://feeds.test/p/1</link></item></channel></rss>"
)
# Inputs on which a pattern that backtracks takes time that grows faster than they do: at
# these sizes time in proportion to them is far under a second, and the growth seen before
# (doubling with each comment; 22 seconds for 1 MiB of declarations) far over the bound.
GREEDY_SAMPLES = {
    "an ordinary feed behind 40 comments": ("<!--a-->" * 40 + ORDINARY_FEED).encode(),
    "2 MiB of declarations of entities that never close": b"<!ENTITY" * (2 * 1024**2 // 8),
}
# The bound on the reading, and on the wait for the process, which starts in a second or two.
LINEAR_SECONDS = 2.0
LINEAR_WAIT_SECONDS = 6.0


@pytest.mark.xfail(
    strict=True,
    reason="F9 as amended, not yet built: comments before a feed take time that doubles with "
    "each, and declarations of entities that never close take time that grows with their square",
)
@pytest.mark.parametrize("sample", sorted(GREEDY_SAMPLES))
def test_reading_takes_time_in_proportion_to_what_is_read(sample: str, tmp_path: Path) -> None:
    reading = bounded_reading(GREEDY_SAMPLES[sample], tmp_path, LINEAR_WAIT_SECONDS, sample)
    assert_ended_within(reading, sample, LINEAR_SECONDS, BOMB_BYTES)
