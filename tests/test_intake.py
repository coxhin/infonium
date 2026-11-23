import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
import openai
import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from playwright.sync_api import Error as PlaywrightError
from pydantic import SecretStr

from infonium import browser, guard, intake, speech, webpage
from infonium.document import (
    BROKEN_OFF_SOURCE,
    HTML_MEDIA_TYPE,
    LITTLE_TEXT_WORDS,
    NO_TEXT,
    WORD_GAP,
    UnreadableSource,
    turned_away,
    word_count,
)

URL = "https://example.test/posts/night-train"
PLAIN_FILE = "source"
ENGINES = intake.Engines(
    pdf=Path("mineru"),
    recogniser=speech.Recogniser(
        base_url="http://127.0.0.1:1/v1", api_key=SecretStr("unused"), model="none"
    ),
    browser=Path("google-chrome"),
)
VOCABULARY = ("river", "sleeper", "ledger", "harbour", "window", "morning", "letter", "valley")
# What a stand-in browser may fail with besides UnreadableSource: what Python, Playwright and
# a program that ends badly raise.
OTHER_FAILURE_KINDS = (ValueError, OSError, RuntimeError, KeyError, TimeoutError, PlaywrightError)

RenderedPageFunction = Callable[[str, Path, Path], browser.RenderedPage]


def page(words: int, title: str = "A post about trains") -> bytes:
    text = " ".join(VOCABULARY[number % len(VOCABULARY)] for number in range(words))
    body = f"<article><p>{text}</p></article>" if words else "<div id='root'></div>"
    return (
        f"<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"
    ).encode()


def page_text(content: bytes) -> webpage.PageText:
    return webpage.page_text(content, URL)


little_pages = st.integers(0, LITTLE_TEXT_WORDS - 1).map(page)
any_pages = st.integers(0, LITTLE_TEXT_WORDS + 50).map(page)
other_failures = st.one_of(
    st.builds(lambda kind, said: kind(said), st.sampled_from(OTHER_FAILURE_KINDS), st.text()),
    st.builds(
        subprocess.CalledProcessError, st.integers(-15, 255), st.just(list(browser.RENDERER))
    ),
)


def completed(content: bytes, broken: bool, broken_off: list[bool]) -> RenderedPageFunction:
    # A browser that renders the page as `content`.
    def rendered_page(url: str, directory: Path, program: Path) -> browser.RenderedPage:
        file = directory / browser.RENDERED_FILE
        file.write_bytes(content)
        return browser.RenderedPage(
            file=file, address=url, broken_off_by_network=broken, broken_off=broken_off
        )

    return rendered_page


def failing(failure: Exception) -> RenderedPageFunction:
    def rendered_page(url: str, directory: Path, program: Path) -> browser.RenderedPage:
        raise failure

    return rendered_page


def never_called(url: str, directory: Path, program: Path) -> browser.RenderedPage:
    raise AssertionError(f"a page whose HTML holds enough words was rendered: {url}")


def read_page(plain: bytes, rendered_page: RenderedPageFunction) -> intake.Readout:
    # The page read from URL, whose server answered with `plain`, in a browser stood in for.
    with TemporaryDirectory() as folder, patch.object(browser, "rendered_page", rendered_page):
        file = Path(folder) / PLAIN_FILE
        file.write_bytes(plain)
        source = intake.Source(file=file, media_type=HTML_MEDIA_TYPE, reference=URL)
        return intake.page_readout(URL, source, Path(folder), ENGINES)


@given(plain=little_pages, failure=other_failures)
def test_a_failure_other_than_an_unreadable_source_passes_through_unchanged(
    plain: bytes, failure: Exception
) -> None:
    with pytest.raises(type(failure)) as raised:
        read_page(plain, failing(failure))
    assert raised.value is failure, (
        f"reading a page replaced the browser's {type(failure).__name__} with {raised.value!r}"
    )


