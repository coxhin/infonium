import sys
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from typing import ClassVar
from unittest.mock import Mock, patch

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from playwright.sync_api import BrowserContext
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as WaitOverdue
from pydantic import BaseModel

from infonium import browser
from infonium.document import UnreadableSource
from infonium.egress import LOOPBACK, PublicProxy, Refusal

URL = "https://example.test/app"
PROGRAM = "/opt/google/chrome/chrome"
# A port of the range this suite may take.
LOCAL_PAGE_PORT = 28850
REPLACEMENT = "\N{REPLACEMENT CHARACTER}"
SURROGATES = frozenset(map(chr, range(0xD800, 0xE000)))
# Marks that an address may hold without a space, among them every error Chrome names.
ADDRESS_MARKS = [
    mark for mark in (*browser.CHROME_ERRORS, browser.PROXY_FAILURE) if " " not in mark
]
CALL_LOG = '\nCall log:\n  - navigating to "{address}", waiting until "commit"\n'

texts = st.text()
texts_with_surrogates = st.text(st.one_of(st.characters(), st.characters(categories=["Cs"])))
surrogates = st.characters(categories=["Cs"])
one_lines = st.text(st.characters(exclude_categories=["Cs", "Cc", "Zl", "Zp"]), min_size=1).filter(
    lambda line: line.strip() and line == line.splitlines()[0]
)
address_parts = st.text(st.characters(exclude_categories=["Cs", "Cc", "Z"]), max_size=12)
addresses = st.builds(
    lambda scheme, parts: f"{scheme}://example.test/{'/'.join(parts)}",
    st.sampled_from(["http", "https"]),
    st.lists(st.one_of(address_parts, st.sampled_from(ADDRESS_MARKS)), max_size=4),
)


def proxy_that_refused(refused: list[Refusal]) -> Mock:
    # The proxy of a rendering, which refused these connections and no others.
    proxy = Mock(spec=PublicProxy)
    proxy.refusals.return_value = refused
    return proxy


def code_units(text: str) -> str:
    # `text` as a page's scripts hold it: one character for each of its UTF-16 code units.
    units = text.encode("utf-16-le")
    return "".join(
        chr(int.from_bytes(units[at : at + 2], "little")) for at in range(0, len(units), 2)
    )


def ending_reason(ending: PlaywrightError | UnreadableSource) -> str:
    return browser.PageRendering(URL, PROGRAM, proxy_that_refused([])).ending_reason(ending)


@given(text=texts_with_surrogates)
def test_wellformed_text_is_utf8(text: str) -> None:
    halves = [character for character in browser.wellformed(text) if character in SURROGATES]
    assert not halves, (
        f"wellformed left halves of UTF-16 pairs, which UTF-8 cannot hold: {halves!r}"
    )


@given(text=texts)
def test_wellformed_keeps_text_without_surrogates(text: str) -> None:
    assert browser.wellformed(text) == text, "wellformed changed text that UTF-8 holds"


@given(text=texts)
def test_wellformed_joins_the_halves_of_utf16_pairs(text: str) -> None:
    assert browser.wellformed(code_units(text)) == text, (
        "wellformed did not make one character of each UTF-16 pair"
    )


@given(before=texts, lone=surrogates, after=texts)
def test_wellformed_puts_one_replacement_for_each_lone_half(
    before: str, lone: str, after: str
) -> None:
    halves = code_units(before) + lone + code_units(after)
    assert browser.wellformed(halves) == before + REPLACEMENT + after, (
        "wellformed did not replace a lone half of a UTF-16 pair, and only it, with U+FFFD"
    )


@given(mark=st.sampled_from(list(browser.CHROME_ERRORS)), address=addresses)
def test_a_chrome_error_is_worded_by_its_mark_whatever_the_address_holds(
    mark: str, address: str
) -> None:
    error = PlaywrightError(f"Page.goto: {mark} at {address}" + CALL_LOG.format(address=address))
    expected = browser.CHROME_ERRORS[mark].format(source=URL, program=PROGRAM)
    assert ending_reason(error) == expected, f"Chrome's {mark} was not worded as its mark says"


@given(name=st.from_regex(r"net::ERR_[A-Z_]{3,24}", fullmatch=True), address=addresses)
def test_an_unnamed_chrome_error_is_given_by_its_name(name: str, address: str) -> None:
    assume(not any(mark in name for mark in (*browser.CHROME_ERRORS, browser.PROXY_FAILURE)))
    error = PlaywrightError(f"Page.goto: {name} at {address}" + CALL_LOG.format(address=address))
    expected = browser.OTHER_CHROME_ERROR.format(source=URL, error=name)
    assert ending_reason(error) == expected, f"Chrome's {name} was not named in the reason"


