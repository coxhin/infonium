import logging
import time
import zlib
from collections.abc import Callable, Collection, Mapping
from enum import StrEnum
from http import HTTPStatus
from threading import Event, Thread
from types import MappingProxyType
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, Field

from infonium import guard
from infonium.document import (
    CONTENT_TYPE,
    FETCH_TIMEOUT_SECONDS,
    OVERDUE_SOURCE,
    OVERSIZE_SOURCE,
    UNDECLARED,
    USER_AGENT,
    Tagged,
    error_status,
    error_text,
    turned_away,
)
from infonium.subscriptions.feed import Feed, UnreadableFeed, read_feed

# The most a feed may hold, counted as the bytes it inflates to, and the longest a poll may
# take in all: its status, its headers and every redirect among it. A poll follows as many
# redirects as httpx does.
FEED_LIMIT_BYTES = 20 * 1024**2
FEED_LIMIT_SECONDS = 60.0
MOST_REDIRECTS = 20
# What an answer of an error is read of, for what it says.
REFUSAL_LIMIT_BYTES = 1024**2
# A feed is asked for in the kinds feeds are served as, then as XML and JSON, then as anything:
# a server that tells kinds apart sends its feed, and one that does not sends what it has.
ACCEPT_FEEDS = (
    "application/rss+xml, application/atom+xml, application/feed+json,"
    " application/rdf+xml;q=0.9, application/xml;q=0.9, text/xml;q=0.9,"
    " application/json;q=0.8, */*;q=0.5"
)
USER_AGENT_HEADER = "user-agent"
ACCEPT = "accept"
# A feed is asked for compressed as gzip or deflate, once at most, and an answer compressed
# otherwise or more than once is refused. What is compressed is taken as it arrives and
# inflated by zlib no further than the bound, so that what an answer that passes it makes the
# fetcher hold is the bound and one piece: inflated by httpx, a piece is inflated whole, and
# 591 bytes gzipped twice took 627 MiB before a byte was counted.
ACCEPT_ENCODING = "accept-encoding"
CONTENT_ENCODING = "content-encoding"
INFLATED = ("gzip", "deflate")
NOT_COMPRESSED = "identity"
CODINGS_APART = ","
# zlib reads a gzip or a zlib stream told by its header; deflate that some servers send
# without the zlib header is read raw.
WRAPPED_WINDOW = 32 + zlib.MAX_WBITS
RAW_DEFLATE_WINDOW = -zlib.MAX_WBITS
DEFLATE = "deflate"
UNASKED_CODING = "{source} is compressed as {coding}, which infonium does not ask for"
UNINFLATED = "{source} is compressed as {coding} in bytes that do not inflate"
IF_NONE_MATCH = "if-none-match"
IF_MODIFIED_SINCE = "if-modified-since"
ETAG = "etag"
LAST_MODIFIED = "last-modified"
GET = "GET"
# What a reader is told of an address whose server could not be reached or read from: httpx's
# own words name neither the address nor what failed.
UNREACHED = "{address} could not be fetched: {kind}: {said}"
TOO_MANY_REQUESTS = "{source} leads on through more redirects than infonium follows"
# A query that a request carries and that no address the fetcher tells or keeps shows, as a key
# that a server asks for: it is added to every request to the server first asked, and taken out
# of the address of every request and answer, and of those httpx writes to its log, where it
# writes each request's address whole.
NOTHING_HIDDEN: Mapping[str, str] = MappingProxyType({})
HIDDEN_NAMES: set[str] = set()
HTTPX_LOG = "httpx"


class Oversized(UnreadableFeed):
    # An answer that holds more than its bound.
    pass


class Validators(BaseModel):
    # Where a subscription's fetcher stopped: what the server said of the feed it last sent, by
    # which it can say the next time that the feed has not changed since.
    etag: str | None = None
    modified: str | None = None


class FetchedKind(StrEnum):
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class Changed(Tagged):
    # The feed as it is served now, and where the fetcher stops with it.
    kind: Literal[FetchedKind.CHANGED] = FetchedKind.CHANGED
    feed: Feed
    validators: Validators


class Unchanged(Tagged):
    # The server says that the feed is as it was where the fetcher stopped.
    kind: Literal[FetchedKind.UNCHANGED] = FetchedKind.UNCHANGED


Fetched = Annotated[Changed | Unchanged, Field(discriminator="kind")]


class Limit(BaseModel):
    # The most an answer may hold once inflated, and the longest it may take from its request.
    most_bytes: int
    most_seconds: float


