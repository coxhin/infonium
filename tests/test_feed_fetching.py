import gzip
import time
import zlib
from datetime import UTC, datetime
from email.utils import format_datetime
from itertools import pairwise

import httpx
import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium import guard, intake
from infonium.document import OVERDUE_SOURCE, OVERSIZE_SOURCE, error_status, turned_away
from infonium.subscriptions.feed import ADDRESS_CHARACTERS, UnreadableFeed, read_feed
from infonium.subscriptions.fetcher import Changed, Unchanged, Validators, fetched_feed
from tests.feed_sites import (
    CONTENT_TYPE,
    LOWERED_FEED_BYTES,
    LOWERED_FEED_SECONDS,
    FeedSites,
    answered,
    broken_off,
    failing,
    kind_of,
    moved,
    slow,
    status_only,
    trickling,
)
from tests.feed_writing import ServedFeed, keyed_feeds, served_feeds

# Judgments of fetched_feed (P1 to P5 and their amendments), polling a feed from stand-in sites
# that each test scripts.

FEED = "https://feeds.test/feed.xml"
RSS_KIND = "application/rss+xml"
# A feed whose reading depends on the address it is read at: postings at relative pages and no
# title, so that it is called by the host of its address.
RELATIVE_FEED = (
    b'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><link>/</link>'
    b'<item><title>One</title><link>posts/1</link><guid isPermaLink="false">1</guid></item>'
    b'<item><title>Two</title><link>../2</link><guid isPermaLink="false">2</guid></item>'
    b"</channel></rss>"
)
FEED_CLOSING = b"</channel></rss>"
ETAG = "etag"
LAST_MODIFIED = "last-modified"
CONTENT_ENCODING = "content-encoding"
IF_NONE_MATCH = "if-none-match"
IF_MODIFIED_SINCE = "if-modified-since"
ACCEPT = "accept"
ACCEPT_ENCODING = "accept-encoding"
USER_AGENT = "user-agent"
FEED_KINDS = ("application/rss+xml", "application/atom+xml", "application/feed+json")
ANYTHING = "*/*"
ASKED_CODINGS = frozenset({"gzip", "deflate", "identity"})
# How long beyond a limit of time a fetch may go on before it is given up: a chunk of a
# trickle, and the load of a busy machine.
OVERRUN_SECONDS = 1.0
EXAMPLES = settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
FAULT_EXAMPLES = settings(
    EXAMPLES, phases=(Phase.explicit, Phase.reuse, Phase.generate), max_examples=10
)

etags = st.from_regex(r'(W/)?"[A-Za-z0-9._-]{1,16}"', fullmatch=True)
modified_times = st.datetimes(
    min_value=datetime(2000, 1, 1), max_value=datetime(2090, 1, 1), timezones=st.just(UTC)
).map(lambda moment: format_datetime(moment.replace(microsecond=0), usegmt=True))
validators = st.builds(Validators, etag=st.none() | etags, modified=st.none() | modified_times)
hosts = st.sampled_from(("feeds.test", "www.feeds.test", "cdn.test", "blog.example.test"))
paths = st.sampled_from(("/feed.xml", "/a/b/rss", "/feeds/all/", "/rss?format=2"))
redirects = st.sampled_from((301, 302, 303, 307, 308))


def accepted(header: str) -> dict[str, float]:
    # The kinds an Accept header names, each with the quality it gives it.
    kinds = {}
    for named in header.split(","):
        kind, *parameters = (piece.strip() for piece in named.split(";"))
        qualities = [float(p.split("=")[1]) for p in parameters if p.startswith("q=")]
        kinds[kind.lower()] = qualities[0] if qualities else 1.0
    return kinds


def codings(header: str) -> set[str]:
    return {named.split(";")[0].strip().lower() for named in header.split(",") if named.strip()}


# P1: one GET, as the intake makes it.