@given(address=addresses, other=addresses)
def test_an_address_names_no_error(address: str, other: str) -> None:
    first = (
        f'Page.goto: Navigation to "{address}" is interrupted by another navigation to "{other}"'
    )
    error = PlaywrightError(first + CALL_LOG.format(address=address))
    assert ending_reason(error) == first, "a mark inside an address was read as Chrome's error"


@given(address=addresses)
def test_a_page_that_does_not_answer_in_time_is_said_so(address: str) -> None:
    error = WaitOverdue(f"Page.goto: Timeout 30000ms exceeded.{CALL_LOG.format(address=address)}")
    expected = browser.NO_ANSWER.format(
        source=URL, seconds=browser.ANSWER_LIMIT_MS / browser.MS_PER_SECOND
    )
    assert ending_reason(error) == expected, "a page that did not answer was not said to be late"


@given(said=st.lists(one_lines, max_size=3))
def test_a_browser_that_ended_is_given_by_its_last_line(said: list[str]) -> None:
    assume(not any(browser.CALL_LOG in line or browser.BROWSER_LOGS in line for line in said))
    written = "".join(f"[pid=4242][err] [1123/093000.1:ERROR:x.cc(7)] {line}\n" for line in said)
    error = PlaywrightError(
        "BrowserType.launch_persistent_context: Target page, context or browser has been "
        f"closed\nBrowser logs:\n\n<launching> {PROGRAM} --no-first-run\n<launched> pid=4242\n"
        f"{written}{browser.CALL_LOG}\n  - <launching> {PROGRAM}\n"
    )
    expected = (
        f"the browser {PROGRAM} ended: {said[-1]}"
        if said
        else f"the browser {PROGRAM} ended without saying why"
    )
    assert ending_reason(error) == expected, "a browser that ended was not given by its last line"


@given(first=one_lines, rest=texts)
def test_infoniums_own_reason_is_its_first_line(first: str, rest: str) -> None:
    assert ending_reason(UnreadableSource(f"{first}\n{rest}")) == first, (
        "a rendering infonium ended was not given by the first line of its reason"
    )


@pytest.mark.parametrize(
    ("hop", "host", "port"),
    [
        ("http://127.0.0.1:28850/", "127.0.0.1", 28850),
        ("https://intranet.test/wiki", "intranet.test", 443),
        ("http://[::1]:8080/x", "::1", 8080),
        ("http://router.test/", "router.test", 80),
    ],
)
def test_a_page_the_proxy_refused_is_given_the_proxys_reason(
    hop: str, host: str, port: int
) -> None:
    refused = Refusal(host=host, port=port, reason=f"{host} is not a public address")
    other = Refusal(host="elsewhere.test", port=port, reason="elsewhere.test did not answer")
    rendering = browser.PageRendering(URL, PROGRAM, proxy_that_refused([refused, other]))
    rendering.failed_hops.append(browser.FailedHop(url=hop, failure=browser.PROXY_FAILURE))
    error = PlaywrightError(
        f"Page.goto: {browser.PROXY_FAILURE} at {hop}" + CALL_LOG.format(address=hop)
    )
    assert rendering.ending_reason(error) == f"{hop} could not be opened: {refused.reason}", (
        "a page whose address the proxy refused was not given the proxy's reason"
    )


class Attempt(BaseModel):
    # How one attempt at a page comes out: taken or failed, broken off by the network or not,
    # and how much a taken page holds.
    taken: bool
    broken: bool
    length: int


class ScriptedRendering(browser.PageRendering):
    # A rendering whose attempts come out as scripted, in place of a browser's.
    def __init__(self, attempts: list[Attempt]) -> None:
        super().__init__(URL, PROGRAM, proxy_that_refused([]))
        self.attempts = attempts
        # Of each attempt made, what it raises when it fails.
        self.made: list[PlaywrightError] = []

    def attempt(self, context: BrowserContext) -> browser.TakenPage:
        scripted = self.attempts[len(self.made)]
        self.made.append(PlaywrightError(f"attempt {len(self.made)} failed"))
        self.broken = [f"{URL}/app.js"] if scripted.broken else []
        if not scripted.taken:
            raise self.made[-1]
        return browser.TakenPage(
            content="x" * scripted.length, address=URL, broken_off_by_network=scripted.broken
        )


attempts = st.lists(
    st.builds(Attempt, taken=st.booleans(), broken=st.booleans(), length=st.integers(0, 50)),
    min_size=browser.RENDERING_ATTEMPTS,
    max_size=browser.RENDERING_ATTEMPTS,
)


def attempted(rendering: ScriptedRendering, deadline: float) -> browser.AttemptedPage:
    # The browser's context is asked for nothing: the scripted attempts stand in for its pages.
    with patch.object(browser, "wait_for_quiet_network", lambda until: None):
        return rendering.attempted_page(Mock(spec=BrowserContext), deadline)


