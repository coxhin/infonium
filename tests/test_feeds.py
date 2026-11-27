import codecs
import gzip
import json
import string
import subprocess
import sys
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import cache
from pathlib import Path
from types import FrameType
from typing import Any
from xml.sax.saxutils import escape

import httpx
import pytest
from hypothesis import Phase, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.subscriptions.feed import (
    ADDRESS_CHARACTERS,
    KEY_CHARACTERS,
    TITLE_CHARACTERS,
    Feed,
    Posting,
    UnreadableFeed,
    read_feed,
)
from tests.feed_reading_bound import PEAK_BYTES, SECONDS
from tests.feed_writing import (
    ATOM_NAMESPACE,
    BASES,
    CLIMBING_BASES,
    DAY_NAMES,
    JSON_FEED_VERSIONS,
    MONTH_NAMES,
    RECORDING_TYPES,
    SUMMARY_TITLE_CHARACTERS,
    XML_DECLARATION,
    XML_FORMS,
    Enclosure,
    FeedForm,
    Marked,
    SaidFeed,
    SaidPosting,
    ServedFeed,
    WrittenTime,
    absolute_addresses,
    absolute_addresses_of,
    addressed_feeds,
    addressed_postings,
    atom_body,
    bases,
    declared_kinds,
    expected_feed_title,
    expected_postings,
    expected_site,
    feed_addresses,
    forms,
    is_cut_from,
    json_feed,
    keyed_feeds,
    marked_texts,
    markup_of,
    moments,
    numeric_zone,
    offsets,
    padded,
    relative_addresses,
    relative_addresses_of,
    rss_body,
    said_feeds,
    said_postings,
    same_time,
    served_feeds,
    slugs,
    spaced_slugs,
    unmarked_text,
    unreadable_times,
    words,
    written_feed,
    written_text,
    youtube_feed,
)

ADDRESS = "https://feeds.test/feed.xml"
RSS_KIND = "application/rss+xml"
# A judgment of a fault that is known, marked as an expected failure, reports the first example
# that fails without shrinking it, which would take the time of a dozen judgments.
FAULT_EXAMPLES = settings(phases=(Phase.explicit, Phase.reuse, Phase.generate))


def read(served: ServedFeed) -> Feed:
    return read_feed(served.content, served.declared, served.address)


def refusal(content: bytes, declared: str, address: str, what: str) -> str:
    # The sentence that reading the bytes is refused with; the judgment fails where they are read
    # as a feed.
    with pytest.raises(UnreadableFeed) as refused:
        feed = read_feed(content, declared, address)
        pytest.fail(
            f"{what} was read as a feed titled {feed.title!r} with {len(feed.postings)} "
            "postings, not refused as no feed"
        )
    return str(refused.value)


def read_or_refused(content: bytes, declared: str, address: str) -> list[Feed]:
    # The feed read from the bytes, or none where they are refused as no feed, an end that the
    # requirements allow; any other end of the reading fails the judgment.
    read_feeds: list[Feed] = []
    with suppress(UnreadableFeed):
        read_feeds.append(read_feed(content, declared, address))
    return read_feeds


def titled(feed: SaidFeed, title: Marked | None) -> SaidFeed:
    return feed.model_copy(
        update={"postings": [p.model_copy(update={"title": title}) for p in feed.postings]}
    )


def postings_read(served: ServedFeed) -> list[Posting]:
    # The postings of a feed whose every posting has an address and a key of its own, each of
    # which reading must give.
    read_postings = read(served).postings
    assert len(read_postings) == len(served.said.postings), (
        f"of {len(served.said.postings)} postings of a {served.form} feed, each with a web "
        f"address and an id of its own, {len(read_postings)} were read"
    )
    return read_postings


# F2: a posting's address.


@given(served=served_feeds(keyed_feeds))
def test_a_posting_without_a_web_address_of_at_most_address_characters_is_left_out(
    served: ServedFeed,
) -> None:
    kept = [posting.key for posting in read(served).postings]
    expected = [
        posting.key for posting in expected_postings(served.said, served.form, served.address)
    ]
    assert kept == expected, (
        f"of a {served.form} feed, the postings {kept} were kept, where those with a web address "
        f"of at most {ADDRESS_CHARACTERS} characters are {expected}"
    )


@given(served=served_feeds(keyed_feeds))
def test_a_postings_address_is_its_first_recording_else_its_page_resolved_against_the_feed(
    served: ServedFeed,
) -> None:
    addresses = {posting.key: posting.address for posting in read(served).postings}
    expected = {
        posting.key: posting.address
        for posting in expected_postings(served.said, served.form, served.address)
    }
    assert addresses == expected, (
        f"the postings of a {served.form} feed at {served.address} were given the addresses "
        f"{addresses}, where their first enclosure of sound or video with a web address, else "
        f"their page, resolved against the feed's base and address, is {expected}"
    )


# Ids that are no web address: relative to any base, or absolute of another scheme.
NOT_WEB_IDS = ("12345", "post-12", "tag:feeds.test,2026:9", "urn:uuid:1b4e28ba-2fa1-11d2-883f")
not_web_ids = st.one_of(st.sampled_from(NOT_WEB_IDS), slugs)
PERMALINK_MARKS = ("", ' isPermaLink="true"', ' isPermaLink="false"')
NO_PERMALINK = PERMALINK_MARKS[2]


def guided_postings(guid: str, permalink: str, base: str | None, address: str) -> list[Posting]:
    # The postings read of a feed of one RSS posting without a link, of the guid given.
    item = f"<item><title>A posting</title><guid{permalink}>{guid}</guid></item>"
    channel = f"<channel{'' if base is None else f' xml:base={base!r}'}><title>A feed</title>"
    content = f'<rss version="2.0">{channel}{item}</channel></rss>'
    return read_feed(content.encode(), RSS_KIND, address).postings


@given(
    guid=absolute_addresses,
    permalink=st.sampled_from(PERMALINK_MARKS),
    base=bases,
    address=feed_addresses,
)
def test_an_rss_guid_that_is_a_web_permalink_is_the_page_of_a_posting_without_a_link(
    guid: str, permalink: str, base: str | None, address: str
) -> None:
    read_postings = guided_postings(guid, permalink, base, address)
    expected = (
        []
        if permalink == NO_PERMALINK
        else [Posting(key=guid, title="A posting", address=guid, published=None)]
    )
    assert read_postings == expected, (
        f"an RSS posting without a link whose guid{permalink} is {guid!r}, under the base "
        f"{base!r}, was read as {read_postings}: a guid is its page where it is a permalink and "
        "a web address, and only then"
    )


@pytest.mark.xfail(
    strict=True,
    reason="F2 and F3 as amended, not yet built: the guid of an RSS posting without a link, "
    "where it is no web address and not said to be no permalink, is joined to the feed's "
    "address or base and made the posting's address",
)
@settings(FAULT_EXAMPLES)
@given(
    guid=not_web_ids,
    permalink=st.sampled_from(PERMALINK_MARKS),
    base=bases,
    address=feed_addresses,
)
def test_an_rss_guid_that_is_no_web_address_is_never_a_postings_page(
    guid: str, permalink: str, base: str | None, address: str
) -> None:
    read_postings = guided_postings(guid, permalink, base, address)
    assert read_postings == [], (
        f"an RSS posting without a link whose guid{permalink} is {guid!r}, under the base "
        f"{base!r}, was read as {read_postings}: a guid that is no web address is never joined "
        "to a base or the feed's address, and the posting has no address"
    )


@pytest.mark.xfail(
    strict=True,
    reason="an Atom entry without a link whose id is no web address is given its id joined to "
    "the feed's address (or base) as its address, as feedparser takes an id for a permalink",
)
@settings(FAULT_EXAMPLES)
@given(entry_id=not_web_ids, address=feed_addresses)
def test_an_atom_entry_without_a_link_has_no_address_whatever_its_id(
    entry_id: str, address: str
) -> None:
    entry = f"<entry><id>{entry_id}</id><title>An entry</title></entry>"
    content = f'<feed xmlns="{ATOM_NAMESPACE}"><title>A feed</title>{entry}</feed>'
    read_postings = read_feed(content.encode(), "application/atom+xml", address).postings
    assert read_postings == [], (
        f"an Atom entry with neither a link nor an enclosure, whose id is {entry_id!r}, was read "
        f"as {read_postings}: it has no address and is left out"
    )


ENCLOSURE_FORMS = [
    pytest.param(
        FeedForm.RSS,
        marks=pytest.mark.xfail(
            strict=True,
            reason="the relative address of an RSS enclosure is resolved against the feed's "
            "address, never against the xml:base of its channel",
        ),
    ),
    FeedForm.ATOM,
]