@EXAMPLES
@given(held=validators)
def test_a_poll_is_one_get_as_the_intake_makes_it_carrying_the_validators_it_holds(
    feed_sites: FeedSites, held: Validators
) -> None:
    feed_sites.forget()
    feed_sites.serve(FEED, answered(RELATIVE_FEED, kind_of(RSS_KIND)))
    fetched_feed(FEED, held)
    assert len(feed_sites.fetches) == len(feed_sites.requests) == 1, (
        f"a poll of a feed that answers at once made the requests {feed_sites.asked()} in "
        f"{len(feed_sites.fetches)} fetches, not one GET"
    )
    [request] = feed_sites.requests
    headers = request.headers
    assert request.method == "GET", f"a poll asked for the feed with {request.method}"
    assert headers.get(USER_AGENT) == intake.USER_AGENT, (
        f"a poll asked as {headers.get(USER_AGENT)!r}, not with the intake's user agent"
    )
    assert (headers.get(IF_NONE_MATCH), headers.get(IF_MODIFIED_SINCE)) == (
        held.etag,
        held.modified,
    ), (
        f"a poll from the validators {held} asked with If-None-Match "
        f"{headers.get(IF_NONE_MATCH)!r} and If-Modified-Since {headers.get(IF_MODIFIED_SINCE)!r}"
    )
    kinds = accepted(headers.get(ACCEPT, ""))
    assert kinds.get(ANYTHING, 0) > 0 and all(
        kinds.get(kind, 0) > kinds[ANYTHING] for kind in FEED_KINDS
    ), (
        f"a poll asked to be sent {headers.get(ACCEPT)!r}, which does not prefer feeds and "
        "take anything"
    )
    asked = codings(headers.get(ACCEPT_ENCODING, ""))
    assert asked and asked <= ASKED_CODINGS, (
        f"a poll asked for the codings {headers.get(ACCEPT_ENCODING)!r}, not gzip or deflate alone"
    )
    timeout = request.extensions["timeout"]
    assert timeout["read"] == intake.FETCH_TIMEOUT_SECONDS and (
        timeout["connect"] is not None and timeout["connect"] <= intake.FETCH_TIMEOUT_SECONDS
    ), (
        f"a poll waits for a silent server as {timeout}, not {intake.FETCH_TIMEOUT_SECONDS} "
        "seconds as the intake does"
    )


@EXAMPLES
@given(
    statuses=st.lists(redirects, min_size=1, max_size=3),
    host=hosts,
    path=paths,
    held=validators,
)
def test_a_poll_follows_redirects_and_reads_the_feed_at_the_address_that_answered(
    feed_sites: FeedSites, statuses: list[int], host: str, path: str, held: Validators
) -> None:
    feed_sites.forget()
    stops = [FEED, *(f"https://{host}/moved/{number}" for number in range(len(statuses) - 1))]
    final = f"https://{host}{path}"
    for stop, onward, status in zip(stops, [*stops[1:], final], statuses, strict=True):
        feed_sites.serve(stop, moved(onward, status))
    feed_sites.serve(final, answered(RELATIVE_FEED, kind_of(RSS_KIND)))
    fetched = fetched_feed(FEED, held)
    expected = read_feed(RELATIVE_FEED, RSS_KIND, final)
    assert isinstance(fetched, Changed) and fetched.feed == expected, (
        f"a poll of {FEED}, moved by {statuses} to {final}, gave {fetched}, not the feed read "
        f"at the address that answered: {expected}"
    )


@EXAMPLES
@given(held=validators)
def test_an_answer_304_is_unchanged(feed_sites: FeedSites, held: Validators) -> None:
    feed_sites.forget()
    feed_sites.serve(FEED, status_only(304, httpx.Headers({ETAG: '"same"'})))
    fetched = fetched_feed(FEED, held)
    assert isinstance(fetched, Unchanged), f"an answer 304 to {held} was taken for {fetched}"


def compressed(content: bytes, coding: str) -> bytes:
    match coding:
        case "gzip":
            return gzip.compress(content)
        case "deflate":
            return zlib.compress(content)
        case _:
            return content


# P2: an answer 200.


@EXAMPLES
@given(
    served=served_feeds(keyed_feeds),
    etag=st.none() | etags,
    modified=st.none() | modified_times,
    coding=st.sampled_from(("identity", "gzip", "deflate")),
)
def test_an_answer_200_is_the_feed_read_from_its_body_with_the_validators_it_gives(
    feed_sites: FeedSites, served: ServedFeed, etag: str | None, modified: str | None, coding: str
) -> None:
    feed_sites.forget()
    given_headers = {
        CONTENT_TYPE: served.declared,
        ETAG: etag,
        LAST_MODIFIED: modified,
        CONTENT_ENCODING: None if coding == "identity" else coding,
    }
    headers = httpx.Headers({name: value for name, value in given_headers.items() if value})
    feed_sites.serve(served.address, answered(compressed(served.content, coding), headers))
    fetched = fetched_feed(served.address, Validators())
    expected = Changed(
        feed=read_feed(served.content, served.declared, served.address),
        validators=Validators(etag=etag, modified=modified),
    )
    assert fetched == expected, (
        f"an answer 200 of a {served.form} feed declared {served.declared!r}, coded {coding}, "
        f"with the ETag {etag!r} and Last-Modified {modified!r}, gave {fetched}, not {expected}"
    )