@given(scripted=attempts)
def test_each_attempt_is_noted_and_the_fullest_kept(scripted: list[Attempt]) -> None:
    rendering = ScriptedRendering(scripted)
    try:
        page = attempted(rendering, time.monotonic() + browser.RENDERING_LIMIT_SECONDS)
    except PlaywrightError as failure:
        made = scripted[: len(rendering.made)]
        assert not any(attempt.taken for attempt in made), "a page was taken and then dropped"
        assert failure is rendering.made[-1], "the failure of the last attempt was not given"
        return
    made = scripted[: len(page.broken_off)]
    assert len(rendering.made) == len(page.broken_off), "an attempt made was not noted"
    assert page.broken_off == [attempt.broken for attempt in made], (
        "an attempt was noted with whether the network broke it off wrongly"
    )
    assert all(page.broken_off[:-1]), "the page was rendered again after a whole attempt"
    assert len(made) == browser.RENDERING_ATTEMPTS or not made[-1].broken, (
        "the attempts ended while the network still broke them off and attempts were left"
    )
    whole = made[-1].taken and not made[-1].broken
    taken = [attempt.length for attempt in made if attempt.taken]
    expected = made[-1].length if whole else max(taken)
    assert len(page.page.content) == expected, "the page kept is not the whole or the fullest one"


@given(scripted=attempts)
def test_no_attempt_begins_that_the_time_left_cannot_hold(scripted: list[Attempt]) -> None:
    rendering = ScriptedRendering(scripted)
    deadline = time.monotonic() + browser.ATTEMPT_LIMIT_SECONDS - 1
    with suppress(PlaywrightError):
        attempted(rendering, deadline)
    assert len(rendering.made) == 1, "an attempt began that the time left could not hold"


@given(
    report=st.builds(
        browser.CompletedRendering,
        address=addresses,
        broken_off_by_network=st.booleans(),
        broken_off=st.lists(st.booleans(), min_size=1, max_size=browser.RENDERING_ATTEMPTS),
    ),
    content=st.binary(max_size=64),
)
def test_a_completed_rendering_reaches_the_caller_as_reported(
    report: browser.CompletedRendering, content: bytes
) -> None:
    def reported_rendering(url: str, work: Path, program: str) -> browser.RenderingReport:
        (work / browser.RENDERED_FILE).write_bytes(content)
        return browser.RENDERING_REPORT.validate_json(browser.RENDERING_REPORT.dump_json(report))

    with (
        TemporaryDirectory() as folder,
        patch.object(browser, "reported_rendering", reported_rendering),
    ):
        page = browser.rendered_page(URL, Path(folder), Path(sys.executable))
        assert page.file.read_bytes() == content, "the rendered page was not handed on"
    assert (page.address, page.broken_off_by_network, page.broken_off) == (
        report.address,
        report.broken_off_by_network,
        report.broken_off,
    ), "the caller was told otherwise than the renderer reported"


@given(reason=one_lines)
def test_an_aborted_rendering_reaches_the_caller_with_its_reason(reason: str) -> None:
    def reported_rendering(url: str, work: Path, program: str) -> browser.RenderingReport:
        return browser.AbortedRendering(reason=reason)

    with (
        TemporaryDirectory() as folder,
        patch.object(browser, "reported_rendering", reported_rendering),
        pytest.raises(UnreadableSource) as raised,
    ):
        browser.rendered_page(URL, Path(folder), Path(sys.executable))
    assert str(raised.value) == reason, "an aborted rendering's reason did not reach the caller"


class CountedRequests(BaseHTTPRequestHandler):
    # A page of this machine, which counts the requests that reach it.
    heard: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        self.heard.append(self.path)
        content = b"<html><body><p>A page of this machine.</p></body></html>"
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *arguments: object) -> None:
        pass


@pytest.mark.chrome
@pytest.mark.enable_socket
def test_a_page_in_the_browser_does_not_reach_this_machine(chrome: Path, tmp_path: Path) -> None:
    with ThreadingHTTPServer((LOOPBACK, LOCAL_PAGE_PORT), CountedRequests) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            with pytest.raises(UnreadableSource) as raised:
                browser.rendered_page(f"http://{LOOPBACK}:{LOCAL_PAGE_PORT}/", tmp_path, chrome)
        finally:
            server.shutdown()
    assert str(raised.value) == (
        f"http://{LOOPBACK}:{LOCAL_PAGE_PORT}/ could not be opened: "
        f"{LOOPBACK} is not a public address"
    ), f"a page of this machine was not refused by the proxy: {raised.value}"
    assert CountedRequests.heard == [], "the browser reached a page of this machine"