@pytest.mark.parametrize("form", ENCLOSURE_FORMS)
@settings(FAULT_EXAMPLES)
@given(feed_base=st.sampled_from(BASES), written=relative_addresses, address=feed_addresses)
def test_an_enclosures_relative_address_is_resolved_against_the_base_the_feed_gives(
    form: FeedForm, feed_base: str, written: str, address: str
) -> None:
    enclosure = Enclosure(address=written, kind="audio/mpeg")
    posting = plain_posting(0, unmarked_text("An episode"), None).model_copy(
        update={"enclosures": [enclosure]}
    )
    said = SaidFeed(title=None, site=None, base=feed_base, postings=[posting])
    addresses = [p.address for p in read_feed(written_feed(said, form), "", address).postings]
    expected = [posting.address for posting in expected_postings(said, form, address)]
    assert addresses == expected, (
        f"a {form} enclosure at {written!r}, under the base {feed_base!r} of the feed at "
        f"{address}, was read at {addresses}, not {expected}"
    )


@st.composite
def feeds_of_postings_based(draw: st.DrawFn) -> SaidFeed:
    # Feeds of postings at relative pages, some with a base of their own, under a base of the
    # feed or none.
    postings = [
        plain_posting(number, unmarked_text("A posting"), draw(relative_addresses)).model_copy(
            update={"base": draw(bases)}
        )
        for number in range(draw(st.integers(1, 4)))
    ]
    return SaidFeed(title=None, site=None, base=draw(bases), postings=postings)


@pytest.mark.xfail(
    strict=True,
    reason="a posting's xml:base holds for the postings after it too, and one that is relative "
    "is not resolved against the feed's base",
)
@settings(FAULT_EXAMPLES)
@given(said=feeds_of_postings_based(), form=st.sampled_from(XML_FORMS), address=feed_addresses)
def test_a_postings_base_applies_to_it_alone_and_within_the_feeds(
    said: SaidFeed, form: FeedForm, address: str
) -> None:
    addresses = [p.address for p in read_feed(written_feed(said, form), "", address).postings]
    expected = [posting.address for posting in expected_postings(said, form, address)]
    assert addresses == expected, (
        f"the postings of a {form} feed at {address} under the base {said.base!r}, of the bases "
        f"{[posting.base for posting in said.postings]} and the pages "
        f"{[posting.page for posting in said.postings]}, were read at {addresses}, not {expected}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="a relative xml:base that climbs (../up/) is resolved as if it did not: its steps "
    "up are lost",
)
@settings(FAULT_EXAMPLES)
@given(
    base=st.sampled_from(CLIMBING_BASES),
    site=relative_addresses,
    pages=st.lists(relative_addresses, min_size=1, max_size=3),
    form=st.sampled_from(XML_FORMS),
    address=feed_addresses,
)
def test_a_base_that_climbs_is_resolved_as_written(
    base: str, site: str, pages: list[str], form: FeedForm, address: str
) -> None:
    postings = [
        plain_posting(number, unmarked_text("A posting"), page) for number, page in enumerate(pages)
    ]
    said = SaidFeed(title=None, site=site, base=base, postings=postings)
    feed = read_feed(written_feed(said, form), "", address)
    read_addresses = [feed.site, *(posting.address for posting in feed.postings)]
    expected = [
        expected_site(said, form, address),
        *(posting.address for posting in expected_postings(said, form, address)),
    ]
    assert read_addresses == expected, (
        f"a {form} feed at {address} under the base {base!r} was read with its site and pages "
        f"at {read_addresses}, not {expected}"
    )


@st.composite
def feeds_of_spaced_addresses(draw: st.DrawFn) -> SaidFeed:
    # Feeds whose pages, recordings and site are written with spaces in them and white space
    # around them.
    said = draw(keyed_feeds)
    spaced = padded(
        st.one_of(absolute_addresses_of(spaced_slugs), relative_addresses_of(spaced_slugs))
    )
    recordings = st.builds(
        Enclosure,
        address=padded(absolute_addresses_of(spaced_slugs)),
        kind=st.sampled_from(RECORDING_TYPES),
    )
    postings = [
        posting.model_copy(
            update={
                "page": draw(spaced),
                "enclosures": draw(st.lists(recordings, max_size=1)),
            }
        )
        for posting in said.postings
    ]
    return said.model_copy(update={"postings": postings, "site": draw(spaced)})


@pytest.mark.xfail(
    strict=True,
    reason="F2 as amended, not yet built: a posting whose address has a space in it is left "
    "out, where the space is to be written %20 and white space around it dropped",
)
@settings(FAULT_EXAMPLES)
@given(served=served_feeds(feeds_of_spaced_addresses()))
def test_an_address_is_kept_as_a_browser_asks_for_it(served: ServedFeed) -> None:
    feed = read(served)
    addresses = {posting.key: posting.address for posting in feed.postings}
    expected = {
        posting.key: posting.address
        for posting in expected_postings(served.said, served.form, served.address)
    }
    assert addresses == expected, (
        f"the postings of a {served.form} feed whose addresses have spaces in them and white "
        f"space around them were read at {addresses}, not as a browser asks for them: {expected}"
    )
    site = expected_site(served.said, served.form, served.address)
    assert feed.site == site, (
        f"a {served.form} feed that gives its site as {served.said.site!r} was given the site "
        f"{feed.site!r}, not as a browser asks for it: {site!r}"
    )


def test_a_youtube_postings_address_is_its_watch_page() -> None:
    channel = "UCa1b2c3d4e5f6g7h8i9j0kL"
    videos = ["Vid000000A1", "Vid000000B2", "Vid-00_00C3"]
    address = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel}"
    feed = read_feed(youtube_feed(channel, videos), "application/atom+xml", address)
    addresses = [posting.address for posting in feed.postings]
    expected = [f"https://www.youtube.com/watch?v={video}" for video in videos]
    assert addresses == expected, (
        f"the postings of YouTube's feed, whose videos are media:content of a Flash type, were "
        f"given the addresses {addresses}, not their watch pages {expected}"
    )


# F3: a posting's key.


@given(served=served_feeds())
def test_a_postings_key_is_its_id_else_its_address_and_a_long_one_its_sha256(
    served: ServedFeed,
) -> None:
    keys = [posting.key for posting in read(served).postings]
    expected = [
        posting.key for posting in expected_postings(served.said, served.form, served.address)
    ]
    assert keys == expected, (
        f"the postings of a {served.form} feed were keyed {keys}, where each is keyed by its id, "
        f"else its address, as the SHA-256 of it where it is longer than {KEY_CHARACTERS} "
        f"characters, and the first of two with one key is kept: {expected}"
    )


@st.composite
def feeds_saying_postings_again(draw: st.DrawFn) -> SaidFeed:
    # Postings each with an address and an id of its own, and after them copies of some, each
    # with the id of the posting it copies, another title and another page.
    said = draw(st.lists(said_postings(), min_size=1, max_size=4))
    postings = [addressed_postings(posting, number) for number, posting in enumerate(said)]
    copied = draw(st.lists(st.sampled_from(postings), min_size=1, max_size=4))
    copies = [
        posting.model_copy(
            update={
                "title": unmarked_text("A copy"),
                "page": f"https://feeds.test/again/{number}",
                "enclosures": [],
            }
        )
        for number, posting in enumerate(copied)
    ]
    return draw(said_feeds(st.just(postings + copies)))


@given(served=served_feeds(feeds_saying_postings_again()))
def test_of_two_postings_with_one_key_the_first_listed_is_kept(served: ServedFeed) -> None:
    read_postings = read(served).postings
    addresses = {posting.key: posting.address for posting in read_postings}
    expected = {
        posting.key: posting.address
        for posting in expected_postings(served.said, served.form, served.address)
    }
    assert len(addresses) == len(read_postings), (
        f"a {served.form} feed was read with two postings of one key: {read_postings}"
    )
    assert addresses == expected, (
        f"of the postings of a {served.form} feed that share a key, those at {addresses} were "
        f"kept, not the first listed of each, at {expected}"
    )


def test_a_json_feed_id_written_as_a_number_is_a_key_as_its_text() -> None:
    items = [{"id": 12, "url": "https://feeds.test/p/12", "title": "Twelve"}]
    content = json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": items})
    keys = [posting.key for posting in read_feed(content.encode(), "", ADDRESS).postings]
    assert keys == ["12"], f"a JSON Feed item of the id 12, a number, was keyed {keys}"


KEY_FORMS = [
    pytest.param(
        FeedForm.ATOM,
        marks=pytest.mark.xfail(
            strict=True,
            reason="the key of an Atom entry whose id is relative is the id joined to the "
            "xml:base in force, not the id as written",
        ),
    ),
    FeedForm.RSS,
    FeedForm.JSON_1_1,
]


