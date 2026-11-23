import subprocess
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st
from playwright.sync_api import Error as PlaywrightError
from pydantic import SecretStr

from infonium import browser, guard, intake, speech, webpage
from infonium.document import (
    BROKEN_OFF_SOURCE,
    HTML_MEDIA_TYPE,
    LITTLE_TEXT_WORDS,
    NO_TEXT,
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
