import time
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from http import HTTPStatus
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, Field

from infonium import guard
from infonium.document import OVERDUE_SOURCE, OVERSIZE_SOURCE, Tagged, error_status, turned_away
from infonium.intake import CONTENT_TYPE, FETCH_TIMEOUT_SECONDS, UNDECLARED, USER_AGENT
from infonium.subscriptions.feed import Feed, UnreadableFeed
from infonium.subscriptions.feedprocess import read_in_process

# The most a feed may hold, counted as the bytes it inflates to, and the longest its fetch may
# take in all.
FEED_LIMIT_BYTES = 20 * 1024**2
FEED_LIMIT_SECONDS = 60.0
# A feed is asked for in the kinds feeds are served as, then as XML and JSON, then as anything:
# a server that tells kinds apart sends its feed, and one that does not sends what it has.
ACCEPT_FEEDS = (
    "application/rss+xml, application/atom+xml, application/feed+json,"
    " application/rdf+xml;q=0.9, application/xml;q=0.9, text/xml;q=0.9,"
    " application/json;q=0.8, */*;q=0.5"
)
USER_AGENT_HEADER = "user-agent"
ACCEPT = "accept"
# A feed is asked for compressed only as gzip or deflate, and an answer compressed otherwise is
# refused: httpx inflates each piece of an answer whole before it is counted, and a piece of
# brotli may inflate to gigabytes (a bomb of 1,617 bytes took 2.2 GB to inflate to its 1 GiB),
# where one of gzip or deflate inflates at most about a thousandfold.
ACCEPT_ENCODING = "accept-encoding"
CONTENT_ENCODING = "content-encoding"
INFLATED = ("gzip", "deflate")
NOT_COMPRESSED = "identity"
CODINGS_APART = ","
UNASKED_CODING = "{source} is compressed as {coding}, which infonium does not ask for"
IF_NONE_MATCH = "if-none-match"
IF_MODIFIED_SINCE = "if-modified-since"
ETAG = "etag"
LAST_MODIFIED = "last-modified"
GET = "GET"
# The answers a poll reads: a feed, and word that it has not changed.
ANSWERED = frozenset({HTTPStatus.OK, HTTPStatus.NOT_MODIFIED})


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


class Answer(BaseModel):
    # What an address answered with: the address the answer came from after redirects, the
    # kind it declares, its bytes, and the validators it gives.
    address: str
    declared: str
    content: bytes
    validators: Validators


def request_headers(validators: Validators) -> dict[str, str]:
    held = [(IF_NONE_MATCH, validators.etag), (IF_MODIFIED_SINCE, validators.modified)]
    return {
        USER_AGENT_HEADER: USER_AGENT,
        ACCEPT: ACCEPT_FEEDS,
        ACCEPT_ENCODING: ", ".join(INFLATED),
        **{header: value for header, value in held if value},
    }


@contextmanager
def asked(address: str, validators: Validators) -> Iterator[httpx.Response]:
    # One GET of an address, made as a source's fetch is made: by infonium's user agent,
    # following redirects, and given up once the server is silent for as long. A guard's check
    # is refused as the site turning infonium away, and an answer compressed in a way infonium
    # did not ask for as that compression; an answer of any status is given.
    with httpx.stream(
        GET,
        address,
        follow_redirects=True,
        timeout=FETCH_TIMEOUT_SECONDS,
        headers=request_headers(validators),
    ) as response:
        if guard.is_guarded(response.headers):
            raise UnreadableFeed(turned_away(address))
        codings = response.headers.get(CONTENT_ENCODING, NOT_COMPRESSED).split(CODINGS_APART)
        unasked = {coding.strip().casefold() for coding in codings} - {*INFLATED, NOT_COMPRESSED}
        if unasked:
            coding = ", ".join(sorted(unasked))
            raise UnreadableFeed(UNASKED_CODING.format(source=address, coding=coding))
        yield response


@contextmanager
def answer_to(address: str, validators: Validators) -> Iterator[httpx.Response]:
    # An address asked once, as asked does, any status but 200 and 304 refused as that status.
    with asked(address, validators) as response:
        if response.status_code not in ANSWERED:
            raise UnreadableFeed(error_status(str(response.url), response.status_code))
        yield response


def bounded_content(response: httpx.Response, source: str, most_bytes: int, ends: float) -> bytes:
    # The body of an answer, counted as it inflates, by the time on the monotonic clock at which
    # its fetch must have ended. Bytes are taken as they arrive, so that an answer that only
    # trickles is still held to the time.
    content = bytearray()
    for chunk in response.iter_bytes():
        content += chunk
        if len(content) > most_bytes:
            raise UnreadableFeed(OVERSIZE_SOURCE.format(source=source))
        if time.monotonic() > ends:
            raise UnreadableFeed(OVERDUE_SOURCE.format(source=source))
    return bytes(content)


def answer_of(response: httpx.Response, content: bytes) -> Answer:
    return Answer(
        address=str(response.url.copy_with(fragment=None)),
        declared=response.headers.get(CONTENT_TYPE, UNDECLARED),
        content=content,
        validators=Validators(
            etag=response.headers.get(ETAG), modified=response.headers.get(LAST_MODIFIED)
        ),
    )


def changed(answer: Answer) -> Changed:
    # The feed an answer holds, read at the address it came from as the kind it declares, by a
    # process of its own.
    return Changed(
        feed=read_in_process(answer.content, answer.declared, answer.address),
        validators=answer.validators,
    )


def fetched_feed(address: str, validators: Validators) -> Fetched:
    # One poll of a feed: it is asked for from where the fetcher stopped, and read where the
    # server sends it. This is the shape of a fetcher: what it is set to and where it stopped
    # go in, what its source lists now and where it stops come out. A failure to connect, and
    # a server silent for too long, are left as httpx raises them.
    ends = time.monotonic() + FEED_LIMIT_SECONDS
    with answer_to(address, validators) as response:
        if response.status_code == HTTPStatus.NOT_MODIFIED:
            return Unchanged()
        answer = answer_of(response, bounded_content(response, address, FEED_LIMIT_BYTES, ends))
    return changed(answer)