@given(plain=little_pages, reason=st.text())
def test_a_failed_rendering_keeps_the_plain_reading_exactly_when_it_has_text(
    plain: bytes, reason: str
) -> None:
    failure = UnreadableSource(reason)
    plain_text = page_text(plain)
    if plain_text.blocks:
        readout = read_page(plain, failing(failure))
        assert readout.file.name == PLAIN_FILE, "a failed rendering did not keep the plain page"
        assert readout.extracted.blocks == plain_text.blocks, (
            "a failed rendering kept another text than the plain reading's"
        )
    else:
        with pytest.raises(UnreadableSource) as raised:
            read_page(plain, failing(failure))
        assert raised.value is failure, (
            "a page without text whose rendering failed did not fail with the browser's reason"
        )


@given(plain=little_pages, title=st.sampled_from(sorted(guard.CHECK_TITLES)))
def test_a_check_answered_to_the_browser_is_a_failed_rendering(plain: bytes, title: str) -> None:
    check = page(5, title=title)
    plain_text = page_text(plain)
    if plain_text.blocks:
        readout = read_page(plain, completed(check, broken=False, broken_off=[False]))
        assert readout.extracted.blocks == plain_text.blocks, (
            "a check the browser was answered with replaced the plain reading"
        )
    else:
        with pytest.raises(UnreadableSource) as raised:
            read_page(plain, completed(check, broken=False, broken_off=[False]))
        assert str(raised.value) == turned_away(URL), (
            "a page without text whose rendering met a check did not fail as turned away"
        )


@given(
    plain=little_pages,
    rendered=any_pages,
    broken=st.booleans(),
    broken_off=st.lists(st.booleans(), min_size=1, max_size=browser.RENDERING_ATTEMPTS),
)
def test_a_completed_rendering_keeps_the_reading_with_more_words(
    plain: bytes, rendered: bytes, broken: bool, broken_off: list[bool]
) -> None:
    plain_text, rendered_text = page_text(plain), page_text(rendered)
    if plain_text.blocks or rendered_text.blocks:
        readout = read_page(plain, completed(rendered, broken, broken_off))
        rendered_kept = word_count(rendered_text.blocks) > word_count(plain_text.blocks)
        fuller, file = (
            (rendered_text, browser.RENDERED_FILE) if rendered_kept else (plain_text, PLAIN_FILE)
        )
        assert (readout.file.name, readout.extracted.blocks) == (file, fuller.blocks), (
            "a completed rendering did not keep the reading with more words"
        )
    else:
        # With no text on either side, the reason is the network's when it broke the page off.
        reason = BROKEN_OFF_SOURCE if broken else NO_TEXT
        with pytest.raises(UnreadableSource) as raised:
            read_page(plain, completed(rendered, broken, broken_off))
        assert str(raised.value) == reason.format(source=URL), (
            f"a page without text, broken off by the network: {broken}, failed with the "
            f"wrong reason {raised.value}"
        )


@given(plain=st.integers(LITTLE_TEXT_WORDS, 2 * LITTLE_TEXT_WORDS).map(page))
def test_a_page_whose_html_holds_enough_words_is_read_without_the_browser(plain: bytes) -> None:
    readout = read_page(plain, never_called)
    assert readout.extracted.blocks == page_text(plain).blocks, (
        "a page whose HTML holds enough words was not read from its HTML"
    )


ERROR_KINDS = (ValueError, OSError, RuntimeError, KeyError, UnreadableSource)
REASON_LENGTH = intake.REASON_LIMIT_CHARACTERS + len(intake.CUT_SHORT)
one_line_texts = (
    st.text(st.characters(exclude_categories=["Cs", "Cc", "Z"]), min_size=1)
    .map(str.strip)
    .filter(bool)
)


def is_one_line(reason: str) -> bool:
    return reason == WORD_GAP.join(reason.split())


