import hashlib
from http import HTTPStatus

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# A browser that holds an answer already names the versions it holds when it asks for it
# again, and is told by a 304, without the body, that one of them is still the current one: a
# refresh of the reader then costs a round trip instead of the documents and sources it shows
# once more. Starlette answers that question for its static files alone, so the service answers
# it here, for every GET: a file names its version itself, and an answer of another kind that
# is sent whole is named by its bytes. An answer that streams has no version.
HELD_VERSIONS = "if-none-match"
VERSION = "etag"
CACHING = "cache-control"
# What a browser may do with an answer it is given the version of: keep it for its reader
# alone, and ask before each use whether it is still the current one.
ASK_BEFORE_USE = "private, no-cache"
# An answer that may not be kept, which is given no version.
NOT_KEPT = "no-store"
# Versions are compared as RFC 9110 compares them for If-None-Match, weakly: a weak version
# names an answer whatever its bytes are on the way, compressed or not.
WEAK = "W/"
VERSIONS_APART = ","
# The answers a browser may hold the current version of: a whole one, and a part of a file.
HOLDABLE = frozenset({HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT})
# The headers a 304 repeats of the answer it stands for: of those RFC 9110 asks it to repeat
# (section 15.4.5), the ones the service's answers carry.
REPEATED = frozenset({VERSION, CACHING})
# The parts of a request and of an answer, as ASGI names them.
HTTP_SCOPE = "http"
GET = "GET"
ANSWER_START = "http.response.start"
ANSWER_BODY = "http.response.body"


def version_of(body: bytes) -> str:
    return f'{WEAK}"{hashlib.sha256(body).hexdigest()}"'


def holds(held: str, version: str) -> bool:
    # Whether the versions a browser says it holds, as an If-None-Match lists them, name this
    # one.
    tags = {tag.strip().removeprefix(WEAK) for tag in held.split(VERSIONS_APART)}
    return version.removeprefix(WEAK) in tags


def given_version(start: Message, first: Message) -> str | None:
    # The version of an answer, from its start and the message that follows it, written into
    # its start with leave to keep it. An answer that is no success, that may not be kept or
    # that streams without a version of its own has none.
    headers = MutableHeaders(scope=start)
    if start["status"] not in HOLDABLE or NOT_KEPT in headers.get(CACHING, ""):
        return None
    if VERSION not in headers:
        if first["type"] != ANSWER_BODY or first.get("more_body", False):
            return None
        headers[VERSION] = version_of(first.get("body", b""))
    headers.setdefault(CACHING, ASK_BEFORE_USE)
    return headers[VERSION]


def not_modified(start: Message) -> Message:
    repeated = [(name, value) for name, value in start["headers"] if name.decode() in REPEATED]
    return {"type": ANSWER_START, "status": HTTPStatus.NOT_MODIFIED, "headers": repeated}


class Answer:
    # An answer to a GET on its way to the browser that asked, whose start waits for the
    # message after it, by which its version is known.
    def __init__(self, onward: Send, held: str) -> None:
        self.onward = onward
        self.held = held
        self.start: Message | None = None
        self.held_already = False

    async def send(self, message: Message) -> None:
        if message["type"] == ANSWER_START:
            self.start = message
        elif self.start is not None:
            start, self.start = self.start, None
            await self.begun(start, message)
        elif not self.held_already:
            await self.onward(message)

    async def begun(self, start: Message, first: Message) -> None:
        version = given_version(start, first)
        self.held_already = version is not None and holds(self.held, version)
        if self.held_already:
            await self.onward(not_modified(start))
            await self.onward({"type": ANSWER_BODY, "body": b""})
        else:
            await self.onward(start)
            await self.onward(first)


class Revalidation:
    # The service, answering a GET for what the browser holds the current version of by a 304.
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == HTTP_SCOPE and scope["method"] == GET:
            send = Answer(send, Headers(scope=scope).get(HELD_VERSIONS, "")).send
        await self.app(scope, receive, send)