@pytest.mark.parametrize("form", KEY_FORMS)
@settings(FAULT_EXAMPLES)
@given(
    said_ids=st.lists(not_web_ids, min_size=1, max_size=4, unique=True),
    base=st.sampled_from(BASES),
    address=feed_addresses,
)
def test_a_postings_key_is_its_id_as_written_under_any_base(
    form: FeedForm, said_ids: list[str], base: str, address: str
) -> None:
    postings = [
        plain_posting(
            number, unmarked_text("A posting"), f"https://feeds.test/p/{number}"
        ).model_copy(update={"id": said_id})
        for number, said_id in enumerate(said_ids)
    ]
    said = SaidFeed(title=None, site=None, base=base, postings=postings)
    keys = [posting.key for posting in read_feed(written_feed(said, form), "", address).postings]
    assert keys == said_ids, (
        f"the postings of a {form} feed under the base {base!r}, of the ids {said_ids}, were "
        f"keyed {keys}: a posting's key is its id"
    )


# F4: a posting's title.

titled_feeds = st.tuples(addressed_feeds(), marked_texts()).map(lambda said: titled(*said))
BLANK_TITLES = (
    None,
    unmarked_text(""),
    Marked(markup=" <i></i> ", unmarked="  ", plain=""),
)
untitled_feeds = st.tuples(addressed_feeds(), st.sampled_from(BLANK_TITLES)).map(
    lambda said: titled(*said)
)


@given(served=served_feeds(titled_feeds))
def test_a_postings_title_is_its_plain_text_cut_to_title_characters_with_an_ellipsis(
    served: ServedFeed,
) -> None:
    expected = expected_postings(served.said, served.form, served.address)
    for posting, said, plain in zip(
        postings_read(served), served.said.postings, expected, strict=True
    ):
        assert is_cut_from(posting.title, plain.called, TITLE_CHARACTERS), (
            f"a {served.form} posting titled {markup_of(said.title)!r} was called "
            f"{posting.title!r}, not its plain text {plain.called!r} cut to {TITLE_CHARACTERS} "
            "characters with an ellipsis"
        )


@given(served=served_feeds(untitled_feeds))
def test_a_posting_without_a_title_is_called_by_its_summary_or_content_else_its_address(
    served: ServedFeed,
) -> None:
    expected = expected_postings(served.said, served.form, served.address)
    for posting, said, plain in zip(
        postings_read(served), served.said.postings, expected, strict=True
    ):
        assert is_cut_from(posting.title, plain.called, plain.called_characters), (
            f"a {served.form} posting without a title, whose summary is "
            f"{markup_of(said.summary)!r} and content "
            f"{markup_of(said.content)!r}, was called {posting.title!r}, not "
            f"{plain.called!r} cut to {plain.called_characters} characters: the first "
            f"{SUMMARY_TITLE_CHARACTERS} of its summary's or content's text, else its address"
        )


