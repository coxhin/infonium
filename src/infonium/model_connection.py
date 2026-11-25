import logging
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from http import HTTPStatus
from threading import Condition, Lock
from typing import Any

import httpx2
from openai import DefaultHttpxClient

# How a request tells the HTTP library to report the steps of its sending, the steps after
# which a connection stands (named after whatever makes it: the connection itself, an HTTPS
# proxy's tunnel or a SOCKS proxy), and what that connection is asked for its socket. Each
# such step may move the connection to a socket of its own, as encryption does; the last
# one is the socket the reply is read from.
TRACE = "trace"
CONNECTED_STEPS = frozenset(
    {"connect_tcp.complete", "connect_unix_socket.complete", "start_tls.complete"}
)
STEP_SEPARATOR = "."
CONNECTION = "return_value"
SOCKET_INFO = "socket"
REQUEST_HOOK = "request"
RESPONSE_HOOK = "response"
REPLY_ENDED = "the reply was ended before this request was sent"
# A request that may be cut is sent on a connection of its own, kept for no later request,
# so that its connection is known while the endpoint has yet to answer and a cut ends that
# request alone. The library speaks HTTP/1.1 only unless it is told otherwise, so one
# connection carries one request.
OWN_CONNECTIONS = httpx2.Limits(max_keepalive_connections=0)
SECONDS_PER_MINUTE = 60.0
# How long the requests to an endpoint wait after it refused one for being asked too often:
# the first pause, and the longest it grows to while requests sent after a pause began are
# refused too.
FIRST_PAUSE_SECONDS = 5.0
LONGEST_PAUSE_SECONDS = 60.0
# How often a request waiting for its turn looks whether its reply has ended.
TURN_CHECK_SECONDS = 1.0
# Where a request keeps how many pauses its pace had begun when the request was sent.
PAUSES_BEGUN = "infonium.pauses_begun"
PAUSED = "%s refused a request for being asked too often: its requests wait %.0f s"

logger = logging.getLogger(__name__)


class ReplyEnded(Exception):
    pass


def cut_off(connection: socket.socket) -> None:
    # An SSL socket's own shutdown drops its TLS state, which the reading thread still uses;
    # the plain socket's wakes that thread's read with the end of the connection. A socket
    # that encryption has taken over, or that is closed already, has nothing left to cut.
    with suppress(OSError):
        socket.socket.shutdown(connection, socket.SHUT_RDWR)


class Ending:
    # Ends a reply from any thread: the connections of its requests are cut, so that the
    # read the replying thread waits in ends at once, whether the endpoint has begun to
    # answer or not. A request of the reply that connects afterwards is cut as it connects,
    # and one that is about to be sent is not sent.
    def __init__(self) -> None:
        self.turn = Lock()
        self.ended = False
        self.connections: list[socket.socket] = []

    def end(self) -> None:
        with self.turn:
            self.ended = True
            for connection in self.connections:
                cut_off(connection)

    def traced(self, step: str, details: dict[str, Any]) -> None:
        if step.split(STEP_SEPARATOR, 1)[-1] in CONNECTED_STEPS:
            connection = details[CONNECTION].get_extra_info(SOCKET_INFO)
            with self.turn:
                self.connections.append(connection)
                if self.ended:
                    cut_off(connection)

    @contextmanager
    def requesting(self) -> Iterator[None]:
        # The requests sent meanwhile from this thread are this reply's.
        token = REPLY_ENDING.set(self)
        try:
            yield
        finally:
            REPLY_ENDING.reset(token)


# The ending of the reply whose request the current thread sends, for the hook that every
# request of a client made by endable_http_client passes through. A client without that hook
# makes no connection to cut; a reply over it ends at its next event.
REPLY_ENDING: ContextVar[Ending] = ContextVar("reply_ending")


def traced_request(request: httpx2.Request) -> None:
    ending = REPLY_ENDING.get()
    if ending.ended:
        raise ReplyEnded(REPLY_ENDED)
    request.extensions[TRACE] = ending.traced


class Pace:
    # Spaces the requests to one endpoint, sent by every client that shares the pace: each is
    # sent at least an interval after the one before, so that no more than so many are sent
    # in a minute, the client's own retries among them. When the endpoint refuses a request for
    # being asked too often, no request is sent before a pause has passed. The pause doubles,
    # up to the longest, while requests sent after it began are refused too, and is over with
    # the first of them that is not. An answer to a request sent before the last pause began
    # tells nothing of the endpoint since.
    def __init__(self, requests_per_minute: int) -> None:
        self.interval = SECONDS_PER_MINUTE / requests_per_minute
        # Held while the pace is read or changed, and let go while a request waits.
        self.turn = Condition()
        self.next_turn = time.monotonic()
        self.pause = 0.0
        self.pauses = 0

    def wait_turn(self, request: httpx2.Request) -> None:
        # A request whose reply ends while it waits is not sent.
        ending = REPLY_ENDING.get()
        with self.turn:
            while (now := time.monotonic()) < self.next_turn:
                if ending.ended:
                    raise ReplyEnded(REPLY_ENDED)
                self.turn.wait(min(self.next_turn - now, TURN_CHECK_SECONDS))
            self.next_turn = now + self.interval
            request.extensions[PAUSES_BEGUN] = self.pauses

    def heard(self, response: httpx2.Response) -> None:
        refused = response.status_code == HTTPStatus.TOO_MANY_REQUESTS
        with self.turn:
            news = response.request.extensions[PAUSES_BEGUN] == self.pauses
            if news and refused:
                doubled = min(2 * self.pause, LONGEST_PAUSE_SECONDS)
                self.pause = doubled if self.pause else FIRST_PAUSE_SECONDS
                self.pauses += 1
                self.next_turn = max(self.next_turn, time.monotonic() + self.pause)
                logger.warning(PAUSED, response.request.url.host, self.pause)
            elif news:
                self.pause = 0.0


def endable_http_client() -> httpx2.Client:
    # The HTTP client of a model whose replies an Ending can cut.
    return DefaultHttpxClient(limits=OWN_CONNECTIONS, event_hooks={REQUEST_HOOK: [traced_request]})


def paced_http_client(pace: Pace) -> httpx2.Client:
    # The HTTP client of a model of an endpoint whose requests the pace spaces.
    return DefaultHttpxClient(
        event_hooks={REQUEST_HOOK: [pace.wait_turn], RESPONSE_HOOK: [pace.heard]}
    )


def endable_paced_http_client(pace: Pace) -> httpx2.Client:
    # The same, whose replies an Ending can cut; a request waits for its turn before it is
    # traced, so that one whose reply ended meanwhile is not sent.
    return DefaultHttpxClient(
        limits=OWN_CONNECTIONS,
        event_hooks={REQUEST_HOOK: [pace.wait_turn, traced_request], RESPONSE_HOOK: [pace.heard]},
    )