class Answer(BaseModel):
    # What an address answered with: the address the answer came from after redirects, its
    # status, the kind it declares, its bytes, and the validators it gives.
    address: str
    status: int
    declared: str
    content: bytes
    validators: Validators


class Allowance:
    # What one exchange of a fetcher may still do: how many requests it may still make, each
    # redirect one, and the time on the monotonic clock by which it ends.
    def __init__(self, requests: int, ends: float) -> None:
        self.requests = requests
        self.ends = ends

    def left(self, addresses: list[str]) -> list[str]:
        # The first of the addresses given that may still be asked.
        return addresses[: max(self.requests, 0)]

    def spend(self, address: str) -> None:
        # One request taken, to the address given.
        if time.monotonic() > self.ends:
            raise UnreadableFeed(OVERDUE_SOURCE.format(source=address))
        if self.requests <= 0:
            raise UnreadableFeed(TOO_MANY_REQUESTS.format(source=address))
        self.requests -= 1


def ends_in(seconds: float) -> float:
    # The time on the monotonic clock the given seconds from now.
    return time.monotonic() + seconds


def request_headers(validators: Validators) -> dict[str, str]:
    held = [(IF_NONE_MATCH, validators.etag), (IF_MODIFIED_SINCE, validators.modified)]
    return {
        USER_AGENT_HEADER: USER_AGENT,
        ACCEPT: ACCEPT_FEEDS,
        ACCEPT_ENCODING: ", ".join(INFLATED),
        **{header: value for header, value in held if value},
    }


def unreached(address: str, failure: httpx.HTTPError | httpx.InvalidURL) -> str:
    # What a failure of the connection to an address, or of its address itself, is told as.
    return UNREACHED.format(address=address, kind=type(failure).__name__, said=error_text(failure))


def shown(url: httpx.URL, hidden: Collection[str]) -> str:
    # An address as the fetcher tells and keeps it: without the hidden query, or a fragment.
    for name in hidden:
        url = url.copy_remove_param(name)
    return str(url.copy_with(fragment=None))


class HiddenInLog(logging.Filter):
    # httpx's log as it is written, with the addresses in it as the fetcher tells them.
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                shown(given, HIDDEN_NAMES) if isinstance(given, httpx.URL) else given
                for given in record.args
            )
        return True


logging.getLogger(HTTPX_LOG).addFilter(HiddenInLog())


def same_server(url: httpx.URL, other: httpx.URL) -> bool:
    return (url.scheme, url.host, url.port) == (other.scheme, other.host, other.port)


def sent_back(value: str | None) -> str | None:
    # A validator as it can be sent back: visible ASCII, as a header is written; one that holds
    # another byte would break every later poll before it is sent, and is not kept.
    return value if value is not None and value.isascii() and value.isprintable() else None


def coding_of(response: httpx.Response, source: str) -> str:
    # The one compression of an answer, or none. One that infonium did not ask for, or more
    # than one, is refused.
    named = response.headers.get(CONTENT_ENCODING, NOT_COMPRESSED).split(CODINGS_APART)
    codings = [coding.strip().casefold() for coding in named]
    applied = [coding for coding in codings if coding and coding != NOT_COMPRESSED]
    if len(applied) > 1 or any(coding not in INFLATED for coding in applied):
        written = ", ".join(applied)
        raise UnreadableFeed(UNASKED_CODING.format(source=source, coding=written))
    return applied[0] if applied else NOT_COMPRESSED


def bounded_content(
    response: httpx.Response, source: str, coding: str, most_bytes: int, ends: float
) -> bytes:
    # The body of an answer, taken as it arrives and inflated no further than the bound, by the
    # time on the monotonic clock at which its fetch must have ended: an answer that only
    # trickles is still held to the time.
    content = bytearray()
    window = WRAPPED_WINDOW
    inflater = zlib.decompressobj(window) if coding != NOT_COMPRESSED else None
    for raw in response.iter_raw():
        pending = raw
        while pending and len(content) <= most_bytes:
            if inflater is None:
                content += pending[: most_bytes + 1 - len(content)]
                pending = b""
                continue
            try:
                content += inflater.decompress(pending, most_bytes + 1 - len(content))
            except zlib.error as failure:
                if coding != DEFLATE or window == RAW_DEFLATE_WINDOW or content:
                    said = UNINFLATED.format(source=source, coding=coding)
                    raise UnreadableFeed(said) from failure
                window = RAW_DEFLATE_WINDOW
                inflater = zlib.decompressobj(window)
                continue
            pending = inflater.unconsumed_tail
        if len(content) > most_bytes:
            raise Oversized(OVERSIZE_SOURCE.format(source=source))
        if time.monotonic() > ends:
            raise UnreadableFeed(OVERDUE_SOURCE.format(source=source))
    return bytes(content)


