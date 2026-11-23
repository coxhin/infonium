import socket
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from threading import Lock
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
REPLY_ENDED = "the reply was ended before this request was sent"
# A request that may be cut is sent on a connection of its own, kept for no later request,
# so that its connection is known while the endpoint has yet to answer and a cut ends that
# request alone. The library speaks HTTP/1.1 only unless it is told otherwise, so one
# connection carries one request.
OWN_CONNECTIONS = httpx2.Limits(max_keepalive_connections=0)


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


def endable_http_client() -> httpx2.Client:
    # The HTTP client of a model whose replies an Ending can cut.
    return DefaultHttpxClient(limits=OWN_CONNECTIONS, event_hooks={REQUEST_HOOK: [traced_request]})