class ServedVersion(BaseModel):
    etag: str
    content: bytes


def test_a_feed_that_changes_is_polled_from_where_the_fetcher_stopped(
    feed_sites: FeedSites,
) -> None:
    # A server that answers 304 to the validators of the feed it serves, and serves another
    # feed, under another ETag, once it changes.
    served = [ServedVersion(etag='"first"', content=RELATIVE_FEED)]
    modified = "Wed, 25 Nov 2026 10:00:00 GMT"

    def answer(request: httpx.Request) -> httpx.Response:
        if request.headers.get(IF_NONE_MATCH) == served[-1].etag:
            return httpx.Response(304, headers={ETAG: served[-1].etag})
        headers = {CONTENT_TYPE: RSS_KIND, ETAG: served[-1].etag, LAST_MODIFIED: modified}
        return httpx.Response(200, headers=headers, content=served[-1].content)

    feed_sites.serve(FEED, answer)
    first = fetched_feed(FEED, Validators())
    assert isinstance(first, Changed) and first.validators == Validators(
        etag='"first"', modified=modified
    ), f"the first poll of a feed gave {first}"
    again = fetched_feed(FEED, first.validators)
    assert isinstance(again, Unchanged), (
        f"a poll from where the fetcher stopped, of a feed that has not changed, gave {again}"
    )
    served.append(ServedVersion(etag='"second"', content=RELATIVE_FEED.replace(b"One", b"Uno")))
    changed = fetched_feed(FEED, first.validators)
    assert isinstance(changed, Changed) and changed.feed.postings[0].title == "Uno", (
        f"a poll of a feed that changed since the fetcher stopped gave {changed}"
    )
    assert changed.validators.etag == '"second"', (
        f"a poll of a feed that changed stopped at {changed.validators}, not at its new ETag"
    )


# Validators with a byte beyond visible ASCII, as servers send them, beside a Last-Modified that
# is well-formed.
UNSEEN_ETAGS = (b'"caf\xe9"', b'"\xe2\x80\x9cquoted\xe2\x80\x9d"', b'"tab\x7f"')
GOOD_MODIFIED = b"Wed, 25 Nov 2026 10:00:00 GMT"


@pytest.mark.xfail(
    strict=True,
    reason="P2 as amended, not yet built: an ETag with a byte beyond visible ASCII is kept and "
    "sent back as If-None-Match",
)
@pytest.mark.parametrize("etag", UNSEEN_ETAGS)
def test_a_validator_with_a_byte_beyond_visible_ascii_is_not_kept(
    feed_sites: FeedSites, etag: bytes
) -> None:
    headers = httpx.Headers(
        [(b"content-type", RSS_KIND.encode()), (b"etag", etag), (b"last-modified", GOOD_MODIFIED)]
    )
    feed_sites.serve(FEED, answered(RELATIVE_FEED, headers))
    fetched = fetched_feed(FEED, Validators())
    assert isinstance(fetched, Changed) and fetched.validators == Validators(
        modified=GOOD_MODIFIED.decode()
    ), f"an answer with the ETag {etag!r} stopped the fetcher at {fetched}"
    fetched_feed(FEED, fetched.validators)
    assert IF_NONE_MATCH not in feed_sites.requests[-1].headers, (
        f"the poll after an answer with the ETag {etag!r} was sent with If-None-Match"
    )


# Codings infonium does not ask for, each with what a server sends under it: a feed compressed
# twice, and a feed under the name of a coding it is not in.
UNASKED_CODINGS = {
    "gzip, gzip": gzip.compress(gzip.compress(RELATIVE_FEED)),
    "br": RELATIVE_FEED,
    "zstd": RELATIVE_FEED,
    "compress": RELATIVE_FEED,
    "x-made-up": RELATIVE_FEED,
}


CODED_TWICE = pytest.mark.xfail(
    strict=True,
    reason="P1 as amended, not yet built: an answer coded gzip twice is inflated twice and read",
)