@given(kind=st.sampled_from(ERROR_KINDS), said=st.text())
def test_a_reason_is_one_line_a_person_reads(kind: type[Exception], said: str) -> None:
    reason = intake.failure_reason(kind(said))
    assert is_one_line(reason), f"a reason was not given on one line: {reason!r}"
    assert len(reason) <= REASON_LENGTH, f"a reason of {len(reason)} characters was kept whole"


@given(kind=st.sampled_from(ERROR_KINDS), said=st.text())
def test_a_reason_says_something(kind: type[Exception], said: str) -> None:
    assume(said.strip() or not said)
    assert intake.failure_reason(kind(said)), f"an error saying {said!r} was given no reason"


@pytest.mark.xfail(
    strict=True,
    reason="an error whose text is only spaces or line breaks is given an empty reason: "
    "failure_reason names the error's kind only when its text is empty",
)
@given(kind=st.sampled_from(ERROR_KINDS), said=st.text(st.sampled_from(" \t\n\x85"), min_size=1))
def test_an_error_that_says_only_spaces_is_given_a_reason(kind: type[Exception], said: str) -> None:
    assert intake.failure_reason(kind(said)), f"an error saying {said!r} was given no reason"


@given(lines=st.lists(one_line_texts, min_size=1, max_size=4), status=st.integers(1, 255))
def test_a_program_that_fails_is_given_by_the_last_line_it_wrote(
    lines: list[str], status: int
) -> None:
    written = "".join(f"[aac @ 0x5581d0a0] {line}\n \n" for line in lines)
    error = subprocess.CalledProcessError(status, ["ffmpeg", "-i", "source"], stderr=written)
    expected = WORD_GAP.join(lines[-1].split())[: intake.REASON_LIMIT_CHARACTERS]
    assert intake.failure_reason(error).removesuffix(intake.CUT_SHORT) == expected, (
        "a program that failed was not given by the last line it wrote"
    )


@given(status=st.integers(1, 255), module=st.sampled_from(["infonium.browser", "speech"]))
def test_a_program_that_fails_without_a_word_is_named_with_how_it_ended(
    status: int, module: str
) -> None:
    silent = subprocess.CalledProcessError(status, [sys.executable, "-m", module, "work"], "")
    overdue = subprocess.TimeoutExpired(["/usr/local/bin/mineru", "-p", "file.pdf"], 90.5)
    assert intake.failure_reason(silent) == f"{module} ended with status {status}", (
        "a program that ended without a word was not named with its status"
    )
    assert intake.failure_reason(overdue) == "mineru took longer than 90.5 seconds", (
        "a program that ran out of time was not named with its time"
    )


def unanswered(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


# A key a service is reached with, which neither a page of the service nor its address holds.
secrets = st.from_regex(r"[A-Za-z]{5,12}[0-9]{2}", fullmatch=True)


@given(secret=secrets, status=st.integers(400, 599))
def test_a_service_that_failed_is_given_without_the_secrets_of_its_address(
    secret: str, status: int
) -> None:
    page = "<html><body>" + "<p>The upstream did not answer in time.</p>\n" * 400 + "</body></html>"
    answered = httpx.MockTransport(lambda request: httpx.Response(status, html=page))
    for transport in (answered, httpx.MockTransport(unanswered)):
        client = openai.OpenAI(
            base_url=f"https://reader:{secret}@speech.test/v1",
            default_query={"key": secret},
            api_key="unused",
            max_retries=0,
            http_client=httpx.Client(transport=transport),
        )
        with pytest.raises(openai.APIError) as raised:
            client.audio.transcriptions.create(file=("a.wav", b"RIFF", "audio/wav"), model="m")
        reason = intake.failure_reason(raised.value)
        assert secret not in reason, f"a reason holds a secret of the address it names: {reason}"
        assert reason.startswith("https://speech.test/v1/audio/transcriptions "), (
            f"a reason does not name the address that was asked: {reason}"
        )
        assert is_one_line(reason) and len(reason) <= REASON_LENGTH, "a reason is not one line"
