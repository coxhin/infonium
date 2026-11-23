import string
from urllib.parse import urlencode

import httpx
from hypothesis import assume, given
from hypothesis import strategies as st

from infonium import guard
from infonium.document import LITTLE_TEXT_WORDS, WORD_GAP

ASKED = "https://news.example.test/2026/11/night-trains"
SHORT_BODY = "<p>Press and hold the button to go on.</p>"
CHECK_ELEMENT_IDS = ("anubis_challenge", "px-captcha")

words = st.text(st.characters(categories=["Ll", "Lu", "Nd"]), min_size=1, max_size=8)
gaps = st.text(st.sampled_from(" \t\n"), min_size=1, max_size=3)
header_values = st.text(st.sampled_from(string.ascii_letters + string.digits), min_size=1)
little = st.integers(0, LITTLE_TEXT_WORDS - 1)
check_titles = st.sampled_from(sorted(guard.CHECK_TITLES))
web_addresses = st.builds(
    lambda scheme, host, path: f"{scheme}://{host}.test/{path}",
    st.sampled_from(["http", "https"]),
    words,
    words,
)


def served(
    title: str, body: str = SHORT_BODY, *, words: int, asked: str = ASKED, at: str = ASKED
) -> guard.ServedPage:
    content = f"<html><head><title>{title}</title></head><body>{body}</body></html>"
    return guard.ServedPage(asked=asked, served=at, content=content.encode(), words=words)


@st.composite
def written_as(draw: st.DrawFn, title: str) -> str:
    # The title as a page may write it: each letter in either case, any spacing between words.
    letters = [draw(st.sampled_from([c.lower(), c.upper()])) for c in title]
    spaced = "".join(letters).split(WORD_GAP)
    gaps_between = [draw(gaps) for _ in spaced]
    return draw(gaps) + "".join(
        f"{word}{gap}" for word, gap in zip(spaced, gaps_between, strict=True)
    )


@st.composite
def check_addresses(draw: st.DrawFn) -> str:
    # The address of a check that names itself in its host or its path and carries in its query
    # the address to go on to.
    where = draw(st.sampled_from(["{word}.captcha.test/verify", "{word}.test/mp/appmsgCAPTCHA"]))
    query = urlencode({"poc_token": draw(words), "target_url": draw(web_addresses)})
    return f"https://{where.format(word=draw(words))}?{query}"


@given(
    title=st.one_of(check_titles, words),
    at=st.one_of(check_addresses(), st.just(ASKED)),
    element=st.sampled_from(CHECK_ELEMENT_IDS),
    count=st.integers(LITTLE_TEXT_WORDS, 10 * LITTLE_TEXT_WORDS),
)
def test_a_page_with_the_words_of_an_article_is_never_a_check(
    title: str, at: str, element: str, count: int
) -> None:
    page = served(title, f"<div id='{element}'></div>", at=at, words=count)
    assert not guard.is_check_page(page), f"a page of {count} words was taken for a check"


@given(title=check_titles.flatmap(written_as), count=little)
def test_a_short_page_with_a_check_title_is_a_check(title: str, count: int) -> None:
    assert guard.is_check_page(served(title, words=count)), (
        f"a short page titled {title!r}, a guard's check title, was not taken for a check"
    )


@given(
    title=check_titles,
    before=st.sampled_from(["", "Quiz: "]),
    after=st.sampled_from(["", " - My blog", " | Acme Consulting"]),
    count=little,
)
def test_a_title_that_only_holds_a_check_title_is_not_a_check(
    title: str, before: str, after: str, count: int
) -> None:
    assume(before or after)
    page = served(f"{before}{title}{after}", words=count)
    assert not guard.is_check_page(page), f"a short page titled {page.content!r} was a check"


@given(at=check_addresses(), title=words, count=little)
def test_a_short_page_at_an_address_that_names_a_check_is_a_check(
    at: str, title: str, count: int
) -> None:
    assert guard.is_check_page(served(title, at=at, words=count)), (
        f"a short page served from the check's address {at} was not taken for a check"
    )


@given(at=check_addresses(), title=words, count=little)
def test_an_address_asked_for_that_names_a_check_itself_is_no_sign(
    at: str, title: str, count: int
) -> None:
    page = served(title, asked=at.split("?")[0], at=at, words=count)
    assert not guard.is_check_page(page), f"{at}, asked for itself, was taken for a check"


@given(
    at=st.sampled_from(
        [
            "https://example.test/posts/why-captcha-fails",
            "https://captcha.example.test/privacy",
            "https://example.test/article?captcha=1",
            "https://example.test/article?next=https://example.test/captcha",
        ]
    ),
    title=words,
    count=little,
)
def test_an_address_that_names_a_check_without_one_to_go_on_to_is_no_sign(
    at: str, title: str, count: int
) -> None:
    assert not guard.is_check_page(served(title, at=at, words=count)), (
        f"a short page at {at}, which carries no address to go on to, was taken for a check"
    )


@given(element=st.sampled_from(CHECK_ELEMENT_IDS), title=words, count=little)
def test_a_short_page_holding_a_checks_element_is_a_check(
    element: str, title: str, count: int
) -> None:
    page = served(title, f"<div id='{element}'></div>{SHORT_BODY}", words=count)
    assert guard.is_check_page(page), f"a short page holding #{element} was not a check"


@given(title=words, count=little)
def test_a_short_page_without_a_sign_is_not_a_check(title: str, count: int) -> None:
    assume(title.casefold() not in guard.CHECK_TITLES)
    assert not guard.is_check_page(served(title, words=count)), (
        f"a short page titled {title!r}, with no guard's sign, was taken for a check"
    )


@given(
    headers=st.dictionaries(st.sampled_from(["content-type", "server", "x-cache"]), header_values),
    guarded=st.sampled_from([*guard.GUARD_HEADERS, *(h.upper() for h in guard.GUARD_HEADERS)]),
)
def test_a_guards_header_marks_its_answer(headers: dict[str, str], guarded: str) -> None:
    assert not guard.is_guarded(httpx.Headers(headers)), "an ordinary answer was taken as guarded"
    assert guard.is_guarded(httpx.Headers({**headers, guarded: "challenge"})), (
        f"an answer carrying {guarded} was not taken as a guard's"
    )
