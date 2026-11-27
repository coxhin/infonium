import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx

# The sites that feeds and pages are fetched from while a test polls or finds a feed, answered
# in the test's own process as each test scripts them: what each address answers, and every
# request heard, each redirect a request of its own. httpx's client and httpx.stream answer from
# them.

Answer = Callable[[httpx.Request], httpx.Response]
CONTENT_TYPE = "content-type"
LOCATION = "location"
HTML = "text/html; charset=utf-8"
# The limits of a feed's fetch, of a page's and of a discovery, lowered so that they are met in
# a second or two and in a megabyte; a page's below a feed's, as the requirements have them,
# and a discovery's room for a page and two feeds.
LOWERED_FEED_SECONDS = 1.5
LOWERED_FEED_BYTES = 1024**2
LOWERED_PAGE_SECONDS = 1.0
LOWERED_PAGE_BYTES = 256 * 1024
LOWERED_DISCOVERY_SECONDS = 3.0
# How often a trickling answer sends a little more. A trickle ends at last, so that a limit that
# is not kept fails the test rather than holding it.
TRICKLE_SECONDS = 0.1


def not_found(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404, headers={CONTENT_TYPE: HTML}, content=iter([b"<p>Not here.</p>"]))


class FeedSites:
    def __init__(self) -> None:
        self.answers: dict[str, Answer] = {}
        self.requests: list[httpx.Request] = []

    def serve(self, address: str, answer: Answer) -> None:
        self.answers[str(httpx.URL(address))] = answer

    def forget(self) -> None:
        # The sites as they were before a test scripted them, for each example of a property.
        self.answers.clear()
        self.requests.clear()

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answers.get(str(request.url), not_found)(request)

    def asked(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def client(self) -> type[httpx.Client]:
        # httpx's client, answering every request from these sites, whatever transport or mounts
        # the code that makes it gives it.
        transport = httpx.MockTransport(self.answer)

        class StandinClient(httpx.Client):
            def __init__(self, *given: Any, **options: Any) -> None:
                super().__init__(*given, **(options | {"transport": transport, "mounts": None}))

        return StandinClient

    @contextmanager
    def stream(self, method: str, url: httpx.URL | str, **options: Any) -> Iterator[httpx.Response]:
        with self.client()() as client, client.stream(method, url, **options) as response:
            yield response


def answered(content: bytes, headers: httpx.Headers, status: int = 200) -> Answer:
    # An answer of the bytes given, sent as a server streams them, with the headers given, whose
    # values may hold any byte.
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, content=iter([content]))

    return answer


def kind_of(kind: str) -> httpx.Headers:
    return httpx.Headers({CONTENT_TYPE: kind})


def status_only(status: int, headers: httpx.Headers) -> Answer:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, content=iter([]))

    return answer


def moved(to: str, status: int = 301) -> Answer:
    return status_only(status, httpx.Headers({LOCATION: to}))


def trickling(opening: bytes, kind: str, seconds: float, closing: bytes = b"") -> Answer:
    # An answer that sends its opening at once and a few spaces every tenth of a second for
    # the seconds given, then its closing.
    def drip() -> Iterator[bytes]:
        yield opening
        for _ in range(round(seconds / TRICKLE_SECONDS)):
            time.sleep(TRICKLE_SECONDS)
            yield b"    "
        yield closing

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={CONTENT_TYPE: kind}, content=drip())

    return answer


def slow(answer: Answer, seconds: float) -> Answer:
    # An answer whose server thinks for the seconds given before it begins to answer.
    def thought(request: httpx.Request) -> httpx.Response:
        time.sleep(seconds)
        return answer(request)

    return thought


def failing(kind: type[httpx.TransportError], raised: list[httpx.TransportError]) -> Answer:
    # An answer that never comes: the connection fails with the error of the kind given, which
    # is kept so that a test can tell it from any other.
    def answer(request: httpx.Request) -> httpx.Response:
        raised.append(kind(f"the stand-in failed with {kind.__name__}", request=request))
        raise raised[-1]

    return answer


def broken_off(
    opening: bytes, kind: str, error: type[httpx.TransportError], raised: list[httpx.TransportError]
) -> Answer:
    # An answer whose connection fails after its opening was sent.
    def body(request: httpx.Request) -> Iterator[bytes]:
        yield opening
        raised.append(error(f"the stand-in broke off with {error.__name__}", request=request))
        raise raised[-1]

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={CONTENT_TYPE: kind}, content=body(request))

    return answer