@pytest.mark.xfail(
    strict=True,
    reason="the title and summary of a JSON Feed item are kept with their markup: references "
    "are resolved, elements are not removed",
)
@settings(FAULT_EXAMPLES)
@given(title=marked_texts(), summary=marked_texts(), address=feed_addresses)
def test_a_json_feed_items_title_and_summary_are_read_as_plain_text(
    title: Marked, summary: Marked, address: str
) -> None:
    items = [
        {"id": "1", "url": "https://feeds.test/p/1", "title": title.markup},
        {"id": "2", "url": "https://feeds.test/p/2", "summary": summary.markup},
    ]
    content = json.dumps({"version": JSON_FEED_VERSIONS[FeedForm.JSON_1_1], "items": items})
    titled_posting, summarised = read_feed(content.encode(), "", address).postings
    assert is_cut_from(titled_posting.title, title.plain, TITLE_CHARACTERS), (
        f"a JSON Feed item titled {title.markup!r} was called {titled_posting.title!r}, not its "
        f"plain text {title.plain!r} cut to {TITLE_CHARACTERS} characters"
    )
    assert is_cut_from(summarised.title, summary.plain, SUMMARY_TITLE_CHARACTERS), (
        f"a JSON Feed item without a title, whose summary is {summary.markup!r}, was called "
        f"{summarised.title!r}, not the first {SUMMARY_TITLE_CHARACTERS} characters of "
        f"{summary.plain!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="F4 as amended, not yet built: a title is cut between a letter and a mark that combines"
    " with it",
)
@settings(FAULT_EXAMPLES)
@given(
    lead=st.integers(0, 8),
    letter=st.sampled_from("eaoक"),
    marks=st.lists(st.sampled_from("̣́̈ि"), min_size=1, max_size=3),
    form=forms,
    address=feed_addresses,
)
def test_a_title_is_never_cut_between_a_letter_and_the_marks_that_combine_with_it(
    lead: int, letter: str, marks: list[str], form: FeedForm, address: str
) -> None:
    cluster = letter + "".join(marks)
    plain = "a" * lead + cluster * (TITLE_CHARACTERS // len(cluster) + 1)
    said = SaidFeed(
        title=None,
        site=None,
        base=None,
        postings=[plain_posting(0, unmarked_text(plain), "https://feeds.test/p/0")],
    )
    [posting] = read_feed(written_feed(said, form), "", address).postings
    assert is_cut_from(posting.title, plain, TITLE_CHARACTERS), (
        f"a {form} title of {len(plain)} characters, letters each with the marks {marks!r}, was "
        f"cut to {posting.title[-4:]!r}: cut as late as it can be without parting a letter from "
        "its marks"
    )


# Markup that is a whole HTML document, an empty one or a bare doctype, the words a document
# holds standing in it at {words}.
DOCUMENTS = (
    "<!DOCTYPE html><html><head></head><body><p>{words}</p></body></html>",
    "<html><body>{words}</body></html>",
    "<!DOCTYPE html>",
    "<!doctype html><html></html>",
    "<html><head></head><body></body></html>",
)


def documents_saying(said_words: str) -> st.SearchStrategy[Marked]:
    return st.sampled_from(DOCUMENTS).map(
        lambda document: Marked(
            markup=document.format(words=escape(said_words)),
            unmarked=document.format(words=escape(said_words)),
            plain=said_words if "{words}" in document else "",
        )
    )


@st.composite
def feeds_titled_by_documents(draw: st.DrawFn) -> SaidFeed:
    said = draw(addressed_feeds().filter(lambda feed: bool(feed.postings)))
    phrases = st.lists(words, min_size=1, max_size=5).map(" ".join)
    postings = [
        posting.model_copy(
            update={
                "title": draw(phrases.flatmap(documents_saying)),
                "summary": draw(phrases.flatmap(documents_saying)),
                "content": None,
            }
        )
        for posting in said.postings
    ]
    return said.model_copy(update={"postings": postings})


@pytest.mark.xfail(
    strict=True,
    reason="F4 as amended, not yet built: a title or summary that is an empty HTML document or a "
    "bare doctype fails the reading with lxml's ParserError or an AssertionError",
)
@settings(FAULT_EXAMPLES)
@given(served=served_feeds(feeds_titled_by_documents()))
def test_a_title_or_summary_that_is_a_whole_html_document_never_fails_the_reading(
    served: ServedFeed,
) -> None:
    feed = read(served)
    assert len(feed.postings) == len(served.said.postings), (
        f"of {len(served.said.postings)} postings of a {served.form} feed titled by HTML "
        f"documents, {len(feed.postings)} were read"
    )


@pytest.mark.xfail(
    strict=True,
    reason="F4 as amended, not yet built: a title or summary that is a whole HTML document is kept"
    " as its markup",
)
@settings(FAULT_EXAMPLES)
@given(served=served_feeds(feeds_titled_by_documents(), st.sampled_from(XML_FORMS)))
def test_a_title_or_summary_that_is_a_whole_html_document_reads_as_its_text_or_nothing(
    served: ServedFeed,
) -> None:
    expected = expected_postings(served.said, served.form, served.address)
    for posting, said, plain in zip(
        postings_read(served), served.said.postings, expected, strict=True
    ):
        assert is_cut_from(posting.title, plain.called, plain.called_characters), (
            f"a {served.form} posting titled {markup_of(said.title)!r}, with the summary "
            f"{markup_of(said.summary)!r}, was called {posting.title!r}, not {plain.called!r}"
        )


# F5: a posting's time.


@given(served=served_feeds(addressed_feeds()))
def test_a_postings_time_is_when_it_was_published_else_updated_in_the_zone_the_feed_gives(
    served: ServedFeed,
) -> None:
    expected = expected_postings(served.said, served.form, served.address)
    for posting, said, timed in zip(
        postings_read(served), served.said.postings, expected, strict=True
    ):
        assert same_time(posting.published, timed.published), (
            f"a {served.form} posting published {written_text(said.published)!r} and "
            f"updated {written_text(said.updated)!r} was given the time "
            f"{posting.published!r}, not {timed.published!r}"
        )


@st.composite
def feeds_of_unreadable_times(draw: st.DrawFn) -> SaidFeed:
    said = draw(addressed_feeds())
    times: list[tuple[WrittenTime, WrittenTime | None]] = draw(
        st.lists(
            st.tuples(unreadable_times, st.none() | unreadable_times),
            min_size=len(said.postings),
            max_size=len(said.postings),
        )
    )
    postings = [
        posting.model_copy(update={"published": published, "updated": updated})
        for posting, (published, updated) in zip(said.postings, times, strict=True)
    ]
    return said.model_copy(update={"postings": postings})


@given(served=served_feeds(feeds_of_unreadable_times()))
def test_a_time_that_cannot_be_read_is_none_and_leaves_no_posting_out(served: ServedFeed) -> None:
    timed = [
        (written_text(said.published), posting.published)
        for said, posting in zip(served.said.postings, postings_read(served), strict=True)
        if posting.published is not None
    ]
    assert not timed, f"times that cannot be read were read as {timed}"


def feed_published(written: str, form: FeedForm, address: str) -> datetime | None:
    # The time read of the one posting of a feed that says it was published at the time written.
    posting = plain_posting(0, unmarked_text("A posting"), "https://feeds.test/p/0").model_copy(
        update={"published": WrittenTime(text=written, meant=None)}
    )
    said = SaidFeed(title=None, site=None, base=None, postings=[posting])
    [read_posting] = read_feed(written_feed(said, form), "", address).postings
    return read_posting.published


def rfc822_text(moment: datetime, zone: str) -> str:
    day = DAY_NAMES[moment.weekday()]
    month = MONTH_NAMES[moment.month - 1]
    return f"{day}, {moment.day:02} {month} {moment.year} {moment.strftime('%H:%M:%S')} {zone}"


@pytest.mark.xfail(
    strict=True,
    reason="F5 as amended, not yet built: a time whose zone is written +hh:mm is read at the same "
    "clock time in UTC",
)
@settings(FAULT_EXAMPLES)
@given(moment=moments, offset=offsets, form=forms, address=feed_addresses)
def test_a_time_whose_zone_is_written_with_a_colon_is_its_moment_or_none(
    moment: datetime, offset: timedelta, form: FeedForm, address: str
) -> None:
    meant = moment.replace(microsecond=0, tzinfo=timezone(offset))
    written = rfc822_text(meant, numeric_zone(offset, ":"))
    published = feed_published(written, form, address)
    assert published is None or published == meant, (
        f"a {form} posting published {written!r} was given the time {published!r}, which is "
        f"neither {meant.isoformat()} nor none"
    )


UNREAD_ZONES = ("XYZ", "Moon", "QQQ", "+x0", "zone")


@pytest.mark.xfail(
    strict=True,
    reason="F5 as amended, not yet built: a time whose zone cannot be read is read at the same "
    "clock time in UTC, where it is to be none",
)
@settings(FAULT_EXAMPLES)
@given(moment=moments, zone=st.sampled_from(UNREAD_ZONES), form=forms, address=feed_addresses)
def test_a_time_whose_zone_cannot_be_read_is_none(
    moment: datetime, zone: str, form: FeedForm, address: str
) -> None:
    written = rfc822_text(moment, zone)
    published = feed_published(written, form, address)
    assert published is None, (
        f"a {form} posting published {written!r}, whose zone cannot be read, was given the time "
        f"{published!r}, a zone it did not say"
    )


# F6: the feed itself.


@given(served=served_feeds())
def test_a_feeds_title_is_its_plain_text_else_the_host_of_its_address(served: ServedFeed) -> None:
    title = read(served).title
    expected = expected_feed_title(served.said, served.address)
    assert is_cut_from(title, expected, TITLE_CHARACTERS), (
        f"a {served.form} feed at {served.address} titled "
        f"{markup_of(served.said.title)!r} was called {title!r}, not "
        f"{expected!r} cut to {TITLE_CHARACTERS} characters"
    )


@given(served=served_feeds())
def test_a_feeds_site_is_the_page_it_gives_resolved_and_none_where_it_gives_none(
    served: ServedFeed,
) -> None:
    site = read(served).site
    expected = expected_site(served.said, served.form, served.address)
    assert site == expected, (
        f"a {served.form} feed at {served.address} that gives its page as {served.said.site!r} "
        f"under the base {served.said.base!r} was given the site {site!r}, not {expected!r}"
    )


# F1: formats and encodings.


class Marking(StrEnum):
    DECLARATION = "its XML declaration"
    BYTE_ORDER_MARK = "a byte order mark"
    CHARSET = "the charset declared"
    NOTHING = "nothing"


# The encodings a feed may name, each with Python's codec of it and the letters of its own that
# a feed in it writes; none for an encoding that writes any letter.
ENCODINGS = {
    "UTF-8": ("utf-8", ""),
    "UTF-16": ("utf-16", ""),
    "GBK": ("gbk", "订阅文章标题摘要中文新闻评论"),
    "GB18030": ("gb18030", "订阅文章标题摘要中文新闻评论"),
    "Shift_JIS": ("shift_jis", "日本語のフィード記事カタカナひらがな"),
    "EUC-JP": ("euc_jp", "日本語のフィード記事カタカナひらがな"),
    "Big5": ("big5", "繁體中文訂閱標題與評論"),
    "EUC-KR": ("euc_kr", "한국어제목기사구독"),
    "KOI8-R": ("koi8_r", "Русскийзаголовоклента"),
    "windows-1251": ("cp1251", "Русскийзаголовоклента"),
    "windows-1252": (
        "cp1252",
        "Caféàçñüß€\N{EN DASH}\N{LEFT DOUBLE QUOTATION MARK}\N{RIGHT DOUBLE QUOTATION MARK}",
    ),
    "ISO-8859-1": ("latin-1", "Caféàçñüß"),
    "ISO-8859-7": ("iso8859_7", "Ελληνικάτίτλος"),
}
# The encodings a charset names, of bytes without a byte order mark.
UNMARKED_ENCODINGS = sorted(set(ENCODINGS) - {"UTF-16"})
BYTE_ORDER_MARKS = {
    "utf-8": codecs.BOM_UTF8,
    "utf-16-le": codecs.BOM_UTF16_LE,
    "utf-16-be": codecs.BOM_UTF16_BE,
}
OTHER_CHARSETS = ("", "ISO-8859-1", "Shift_JIS", "windows-1251", "UTF-8")


def plain_posting(number: int, title: Marked | None, page: str | None) -> SaidPosting:
    return SaidPosting(
        id=f"tag:feeds.test,2026:{number}",
        title=title,
        summary=None,
        content=None,
        page=page,
        page_relation="alternate",
        other_links=[],
        enclosures=[],
        published=None,
        updated=None,
        base=None,
    )


class EncodedFeed(BaseModel):
    said: SaidFeed
    form: FeedForm
    encoding: str
    marking: Marking
    content: bytes
    declared: str


def letters_of(encoding: str) -> st.SearchStrategy[str]:
    own = ENCODINGS[encoding][1] if encoding in ENCODINGS else ""
    alphabet = st.sampled_from(own + string.ascii_letters)
    return st.text(alphabet, min_size=1, max_size=10) if own else words


@st.composite
def encoded_feeds(draw: st.DrawFn, markings: tuple[Marking, ...]) -> EncodedFeed:
    # A feed whose titles are in letters of an encoding, written in it, and the encoding
    # marked as given; declared, besides, as in another charset where a mark names it.
    marking = draw(st.sampled_from(markings))
    form = draw(st.sampled_from(XML_FORMS if marking is Marking.DECLARATION else tuple(FeedForm)))
    named = {
        Marking.DECLARATION: sorted(ENCODINGS),
        Marking.BYTE_ORDER_MARK: sorted(BYTE_ORDER_MARKS),
        Marking.CHARSET: UNMARKED_ENCODINGS,
        Marking.NOTHING: ["UTF-8"],
    }[marking]
    encoding = draw(st.sampled_from(named))
    texts = marked_texts(letters_of(encoding))
    said = SaidFeed(
        title=draw(texts),
        site=None,
        base=None,
        postings=[
            plain_posting(number, draw(texts), f"https://feeds.test/p/{number}")
            for number in range(draw(st.integers(0, 3)))
        ],
    )
    body = {FeedForm.RSS: rss_body, FeedForm.ATOM: atom_body}.get(form)
    text = json_feed(said, form) if body is None else body(said)
    kind = draw(declared_kinds(form).filter(bool)).split(";")[0]
    other = draw(st.sampled_from(OTHER_CHARSETS))
    declared = f"{kind}; charset={other}" if other else kind
    match marking:
        case Marking.DECLARATION:
            declaration = XML_DECLARATION.format(encoding=encoding)
            content = (declaration + text).encode(ENCODINGS[encoding][0])
        case Marking.BYTE_ORDER_MARK:
            content = BYTE_ORDER_MARKS[encoding] + text.encode(encoding)
        case Marking.CHARSET:
            content = text.encode(ENCODINGS[encoding][0])
            named_as = draw(st.sampled_from((encoding, encoding.lower())))
            declared = f"{kind}; charset={named_as}"
        case _:
            content = text.encode()
            declared = draw(st.sampled_from((kind, "")))
    return EncodedFeed(
        said=said, form=form, encoding=encoding, marking=marking, content=content, declared=declared
    )


def assert_titles_read_in_their_encoding(encoded: EncodedFeed) -> None:
    feed = read_feed(encoded.content, encoded.declared, ADDRESS)
    said = [encoded.said.title, *(posting.title for posting in encoded.said.postings)]
    plain = [title.plain for title in said if title is not None]
    read_titles = [feed.title, *(posting.title for posting in feed.postings)]
    assert len(read_titles) == len(plain) and all(
        is_cut_from(title, text, TITLE_CHARACTERS)
        for title, text in zip(read_titles, plain, strict=True)
    ), (
        f"a {encoded.form} feed in {encoded.encoding}, as {encoded.marking.value} gives it, "
        f"declared {encoded.declared!r}, was read with the titles {read_titles}, not {plain}"
    )


@given(encoded=encoded_feeds((Marking.DECLARATION, Marking.BYTE_ORDER_MARK)))
def test_a_feed_is_read_in_the_encoding_its_declaration_or_byte_order_mark_gives(
    encoded: EncodedFeed,
) -> None:
    assert_titles_read_in_their_encoding(encoded)


@given(encoded=encoded_feeds((Marking.CHARSET, Marking.NOTHING)))
def test_a_feed_that_names_no_encoding_is_read_in_the_charset_declared_else_utf_8(
    encoded: EncodedFeed,
) -> None:
    assert_titles_read_in_their_encoding(encoded)


RSS_1_0 = """<?xml version="1.0" encoding="utf-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
 <channel rdf:about="https://listings.test/night">
  <title>Listings of a made-up archive</title>
  <link>https://listings.test/night</link>
  <description>New papers on night trains.</description>
  <items><rdf:Seq><rdf:li rdf:resource="https://listings.test/abs/1"/>
   <rdf:li rdf:resource="https://listings.test/abs/2"/></rdf:Seq></items>
 </channel>
 <item rdf:about="https://listings.test/abs/1">
  <title>On the timetables of night trains</title>
  <link>https://listings.test/abs/1</link>
  <description>A study of how late the trains ran.</description>
  <dc:date>2026-11-20T08:00:00+09:00</dc:date>
 </item>
 <item rdf:about="https://listings.test/abs/2">
  <title>Sleepers &amp; their berths</title>
  <link>https://listings.test/abs/2</link>
  <description>Where travellers slept.</description>
 </item>
</rdf:RDF>"""
RSS_0_90 = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns="http://my.netscape.com/rdf/simple/0.9/">
 <channel><title>An old channel</title><link>https://old.test/</link>
  <description>Made up for the tests.</description></channel>
 <item><title>The first of old</title><link>https://old.test/1</link></item>
 <item><title>The second of old</title><link>https://old.test/2</link></item>
</rdf:RDF>"""
RSS_0_91 = """<?xml version="1.0" encoding="ISO-8859-1"?>
<rss version="0.91"><channel><title>A 0.91 channel</title><link>https://old.test/</link>
 <description>Made up for the tests.</description><language>en-us</language>
 <item><title>The first of old</title><link>https://old.test/1</link>
  <description>The first.</description></item>
 <item><title>The second of old</title><link>https://old.test/2</link></item>
</channel></rss>"""
RSS_0_92 = """<?xml version="1.0"?>
<rss version="0.92"><channel><title>A 0.92 show</title><link>https://show.test/</link>
 <description>Made up for the tests.</description>
 <item><description>An episode about night trains, told by the guard who rode them.</description>
  <enclosure url="https://show.test/1.mp3" length="1024" type="audio/mpeg"/></item>
 <item><title>The second episode</title><link>https://show.test/2</link></item>
</channel></rss>"""


def posting_at(address: str, title: str, published: datetime | None = None) -> Posting:
    return Posting(key=address, title=title, address=address, published=published)


OLD_FORMS = {
    "RSS 1.0": (
        RSS_1_0,
        Feed(
            title="Listings of a made-up archive",
            site="https://listings.test/night",
            postings=[
                posting_at(
                    "https://listings.test/abs/1",
                    "On the timetables of night trains",
                    datetime(2026, 11, 20, 8, tzinfo=timezone(timedelta(hours=9))),
                ),
                posting_at("https://listings.test/abs/2", "Sleepers & their berths"),
            ],
        ),
    ),
    "RSS 0.90": (
        RSS_0_90,
        Feed(
            title="An old channel",
            site="https://old.test/",
            postings=[
                posting_at("https://old.test/1", "The first of old"),
                posting_at("https://old.test/2", "The second of old"),
            ],
        ),
    ),
    "RSS 0.91": (
        RSS_0_91,
        Feed(
            title="A 0.91 channel",
            site="https://old.test/",
            postings=[
                posting_at("https://old.test/1", "The first of old"),
                posting_at("https://old.test/2", "The second of old"),
            ],
        ),
    ),
    "RSS 0.92": (
        RSS_0_92,
        Feed(
            title="A 0.92 show",
            site="https://show.test/",
            postings=[
                posting_at(
                    "https://show.test/1.mp3",
                    "An episode about night trains, told by the guard who rode them.",
                ),
                posting_at("https://show.test/2", "The second episode"),
            ],
        ),
    ),
}


@pytest.mark.parametrize("form", sorted(OLD_FORMS))
def test_rss_1_0_and_0_9x_are_read(form: str) -> None:
    written, expected = OLD_FORMS[form]
    feed = read_feed(written.encode("latin-1"), "application/xml", ADDRESS)
    assert feed == expected, f"a feed in {form} was read as {feed}, not {expected}"
    assert all(
        same_time(posting.published, said.published)
        for posting, said in zip(feed.postings, expected.postings, strict=True)
    ), f"a feed in {form} was read with the times {[p.published for p in feed.postings]}"


# F7: what is not a feed.

PAGE_OPENINGS = (
    "<!doctype html>",
    "<!DOCTYPE html>\n",
    "",
    "\n  <!-- made up for the tests -->\n<!doctype html>",
    '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 '
    'Strict//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">\n',
)
FEED_NAMED = '<link rel="alternate" type="application/rss+xml" href="/feed.xml">'
PAGE_KINDS = ("text/html; charset=utf-8", "", "application/xhtml+xml")


@st.composite
def web_pages(draw: st.DrawFn) -> bytes:
    # A page with a title and paragraphs, which may name its feed in its head.
    paragraphs = "".join(
        f"<p>{escape(' '.join(draw(st.lists(words, min_size=1, max_size=12))))}</p>"
        for _ in range(draw(st.integers(0, 4)))
    )
    head = f"<title>{escape(draw(words))}</title>{draw(st.sampled_from(('', FEED_NAMED)))}"
    opening = draw(st.sampled_from(PAGE_OPENINGS))
    return f"{opening}<html><head>{head}</head><body>{paragraphs}</body></html>".encode()


@given(page=web_pages(), declared=st.sampled_from(PAGE_KINDS), address=feed_addresses)
def test_a_web_page_is_not_a_feed(page: bytes, declared: str, address: str) -> None:
    told = refusal(page, declared, address, f"a web page at {address}")
    assert address in told, f"a web page was refused as {told!r}, which does not name {address}"


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner),
    max_leaves=12,
)
NOT_JSON_FEED_NAMES = ("version", "items")


@given(
    value=json_values.filter(
        lambda value: not (isinstance(value, dict) and set(NOT_JSON_FEED_NAMES) & value.keys())
    ),
    declared=st.sampled_from(("application/json", "")),
    address=feed_addresses,
)
def test_json_that_is_no_json_feed_is_not_a_feed(value: Any, declared: str, address: str) -> None:
    told = refusal(json.dumps(value).encode(), declared, address, f"the JSON {value!r}")
    assert address in told, f"JSON that is no feed was refused as {told!r}, not naming {address}"


FILE_OPENINGS = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
    b"PK\x03\x04\x14\x00",
    b"ID3\x04\x00\x00",
    gzip.compress(b'<rss version="2.0"><channel><title>Packed</title></channel></rss>'),
)
MARKED_OPENINGS = (b"<", b"{", b"[", codecs.BOM_UTF8, codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
other_bytes = st.one_of(
    st.binary(min_size=1),
    st.text(min_size=1).map(str.encode),
    st.tuples(st.sampled_from(FILE_OPENINGS), st.binary()).map(b"".join),
).filter(lambda content: not content.lstrip().startswith(MARKED_OPENINGS))


@given(content=other_bytes, declared=st.sampled_from(("", "image/png", "text/plain")))
def test_bytes_of_any_other_kind_are_not_a_feed(content: bytes, declared: str) -> None:
    told = refusal(content, declared, ADDRESS, f"the bytes {content[:40]!r}")
    assert ADDRESS in told, f"bytes that are no feed were refused as {told!r}, not naming {ADDRESS}"


@pytest.mark.parametrize("content", [b"", b" \n\t ", codecs.BOM_UTF8])
def test_nothing_at_all_is_not_a_feed(content: bytes) -> None:
    told = refusal(content, "application/rss+xml", ADDRESS, f"the answer {content!r}")
    assert ADDRESS in told, f"an empty answer was refused as {told!r}, not naming {ADDRESS}"


NOT_FEEDS = {
    "a web page": (
        b"<!doctype html><html><head><title>Night trains</title></head><body>"
        b"<p>The night train leaves at nine.</p></body></html>",
        "text/html; charset=utf-8",
    ),
    "JSON that is no JSON Feed": (
        b'{"id": 12, "title": {"rendered": "Night trains"}, "link": "https://blog.test/n"}',
        "application/json",
    ),
    "a picture": (FILE_OPENINGS[0] + bytes(64), "image/png"),
    "nothing": (b"", ""),
}
# A word that a sentence about each of these must hold to say what was found.
FOUND_WORDS = {"a web page": ("page", "html"), "JSON that is no JSON Feed": ("json",)}


def test_what_is_not_a_feed_is_told_by_what_was_found_instead() -> None:
    told = {
        found: refusal(content, declared, ADDRESS, found).replace(ADDRESS, "")
        for found, (content, declared) in NOT_FEEDS.items()
    }
    assert len(set(told.values())) == len(told), (
        f"what is not a feed was told alike whatever was found instead: {told}"
    )
    for found, named in FOUND_WORDS.items():
        assert any(word in told[found].casefold() for word in named), (
            f"{found} was refused as {told[found]!r}, which does not say what was found"
        )


@given(served=served_feeds(said_feeds(st.just([]))))
def test_a_feed_with_no_postings_is_a_feed(served: ServedFeed) -> None:
    feed = read(served)
    assert feed.postings == [], f"a {served.form} feed without postings was read with {feed}"


class Breakage(StrEnum):
    CUT = "cut off after a posting"
    AMPERSAND = "a bare ampersand"
    ENTITY = "an entity XML does not define"


BROKEN_ELEMENT = {
    Breakage.AMPERSAND: "<remark>Fish & Chips</remark>",
    Breakage.ENTITY: "<remark>Fish&nbsp;Chips</remark>",
}
POSTING_ELEMENTS = {FeedForm.RSS: "item", FeedForm.ATOM: "entry"}


@given(
    served=served_feeds(
        addressed_feeds().filter(lambda feed: bool(feed.postings)), st.sampled_from(XML_FORMS)
    ),
    breakage=st.sampled_from(Breakage),
    data=st.data(),
)
def test_a_feed_that_is_not_well_formed_is_read_for_the_postings_it_holds(
    served: ServedFeed, breakage: Breakage, data: st.DataObject
) -> None:
    text = served.content.decode()
    name = POSTING_ELEMENTS[served.form]
    expected = [
        posting.key for posting in expected_postings(served.said, served.form, served.address)
    ]
    if breakage is Breakage.CUT:
        whole = data.draw(st.integers(1, len(expected)))
        ends = [index for index in range(len(text)) if text.startswith(f"</{name}>", index)]
        broken = text[: ends[whole - 1] + len(f"</{name}>")]
        expected = expected[:whole]
    else:
        opened = text.index(">", text.index(f"<{name}")) + 1
        broken = text[:opened] + BROKEN_ELEMENT[breakage] + text[opened:]
    keys = [
        posting.key
        for posting in read_feed(broken.encode(), served.declared, served.address).postings
    ]
    assert keys[: len(expected)] == expected, (
        f"a {served.form} feed with {breakage.value} was read with the postings {keys}, not "
        f"those it holds whole, {expected}"
    )


# Words of a title that put, at an offset of the bytes, what another kind of file holds there:
# the word, where in it the signature begins, the signature and its offset.
SIGNATURES = {
    "a tar archive's": ("Mustard", 1, b"ustar", 257),
    "a DICOM file's": ("PERIODICMAN", 5, b"DICM", 128),
}
DECLARED_UTF_8 = XML_DECLARATION.format(encoding="utf-8")
FEED_HEADS = {
    FeedForm.RSS: f'{DECLARED_UTF_8}<rss version="2.0"><channel><title>',
    FeedForm.ATOM: f'{DECLARED_UTF_8}<feed xmlns="{ATOM_NAMESPACE}"><title>',
}
FEED_TAILS = {
    FeedForm.RSS: "</title><item><title>A posting</title><link>https://feeds.test/p/1</link>"
    "</item></channel></rss>",
    FeedForm.ATOM: "</title><entry><id>tag:feeds.test,2026:1</id><title>A posting</title>"
    '<link href="https://feeds.test/p/1"/></entry></feed>',
}
GUESSED_KIND = pytest.mark.xfail(
    strict=True,
    reason="F7 as amended, not yet built: a feed whose bytes hold another kind of file's "
    "signature where that kind holds it is refused as a file of that kind",
)
SIGNED_FEEDS = [
    pytest.param("a tar archive's", FeedForm.RSS, marks=GUESSED_KIND),
    pytest.param("a tar archive's", FeedForm.ATOM, marks=GUESSED_KIND),
    pytest.param("a DICOM file's", FeedForm.RSS, marks=GUESSED_KIND),
    pytest.param("a DICOM file's", FeedForm.ATOM, marks=GUESSED_KIND),
]


@pytest.mark.parametrize(("kind", "form"), SIGNED_FEEDS)
def test_a_feed_whose_bytes_look_like_another_kind_of_file_is_read(
    kind: str, form: FeedForm
) -> None:
    word, within, signature, offset = SIGNATURES[kind]
    title = "a" * (offset - len(FEED_HEADS[form]) - within) + f"{word} pie"
    content = (FEED_HEADS[form] + title + FEED_TAILS[form]).encode()
    assert content[offset : offset + len(signature)] == signature, "the sample is misplaced"
    feed = read_feed(content, "", ADDRESS)
    assert (feed.title, [posting.address for posting in feed.postings]) == (
        title,
        ["https://feeds.test/p/1"],
    ), f"a {form} feed whose bytes hold {kind} signature was read as {feed}, not as the feed"


# Fragments that break a feed in the ways a parser meets: markup cut off, references out of
# range, encodings marked twice, escapes of JSON that name no character.
FRAGMENTS = (
    b"<",
    b">",
    b"&",
    b"&#",
    b"&#x110000;",
    b"&#0;",
    b"&#xD800;",
    b"<![CDATA[",
    b"]]>",
    b"<!--",
    b"-->",
    b"<!ENTITY a 'b'>",
    b"<!DOCTYPE x [",
    b"\x00",
    b"\xff\xfe",
    codecs.BOM_UTF8,
    b'"',
    b"{",
    b"}",
    b"[",
    b"\\ud800",
    b"\\u",
    b"<item>",
    b"<entry>",
    b"</channel>",
    b" xml:base='::'",
    b"<?xml",
    b"?>",
)
changes = st.lists(
    st.tuples(
        st.sampled_from(("delete", "insert", "replace")),
        st.integers(0, 2**16),
        st.integers(1, 64),
        st.one_of(st.sampled_from(FRAGMENTS), st.binary(min_size=1, max_size=8)),
    ),
    min_size=1,
    max_size=6,
)


def changed(content: bytes, made: list[tuple[str, int, int, bytes]]) -> bytes:
    for kind, at, span, piece in made:
        where = at % (len(content) + 1)
        match kind:
            case "delete":
                content = content[:where] + content[where + span :]
            case "insert":
                content = content[:where] + piece + content[where:]
            case _:
                content = content[:where] + piece + content[where + len(piece) :]
    return content


@given(served=served_feeds(), made=changes)
def test_any_bytes_end_in_a_feed_or_unreadable_feed(
    served: ServedFeed, made: list[tuple[str, int, int, bytes]]
) -> None:
    read_or_refused(changed(served.content, made), served.declared, served.address)


# F8: bounds.

MOST_POSTINGS = 2000
# The most postings a reading need look at: twice those it keeps, as F8 bounds the items of a
# JSON Feed that are read.
MOST_POSTINGS_READ = 2 * MOST_POSTINGS


@pytest.mark.parametrize("form", [FeedForm.RSS, FeedForm.ATOM, FeedForm.JSON_1_1])
@pytest.mark.parametrize("unaddressed", [0, 300, MOST_POSTINGS + 100])
def test_of_a_feed_of_more_than_2000_postings_the_first_2000_with_an_address_are_kept(
    form: FeedForm, unaddressed: int
) -> None:
    # Of postings beyond the first 4,000 none need be kept: where more than 2,000 without an
    # address come first, those with one among the first 4,000 are enough.
    title = unmarked_text("A posting")
    postings = [plain_posting(number, title, None) for number in range(unaddressed)] + [
        plain_posting(number, title, f"https://feeds.test/p/{number}")
        for number in range(unaddressed, unaddressed + MOST_POSTINGS + 100)
    ]
    said = SaidFeed(title=title, site=None, base=None, postings=postings)
    keys = [posting.key for posting in read_feed(written_feed(said, form), "", ADDRESS).postings]
    expected = [
        f"tag:feeds.test,2026:{number}"
        for number in range(unaddressed, unaddressed + MOST_POSTINGS)
    ]
    fewest = min(MOST_POSTINGS, MOST_POSTINGS_READ - unaddressed)
    assert keys == expected[: len(keys)] and len(keys) >= fewest, (
        f"of a {form} feed of {unaddressed} postings without an address followed by "
        f"{MOST_POSTINGS + 100} with one, {len(keys)} were kept, from {keys[:1]} to {keys[-1:]}, "
        f"not the first {MOST_POSTINGS} with an address, or at least the {fewest} with one among "
        f"the first {MOST_POSTINGS_READ}"
    )


def traced_peak(reading: Callable[[], object]) -> int:
    # The most memory that Python's objects held at once while the reading ran, beyond what
    # they held before it: a count of what the reading made, which the clock does not sway.
    tracemalloc.start()
    try:
        reading()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def calls_made(content: bytes) -> int:
    # The calls of functions, Python's and its built-ins', that reading the bytes makes.
    calls = [0]

    def heard(frame: FrameType, event: str, argument: object) -> None:
        if event in CALL_EVENTS:
            calls[0] += 1

    sys.setprofile(heard)
    try:
        read_feed(content, "", ADDRESS)
    finally:
        sys.setprofile(None)
    return calls[0]


CALL_EVENTS = frozenset({"call", "c_call"})
# Reading what is sent beyond a bound may hold more than parsing its JSON holds more by half at
# most, and make a few more calls: holding a copy of its text, say. Reading each item or
# attachment as a model holds three times as much, as a stand-in of pydantic models measured.
MORE_HELD_THAN_PARSED = 1.5
MORE_CALLS = 1000


def assert_read_in_proportion_to_what_is_kept(sent: bytes, bounded: bytes, what: str) -> None:
    more_calls = calls_made(sent) - calls_made(bounded)
    assert more_calls <= MORE_CALLS, (
        f"reading {what} made {more_calls} calls more than reading the bounded feed: what is "
        "sent beyond the bound is read"
    )
    read_more = traced_peak(lambda: read_feed(sent, "", ADDRESS)) - traced_peak(
        lambda: read_feed(bounded, "", ADDRESS)
    )
    parsed_more = traced_peak(lambda: json.loads(sent)) - traced_peak(lambda: json.loads(bounded))
    assert read_more <= MORE_HELD_THAN_PARSED * parsed_more, (
        f"reading {what} held {read_more / 1024**2:.2f} MiB more than reading the bounded feed, "
        f"where parsing its JSON holds {parsed_more / 1024**2:.2f} MiB more: what is sent beyond "
        "the bound is read"
    )


def json_feed_of(items: list[dict[str, Any]]) -> bytes:
    return json.dumps({"version": JSON_FEED_VERSIONS[FeedForm.JSON_1_1], "items": items}).encode()


def items_beyond(extra: int) -> bytes:
    # A feed of 2,000 items with an address, and after them small items without one.
    items: list[dict[str, Any]] = [
        {"id": str(number), "url": f"https://feeds.test/p/{number}"}
        for number in range(MOST_POSTINGS)
    ]
    items += [{"id": str(number)} for number in range(MOST_POSTINGS, MOST_POSTINGS + extra)]
    return json_feed_of(items)


SENT_ITEMS = 10_000
FAULT_OF_BOUNDS = (
    "F8 as amended, not yet built: every item of a JSON Feed, and every attachment of an item, "
    "is read, whatever the bound"
)


@pytest.mark.xfail(strict=True, reason=FAULT_OF_BOUNDS)
def test_of_a_json_feed_no_more_items_are_read_than_twice_those_kept() -> None:
    sent = items_beyond(SENT_ITEMS - MOST_POSTINGS)
    bounded = items_beyond(MOST_POSTINGS_READ - MOST_POSTINGS)
    kept = len(read_feed(sent, "", ADDRESS).postings)
    assert kept == MOST_POSTINGS, f"of {SENT_ITEMS} items of a JSON Feed {kept} were kept"
    assert_read_in_proportion_to_what_is_kept(sent, bounded, f"a JSON Feed of {SENT_ITEMS} items")


def attached(count: int) -> bytes:
    # A feed of one item with a recording, and after it small attachments.
    attachments: list[dict[str, Any]] = [
        {"url": "https://feeds.test/a/0.mp3", "mime_type": "audio/mpeg"}
    ]
    attachments += [{"url": "a"} for _ in range(count - 1)]
    return json_feed_of([{"id": "1", "url": "https://feeds.test/p/1", "attachments": attachments}])


SENT_ATTACHMENTS = 20_000
BOUNDED_ATTACHMENTS = 1_000


@pytest.mark.xfail(strict=True, reason=FAULT_OF_BOUNDS)
def test_of_one_json_feed_item_the_attachments_read_are_bounded() -> None:
    sent, bounded = attached(SENT_ATTACHMENTS), attached(BOUNDED_ATTACHMENTS)
    addresses = [posting.address for posting in read_feed(sent, "", ADDRESS).postings]
    assert addresses == ["https://feeds.test/a/0.mp3"], (
        f"an item of {SENT_ATTACHMENTS} attachments, the first of sound, was read at {addresses}"
    )
    assert_read_in_proportion_to_what_is_kept(
        sent, bounded, f"a JSON Feed item of {SENT_ATTACHMENTS} attachments"
    )


# F9 and F10: a feed is another site's text, and reading it touches nothing else.

# Events of Python's audit that tell a file opened, a name looked up, a connection made, a
# request sent or a program started. A module imported opens its file too, and is no file a
# feed made the reading open.
WATCHED_EVENTS = frozenset(
    {
        "open",
        "socket.connect",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "urllib.Request",
        "http.client.connect",
        "subprocess.Popen",
        "os.system",
        "os.posix_spawn",
        "os.exec",
        "os.fork",
    }
)
IMPORTED_SUFFIXES = (".py", ".pyc", ".so", ".pth")


class Touches:
    # What the process touches beyond itself while a call is watched, as its audit events tell.
    def __init__(self) -> None:
        self.watching = False
        self.touched: list[str] = []

    def hear(self, event: str, arguments: tuple[Any, ...]) -> None:
        imported = event == "open" and str(arguments[0]).endswith(IMPORTED_SUFFIXES)
        if self.watching and event in WATCHED_EVENTS and not imported:
            self.touched.append(f"{event} {arguments!r}"[:300])

    @contextmanager
    def watched(self) -> Iterator[list[str]]:
        self.touched = []
        self.watching = True
        try:
            yield self.touched
        finally:
            self.watching = False


@cache
def touches() -> Touches:
    # An audit hook stays for the life of the process; one is added, and listens only while a
    # call is watched.
    hearing = Touches()
    sys.addaudithook(hearing.hear)
    return hearing


@given(served=served_feeds())
def test_reading_a_feed_reads_its_arguments_and_nothing_else(served: ServedFeed) -> None:
    with touches().watched() as touched:
        feed = read(served)
    assert not touched, f"reading a {served.form} feed touched what is not its arguments: {touched}"
    assert read(served) == feed, f"a {served.form} feed read twice was read as two feeds"


# Words that only a file of this machine holds, so that finding them in a feed tells that the
# file was read into it.
SECRET = "quorvex zindlap mertusk"
REFERRING_ITEM = (
    "<item><title>&secret;</title><link>https://feeds.test/p/1</link>"
    "<description>&secret;</description></item>"
)
XINCLUDE = ' xmlns:xi="http://www.w3.org/2001/XInclude"'


def rss_with(doctype: str, root: str = "", item: str = REFERRING_ITEM) -> str:
    return (
        f'<?xml version="1.0"?>{doctype}<rss version="2.0"{root}><channel><title>&secret;</title>'
        f"{item}</channel></rss>"
    )


def included(address: str) -> str:
    return (
        "<item><title>Included</title><link>https://feeds.test/p/1</link><description>"
        f'<xi:include href="{address}" parse="text"/></description></item>'
    )


# Feeds that refer, by an entity, a DTD or an inclusion, to a file of this machine: the file
# that holds the secret, or one that declares it as an entity.
LOCAL_SAMPLES = {
    "a general entity naming a file by its URL": rss_with(
        '<!DOCTYPE rss [<!ENTITY secret SYSTEM "{secret_url}">]>'
    ),
    "a general entity naming a file by its path": rss_with(
        '<!DOCTYPE rss [<!ENTITY secret SYSTEM "{secret_path}">]>'
    ),
    "a parameter entity naming a file of declarations": rss_with(
        '<!DOCTYPE rss [<!ENTITY % outside SYSTEM "{declarations_url}"> %outside;]>'
    ),
    "an external DTD": rss_with('<!DOCTYPE rss SYSTEM "{declarations_url}">'),
    "an XInclude": rss_with("", XINCLUDE, included("{secret_url}")),
}
# Feeds that refer to what a server elsewhere holds.
REMOTE_SAMPLES = {
    "an external DTD on the web": rss_with('<!DOCTYPE rss SYSTEM "http://dtd.test/rss.dtd">'),
    "an entity on the web": rss_with(
        '<!DOCTYPE rss [<!ENTITY secret SYSTEM "http://entities.test/secret.txt">]>'
    ),
    "a parameter entity on the web": rss_with(
        '<!DOCTYPE rss [<!ENTITY % outside SYSTEM "http://entities.test/outside.dtd"> %outside;]>'
    ),
    "a stylesheet on the web": rss_with(
        '<?xml-stylesheet type="text/xsl" href="http://styles.test/feed.xsl"?>'
        '<!DOCTYPE rss [<!ENTITY secret "kept">]>'
    ),
    "an XInclude on the web": rss_with("", XINCLUDE, included("http://include.test/part.xml")),
}


@pytest.mark.parametrize("sample", sorted(LOCAL_SAMPLES))
def test_a_feed_that_refers_to_a_file_of_this_machine_reads_no_file(
    sample: str, tmp_path: Path
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text(SECRET)
    declarations = tmp_path / "outside.dtd"
    declarations.write_text(f'<!ENTITY secret "{SECRET}">')
    content = LOCAL_SAMPLES[sample].format(
        secret_url=secret.as_uri(), secret_path=secret, declarations_url=declarations.as_uri()
    )
    with touches().watched() as touched:
        read_feeds = read_or_refused(content.encode(), RSS_KIND, ADDRESS)
    assert not touched, f"reading a feed with {sample} touched {touched}"
    told = [feed.model_dump_json() for feed in read_feeds]
    assert not any(word in text for text in told for word in SECRET.split()), (
        f"a feed with {sample} was read with the file's words in it: {told}"
    )


def no_fetch(method: str, url: str, **options: Any) -> Any:
    pytest.fail(f"reading a feed fetched {url}")


@pytest.mark.parametrize("sample", sorted(REMOTE_SAMPLES))
def test_a_feed_that_refers_to_what_a_server_holds_fetches_nothing(
    sample: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(httpx, "stream", no_fetch)
    with touches().watched() as touched:
        read_or_refused(REMOTE_SAMPLES[sample].encode(), RSS_KIND, ADDRESS)
    assert not touched, f"reading a feed with {sample} touched {touched}"


def bomb_feed(declarations: str, reference: str, form: FeedForm) -> str:
    # A feed whose titles are an entity that its declarations make vast.
    if form is FeedForm.RSS:
        return (
            f'<?xml version="1.0"?><!DOCTYPE rss [{declarations}]><rss version="2.0"><channel>'
            f"<title>{reference}</title><item><title>{reference}</title>"
            "<link>https://feeds.test/p/1</link></item></channel></rss>"
        )
    return (
        f'<?xml version="1.0"?><!DOCTYPE feed [{declarations}]><feed xmlns="{ATOM_NAMESPACE}">'
        f"<title>{reference}</title><entry><id>tag:feeds.test,2026:1</id>"
        f'<title>{reference}</title><link href="https://feeds.test/p/1"/></entry></feed>'
    )


BOMBS = {
    # Ten entities each of ten of the one before: a thousand million laughs.
    "the billion laughs": (
        '<!ENTITY lol0 "lol">'
        + "".join(f'<!ENTITY lol{level} "{f"&lol{level - 1};" * 10}">' for level in range(1, 10)),
        "&lol9;",
    ),
    # One entity of fifty thousand letters said fifty thousand times: 2.5 GB.
    "a quadratic blow-up": (f'<!ENTITY a "{"a" * 50_000}">', "&a;" * 50_000),
}
# What reading a feed may take, whatever it holds, as the requirements bound the reading a poll
# and a discovery make. The process that reads it may hold no more than CHILD_MEMORY_BYTES, so
# that a reading that runs away ends there; each sample is waited for a time of its own.
BOMB_SECONDS = 30.0
BOMB_BYTES = 512 * 1024**2
CHILD_MEMORY_BYTES = 4 * 1024**3
BOMB_WAIT_SECONDS = 120.0
REPOSITORY = Path(__file__).resolve().parents[1]
REFUSED = f"{UnreadableFeed.__module__}.{UnreadableFeed.__qualname__}"


class BoundedReading(BaseModel):
    # How a reading in a process of its own ended: its status, the last line it wrote of an
    # error, and the last line it wrote of what it measured.
    status: int
    ending: str
    measured: str


def bounded_reading(content: bytes, folder: Path, wait_seconds: float, what: str) -> BoundedReading:
    sample = folder / "sample.xml"
    sample.write_bytes(content)
    command = [
        sys.executable,
        "-m",
        "tests.feed_reading_bound",
        str(sample),
        ADDRESS,
        str(CHILD_MEMORY_BYTES),
    ]
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=REPOSITORY
    ) as child:
        try:
            said, failed = child.communicate(timeout=wait_seconds)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            pytest.fail(f"reading {what} went on for more than {wait_seconds:.0f} seconds")
    return BoundedReading(
        status=child.returncode,
        ending=(failed.strip().splitlines() or [""])[-1],
        measured=(said.strip().splitlines() or ["{}"])[-1],
    )


def assert_ended_within(reading: BoundedReading, what: str, seconds: float, most: int) -> None:
    assert reading.status == 0 or reading.ending.startswith(REFUSED), (
        f"reading {what} ended with the status {reading.status} and {reading.ending!r}, not in a "
        "feed or in UnreadableFeed"
    )
    measured = json.loads(reading.measured)
    assert measured[SECONDS] <= seconds, f"reading {what} took {measured[SECONDS]:.1f} seconds"
    assert measured[PEAK_BYTES] <= most, (
        f"reading {what} held {measured[PEAK_BYTES] / 1024**2:.0f} MiB at most"
    )


@pytest.mark.parametrize("form", XML_FORMS)
@pytest.mark.parametrize("bomb", sorted(BOMBS))
def test_an_entity_bomb_ends_in_a_feed_or_unreadable_feed_within_bounds(
    bomb: str, form: FeedForm, tmp_path: Path
) -> None:
    what = f"{bomb} in {form}"
    reading = bounded_reading(
        bomb_feed(*BOMBS[bomb], form).encode(), tmp_path, BOMB_WAIT_SECONDS, what
    )
    assert_ended_within(reading, what, BOMB_SECONDS, BOMB_BYTES)


ORDINARY_FEED = (
    '<rss version="2.0"><channel><title>Behind comments</title><item><title>A posting</title>'
    "<link>https://feeds.test/p/1</link></item></channel></rss>"
)
# Inputs on which a pattern that backtracks takes time that grows faster than they do: at
# these sizes time in proportion to them is far under a second, and the growth seen before
# (doubling with each comment; 22 seconds for 1 MiB of declarations) far over the bound.
GREEDY_SAMPLES = {
    "an ordinary feed behind 40 comments": ("<!--a-->" * 40 + ORDINARY_FEED).encode(),
    "2 MiB of declarations of entities that never close": b"<!ENTITY" * (2 * 1024**2 // 8),
}
# The bound on the reading, and on the wait for the process, which starts in a second or two.
LINEAR_SECONDS = 2.0
LINEAR_WAIT_SECONDS = 6.0


@pytest.mark.xfail(
    strict=True,
    reason="F9 as amended, not yet built: comments before a feed take time that doubles with "
    "each, and declarations of entities that never close take time that grows with their square",
)
@pytest.mark.parametrize("sample", sorted(GREEDY_SAMPLES))
def test_reading_takes_time_in_proportion_to_what_is_read(sample: str, tmp_path: Path) -> None:
    reading = bounded_reading(GREEDY_SAMPLES[sample], tmp_path, LINEAR_WAIT_SECONDS, sample)
    assert_ended_within(reading, sample, LINEAR_SECONDS, BOMB_BYTES)