@pytest.mark.parametrize(
    "coding",
    [
        pytest.param(coding, marks=CODED_TWICE) if "," in coding else coding
        for coding in sorted(UNASKED_CODINGS)
    ],
)
def test_an_answer_with_a_coding_but_one_of_gzip_or_deflate_is_unreadable(
    feed_sites: FeedSites, coding: str
) -> None:
    headers = httpx.Headers({CONTENT_TYPE: RSS_KIND, CONTENT_ENCODING: coding})
    feed_sites.serve(FEED, answered(UNASKED_CODINGS[coding], headers))
    with pytest.raises(UnreadableFeed) as refused:
        fetched = fetched_feed(FEED, Validators())
        pytest.fail(f"an answer coded {coding!r} was read as {fetched}")
    assert FEED in str(refused.value), (
        f"an answer coded {coding!r} was refused as {refused.value}, which does not name {FEED}"
    )


# P3: any other status, and a guard's check.

other_statuses = st.one_of(
    st.integers(201, 599).filter(lambda status: status != 304),
    st.sampled_from((204, 299, 420, 520, 599)),
)


@EXAMPLES
@given(status=other_statuses)
def test_an_answer_of_any_other_status_is_unreadable_with_that_status(
    feed_sites: FeedSites, status: int
) -> None:
    feed_sites.forget()
    feed_sites.serve(FEED, answered(RELATIVE_FEED, kind_of(RSS_KIND), status))
    with pytest.raises(UnreadableFeed) as refused:
        fetched = fetched_feed(FEED, Validators())
        pytest.fail(f"an answer {status} was read as {fetched}")
    assert str(refused.value) == error_status(FEED, status), (
        f"an answer {status} was refused as {str(refused.value)!r}, not as "
        f"{error_status(FEED, status)!r}"
    )


@EXAMPLES
@given(
    header=st.sampled_from(guard.GUARD_HEADERS),
    value=st.sampled_from(("challenge", "captcha", "1")),
    content=st.sampled_from((RELATIVE_FEED, b"<html><body><p>Wait.</p></body></html>")),
)
def test_an_answer_a_guard_gives_for_its_check_is_turned_away(
    feed_sites: FeedSites, header: str, value: str, content: bytes
) -> None:
    feed_sites.forget()
    headers = httpx.Headers({CONTENT_TYPE: RSS_KIND, header: value})
    feed_sites.serve(FEED, answered(content, headers))
    with pytest.raises(UnreadableFeed) as refused:
        fetched = fetched_feed(FEED, Validators())
        pytest.fail(f"an answer with the guard's header {header} was read as {fetched}")
    assert str(refused.value) == turned_away(FEED), (
        f"an answer with the guard's header {header} was refused as {str(refused.value)!r}, not "
        f"as {turned_away(FEED)!r}"
    )


# P4: the bytes and the time a feed may take.


def feed_of_bytes(size: int) -> bytes:
    # A feed of exactly the bytes given, made up to them with a comment.
    bare = len(RELATIVE_FEED) + len(b"<!---->")
    return RELATIVE_FEED.replace(
        FEED_CLOSING, b"<!--" + b" " * (size - bare) + b"-->" + FEED_CLOSING
    )


@pytest.mark.parametrize("coding", ["identity", "gzip"])
def test_a_feed_of_the_bytes_allowed_is_read_and_one_byte_beyond_is_oversize(
    feed_sites: FeedSites, coding: str
) -> None:
    headers = httpx.Headers(
        {CONTENT_TYPE: RSS_KIND} | ({} if coding == "identity" else {CONTENT_ENCODING: coding})
    )
    feed_sites.serve(FEED, answered(compressed(feed_of_bytes(LOWERED_FEED_BYTES), coding), headers))
    fetched = fetched_feed(FEED, Validators())
    assert isinstance(fetched, Changed), (
        f"a feed of {LOWERED_FEED_BYTES} bytes, the most allowed, coded {coding}, gave {fetched}"
    )
    beyond = compressed(feed_of_bytes(LOWERED_FEED_BYTES + 1), coding)
    feed_sites.serve(FEED, answered(beyond, headers))
    with pytest.raises(UnreadableFeed) as refused:
        fetched = fetched_feed(FEED, Validators())
        pytest.fail(
            f"a feed of {LOWERED_FEED_BYTES + 1} bytes, {len(beyond)} as sent coded {coding}, was "
            f"read as {fetched}, beyond the {LOWERED_FEED_BYTES} allowed"
        )
    assert str(refused.value) == OVERSIZE_SOURCE.format(source=FEED), (
        f"a feed beyond the bytes allowed was refused as {str(refused.value)!r}"
    )