def exchanged(
    address: str,
    validators: Validators,
    allowance: Allowance,
    limit_of: Callable[[int, str], Limit],
    hidden: Mapping[str, str],
) -> Answer:
    # One GET of an address, made as a source's fetch is made, by infonium's user agent and
    # given up once the server is silent for as long. Redirects are followed one at a time,
    # each a request of the allowance. A guard's check is refused as the site turning infonium
    # away. The answer is read within the limit its status and the kind it declares are held
    # to, and the allowance's time.
    began = time.monotonic()
    headers = request_headers(validators)
    HIDDEN_NAMES.update(hidden)
    with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
        first = client.build_request(GET, address, headers=headers, params=hidden)
        request = first
        while True:
            allowance.spend(shown(request.url, hidden))
            response = client.send(request, stream=True)
            try:
                if response.next_request is not None:
                    request = response.next_request
                    if same_server(request.url, first.url):
                        request.url = request.url.copy_merge_params(hidden)
                    continue
                if guard.is_guarded(response.headers):
                    raise UnreadableFeed(turned_away(address))
                declared = response.headers.get(CONTENT_TYPE, UNDECLARED)
                limit = limit_of(response.status_code, declared)
                ends = min(allowance.ends, began + limit.most_seconds)
                coding = coding_of(response, address)
                content = bounded_content(response, address, coding, limit.most_bytes, ends)
                return Answer(
                    address=shown(response.url, hidden),
                    status=response.status_code,
                    declared=declared,
                    content=content,
                    validators=Validators(
                        etag=sent_back(response.headers.get(ETAG)),
                        modified=sent_back(response.headers.get(LAST_MODIFIED)),
                    ),
                )
            finally:
                response.close()


def answer_within(
    address: str,
    validators: Validators,
    allowance: Allowance,
    limit_of: Callable[[int, str], Limit],
    hidden: Mapping[str, str] = NOTHING_HIDDEN,
) -> Answer:
    # An exchange held to the allowance's time as a whole, its status, headers and redirects
    # among it: httpx bounds only each silence of a server, which one sending a byte at a time
    # never keeps. The exchange runs in a thread of its own, which is waited for no longer;
    # left behind, it ends with the next byte its server sends or with its silence.
    outcome: list[Answer | BaseException] = []
    done = Event()

    def exchange() -> None:
        try:
            outcome.append(exchanged(address, validators, allowance, limit_of, hidden))
        except BaseException as failure:
            outcome.append(failure)
        finally:
            done.set()

    Thread(target=exchange, daemon=True).start()
    if not done.wait(max(allowance.ends - time.monotonic(), 0.0)):
        raise UnreadableFeed(OVERDUE_SOURCE.format(source=address))
    match outcome[0]:
        case Answer() as answer:
            return answer
        case failure:
            raise failure


def feed_limit(status: int, declared: str) -> Limit:
    # A poll reads a feed, whatever kind it declares, and of an error what it may say.
    served = status in (HTTPStatus.OK, HTTPStatus.NOT_MODIFIED)
    most_bytes = FEED_LIMIT_BYTES if served else REFUSAL_LIMIT_BYTES
    return Limit(most_bytes=most_bytes, most_seconds=FEED_LIMIT_SECONDS)


def changed(answer: Answer) -> Changed:
    # The feed an answer holds, read at the address it came from as the kind it declares.
    return Changed(
        feed=read_feed(answer.content, answer.declared, answer.address),
        validators=answer.validators,
    )


def fetched_feed(address: str, validators: Validators) -> Fetched:
    # One poll of a feed: it is asked for from where the fetcher stopped, and read where the
    # server sends it. This is the shape of a fetcher: what it is set to and where it stopped
    # go in, what its source lists now and where it stops come out. A failure to connect, and
    # a server silent for too long, are left as httpx raises them.
    allowance = Allowance(requests=1 + MOST_REDIRECTS, ends=ends_in(FEED_LIMIT_SECONDS))
    answer = answer_within(address, validators, allowance, feed_limit)
    if answer.status == HTTPStatus.NOT_MODIFIED:
        return Unchanged()
    if answer.status != HTTPStatus.OK:
        raise UnreadableFeed(error_status(answer.address, answer.status))
    return changed(answer)