def assert_overdue(feed_sites: FeedSites, what: str, most_seconds: float) -> None:
    began = time.monotonic()
    with pytest.raises(UnreadableFeed) as refused:
        fetched = fetched_feed(FEED, Validators())
        pytest.fail(f"{what} was read as {fetched}, beyond the time allowed")
    took = time.monotonic() - began
    assert str(refused.value) == OVERDUE_SOURCE.format(source=FEED), (
        f"{what} was refused as {str(refused.value)!r}"
    )
    assert took <= most_seconds, f"{what} was given up after {took:.1f} seconds"


TRICKLE_LIMITS = 5


def test_a_feed_that_trickles_beyond_the_time_allowed_is_overdue(feed_sites: FeedSites) -> None:
    opening = RELATIVE_FEED.removesuffix(FEED_CLOSING)
    seconds = TRICKLE_LIMITS * LOWERED_FEED_SECONDS
    feed_sites.serve(FEED, trickling(opening, RSS_KIND, seconds, FEED_CLOSING))
    assert_overdue(
        feed_sites,
        f"a feed that trickles for {seconds} seconds",
        LOWERED_FEED_SECONDS + OVERRUN_SECONDS,
    )


def test_the_time_allowed_holds_from_the_request_not_from_the_answer(
    feed_sites: FeedSites,
) -> None:
    # A server that thinks for two thirds of the time allowed, then sends its feed in another
    # two thirds: each alone is allowed, both are not.
    part = 2 * LOWERED_FEED_SECONDS / 3
    opening = RELATIVE_FEED.removesuffix(FEED_CLOSING)
    feed_sites.serve(FEED, slow(trickling(opening, RSS_KIND, part, FEED_CLOSING), part))
    assert_overdue(
        feed_sites,
        f"a feed that a server began to send after {part:.1f} seconds and sent in {part:.1f}",
        2 * part + OVERRUN_SECONDS,
    )


REDIRECTS = 4


def test_the_time_allowed_holds_for_every_redirect(feed_sites: FeedSites) -> None:
    # Redirects, each a server's answer after it thought, that take longer than allowed in all.
    each = LOWERED_FEED_SECONDS / (REDIRECTS - 1)
    stops = [FEED, *(f"https://cdn.test/moved/{number}" for number in range(REDIRECTS))]
    for stop, onward in pairwise(stops):
        feed_sites.serve(stop, slow(moved(onward), each))
    feed_sites.serve(stops[-1], answered(RELATIVE_FEED, kind_of(RSS_KIND)))
    assert_overdue(
        feed_sites,
        f"a feed reached through {REDIRECTS} redirects of {each:.1f} seconds each",
        REDIRECTS * each + OVERRUN_SECONDS,
    )


# P5: a failure of the connection.

TRANSPORT_FAILURES = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)


@EXAMPLES
@given(kind=st.sampled_from(TRANSPORT_FAILURES), in_body=st.booleans())
def test_a_failure_of_the_connection_is_left_as_it_is_raised(
    feed_sites: FeedSites, kind: type[httpx.TransportError], in_body: bool
) -> None:
    feed_sites.forget()
    raised: list[httpx.TransportError] = []
    opening = RELATIVE_FEED.removesuffix(FEED_CLOSING)
    feed_sites.serve(
        FEED, broken_off(opening, RSS_KIND, kind, raised) if in_body else failing(kind, raised)
    )
    with pytest.raises(kind) as failed:
        fetched_feed(FEED, Validators())
    assert raised and failed.value is raised[-1], (
        f"a connection that failed with {kind.__name__} "
        f"{'while the feed was sent' if in_body else 'before an answer'} was told as "
        f"{failed.value!r}, not as the error raised"
    )


LONG_ADDRESS = "https://feeds.test/" + "a" * ADDRESS_CHARACTERS


@pytest.mark.xfail(
    strict=True,
    reason="being mended: a feed moved to an address longer than a feed's address may be is "
    "read there",
)
def test_an_address_that_leads_to_one_longer_than_a_feeds_address_may_be_is_unreadable(
    feed_sites: FeedSites,
) -> None:
    feed_sites.serve(FEED, moved(LONG_ADDRESS))
    feed_sites.serve(LONG_ADDRESS, answered(RELATIVE_FEED, kind_of(RSS_KIND)))
    with pytest.raises(UnreadableFeed):
        fetched = fetched_feed(FEED, Validators())
        pytest.fail(
            f"a feed moved to an address of {len(LONG_ADDRESS)} characters was read as {fetched}"
        )
