import html
import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import partial
from xml.sax.saxutils import escape

import pytest
from hypothesis import given
from hypothesis import strategies as st

from infonium.subscriptions.feed import (
    ADDRESS_CHARACTERS,
    KEY_CHARACTERS,
    TITLE_CHARACTERS,
    Posting,
    read_feed,
)
from tests.feed_writing import (
    ADDRESS,
    ATOM_NAMESPACE,
    BASES,
    CLIMBING_BASES,
    DAY_NAMES,
    JSON_FEED_VERSIONS,
    MONTH_NAMES,
    POSTING_ELEMENTS,
    RECORDING_TYPES,
    RSS_KIND,
    SUMMARY_TITLE_CHARACTERS,
    WORD_GAP,
    XML_FORMS,
    Enclosure,
    FeedForm,
    Marked,
    SaidFeed,
    ServedFeed,
    TextKind,
    WrittenTime,
    absolute_addresses,
    absolute_addresses_of,
    addressed_feeds,
    addressed_postings,
    bases,
    expected_feed_title,
    expected_postings,
    expected_site,
    feed_addresses,
    forms,
    is_cut_from,
    keyed_feeds,
    marked_texts,
    markup_of,
    moments,
    numeric_zone,
    offsets,
    padded,
    plain_posting,
    read,
    read_text,
    refusal,
    relative_addresses,
    relative_addresses_of,
    said_feeds,
    said_plainly,
    said_postings,
    same_time,
    served_feeds,
    slugs,
    spaced_slugs,
    unreadable_times,
    words,
    written_feed,
    written_text,
    youtube_feed,
)

# Judgments of read_feed: a posting's address, key, title and time, and the feed itself
# (F2 to F6, G12 and G15). A reading raises no warning of any kind (G16).
pytestmark = pytest.mark.filterwarnings("error")


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


ENCLOSURE_FORMS = [FeedForm.RSS, FeedForm.ATOM]


@pytest.mark.parametrize("form", ENCLOSURE_FORMS)
@given(feed_base=st.sampled_from(BASES), written=relative_addresses, address=feed_addresses)
def test_an_enclosures_relative_address_is_resolved_against_the_base_the_feed_gives(
    form: FeedForm, feed_base: str, written: str, address: str
) -> None:
    enclosure = Enclosure(address=written, kind="audio/mpeg")
    posting = plain_posting(0, said_plainly("An episode"), None).model_copy(
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
        plain_posting(number, said_plainly("A posting"), draw(relative_addresses)).model_copy(
            update={"base": draw(bases)}
        )
        for number in range(draw(st.integers(1, 4)))
    ]
    return SaidFeed(title=None, site=None, base=draw(bases), postings=postings)


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
        plain_posting(number, said_plainly("A posting"), page) for number, page in enumerate(pages)
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
                "title": said_plainly("A copy"),
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


KEY_FORMS = [FeedForm.ATOM, FeedForm.RSS, FeedForm.JSON_1_1]


@pytest.mark.parametrize("form", KEY_FORMS)
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
            number, said_plainly("A posting"), f"https://feeds.test/p/{number}"
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
    said_plainly(""),
    Marked(markup=" <i></i> ", plain=""),
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


def json_item_with(field: str, written: str) -> bytes:
    item = {"id": "1", "url": "https://feeds.test/p/1", field: written}
    return json.dumps({"version": JSON_FEED_VERSIONS[FeedForm.JSON_1_1], "items": [item]}).encode()


def atom_titled(typed: str, written: str) -> bytes:
    entry = (
        f"<entry><id>tag:feeds.test,2026:1</id><title{typed}>{written}</title>"
        '<link href="https://feeds.test/p/1"/></entry>'
    )
    return f'<feed xmlns="{ATOM_NAMESPACE}"><title>A feed</title>{entry}</feed>'.encode()


def rss_item_with(element: str, written: str) -> bytes:
    item = (
        f"<item><{element}>{escape(written)}</{element}><link>https://feeds.test/p/1</link></item>"
    )
    return f'<rss version="2.0"><channel><title>A feed</title>{item}</channel></rss>'.encode()


XML_REFERENCES = frozenset({"amp", "lt", "gt", "quot", "apos"})
NAMED_REFERENCE = re.compile(r"&([A-Za-z]+);")


def as_xhtml(markup: str) -> str:
    # Markup as XHTML writes it: a reference XML does not name written by its number.
    return '<div xmlns="http://www.w3.org/1999/xhtml">{}</div>'.format(
        NAMED_REFERENCE.sub(
            lambda found: (
                found.group()
                if found.group(1) in XML_REFERENCES
                else f"&#{ord(html.unescape(found.group()))};"
            ),
            markup,
        )
    )


# Each field a test writes a text into: how the feed is written, what its format says the field
# is, and the most characters of it a posting is called by.
FIELDS: dict[str, tuple[Callable[[str], bytes], TextKind, int]] = {
    "JSON Feed's title": (partial(json_item_with, "title"), TextKind.PLAIN, TITLE_CHARACTERS),
    "JSON Feed's summary": (
        partial(json_item_with, "summary"),
        TextKind.PLAIN,
        SUMMARY_TITLE_CHARACTERS,
    ),
    "JSON Feed's content_text": (
        partial(json_item_with, "content_text"),
        TextKind.PLAIN,
        SUMMARY_TITLE_CHARACTERS,
    ),
    "JSON Feed's content_html": (
        partial(json_item_with, "content_html"),
        TextKind.MARKUP,
        SUMMARY_TITLE_CHARACTERS,
    ),
    "Atom's title of type text": (
        lambda written: atom_titled(' type="text"', escape(written)),
        TextKind.PLAIN,
        TITLE_CHARACTERS,
    ),
    "Atom's title of no type": (
        lambda written: atom_titled("", escape(written)),
        TextKind.PLAIN,
        TITLE_CHARACTERS,
    ),
    "Atom's title of type html": (
        lambda written: atom_titled(' type="html"', escape(written)),
        TextKind.MARKUP,
        TITLE_CHARACTERS,
    ),
    "Atom's title of type xhtml": (
        lambda written: atom_titled(' type="xhtml"', as_xhtml(written)),
        TextKind.MARKUP,
        TITLE_CHARACTERS,
    ),
    "RSS's title": (partial(rss_item_with, "title"), TextKind.MARKUP, TITLE_CHARACTERS),
    "RSS's description": (
        partial(rss_item_with, "description"),
        TextKind.MARKUP,
        SUMMARY_TITLE_CHARACTERS,
    ),
}


@given(text=marked_texts(), field=st.sampled_from(sorted(FIELDS)), address=feed_addresses)
def test_a_field_is_read_as_what_its_format_says_it_is(
    text: Marked, field: str, address: str
) -> None:
    write, kind, characters = FIELDS[field]
    [posting] = read_feed(write(text.markup), "", address).postings
    said = read_text(text, kind)
    assert is_cut_from(posting.title, said, characters), (
        f"{field}, {kind.value}, written {text.markup!r}, called its posting {posting.title!r}, "
        f"not {said!r} cut to {characters} characters"
    )


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
        postings=[plain_posting(0, said_plainly(plain), "https://feeds.test/p/0")],
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


@given(served=served_feeds(feeds_titled_by_documents()))
def test_a_title_or_summary_that_is_a_whole_html_document_never_fails_the_reading(
    served: ServedFeed,
) -> None:
    feed = read(served)
    assert len(feed.postings) == len(served.said.postings), (
        f"of {len(served.said.postings)} postings of a {served.form} feed titled by HTML "
        f"documents, {len(feed.postings)} were read"
    )


@given(served=served_feeds(feeds_titled_by_documents(), st.just(FeedForm.ATOM)))
def test_an_atom_title_or_summary_that_is_a_whole_html_document_reads_as_its_text_or_nothing(
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


def rdf_feed(title: str, posting_title: str | None, description: str | None) -> bytes:
    # A feed of RSS 1.0 with one posting, its texts written as XML text.
    titled = "" if posting_title is None else f"<title>{escape(posting_title)}</title>"
    described = "" if description is None else f"<description>{escape(description)}</description>"
    return (
        '<?xml version="1.0" encoding="utf-8"?><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/">'
        f'<channel rdf:about="https://feeds.test/"><title>{escape(title)}</title>'
        "<link>https://feeds.test/</link></channel>"
        f'<item rdf:about="https://feeds.test/p/1">{titled}<link>https://feeds.test/p/1</link>'
        f"{described}</item></rdf:RDF>"
    ).encode()


def rss_feed_of(title: str, posting_title: str | None, description: str | None) -> bytes:
    titled = "" if posting_title is None else f"<title>{escape(posting_title)}</title>"
    described = "" if description is None else f"<description>{escape(description)}</description>"
    return (
        f'<rss version="2.0"><channel><title>{escape(title)}</title>'
        f"<item>{titled}<link>https://feeds.test/p/1</link>{described}</item></channel></rss>"
    ).encode()


RSS_WRITERS: dict[str, Callable[[str, str | None, str | None], bytes]] = {
    "RSS 2.0": rss_feed_of,
    "RSS 1.0": rdf_feed,
}
# Texts as the ruling gives them, as an RSS or RDF feed holds them once XML is read, and what is
# read of each.
RULED_TEXTS = {
    "Why Vec<T> is fast": "Why Vec<T> is fast",
    "The <script> element explained": "The <script> element explained",
    "<details> and <summary>": "<details> and <summary>",
    "Option<Box<dyn Error>> in Rust": "Option<Box<dyn Error>> in Rust",
    "<em>Seen</em> and heard": "Seen and heard",
}


@pytest.mark.parametrize("form", sorted(RSS_WRITERS))
@pytest.mark.parametrize("written", sorted(RULED_TEXTS))
def test_an_rss_title_is_markup_only_where_it_is_markup(written: str, form: str) -> None:
    expected = RULED_TEXTS[written]
    feed = read_feed(RSS_WRITERS[form](written, written, None), "", ADDRESS)
    assert [feed.title, feed.postings[0].title] == [expected, expected], (
        f"an {form} feed and posting titled {written!r} were read as {feed.title!r} and "
        f"{feed.postings[0].title!r}, not as {expected!r}"
    )


# Words of text that is no markup: signs of markup and tags that open, and no element that closes
# and no character reference.
UNMARKED_TOKENS = ("<", ">", "&", "<T>", "<script>", "<details>", "Vec<T>", "a<b", "x>y", "Box<dyn")
unmarked_words = st.one_of(
    st.text(st.characters(categories=("L", "N")), min_size=1, max_size=8),
    st.sampled_from(UNMARKED_TOKENS),
)
unmarked_writing = st.lists(unmarked_words, min_size=1, max_size=10).map(WORD_GAP.join)
rss_forms = st.sampled_from(sorted(RSS_WRITERS))


@given(title=unmarked_writing, form=rss_forms)
def test_an_rss_title_with_no_element_that_closes_and_no_reference_is_kept_as_written(
    title: str, form: str
) -> None:
    feed = read_feed(RSS_WRITERS[form](title, title, None), "", ADDRESS)
    assert [feed.title, feed.postings[0].title] == [title, title], (
        f"an {form} feed and posting titled {title!r} were read as {feed.title!r} and "
        f"{feed.postings[0].title!r}"
    )


# Descriptions as RSS writes them, which name a posting without a title, and what each is
# read as: HTML, whatever it looks like (T1).
HTML_DESCRIPTIONS = {
    "one<br>two": "one two",
    '<img src="https://feeds.test/a.png" /> Night trains': "Night trains",
    "<p>Seen <b>and</b> heard</p>": "Seen and heard",
}


@pytest.mark.parametrize("form", sorted(RSS_WRITERS))
@pytest.mark.parametrize("written", sorted(HTML_DESCRIPTIONS))
def test_an_rss_description_is_read_as_html_whatever_it_looks_like(written: str, form: str) -> None:
    [posting] = read_feed(RSS_WRITERS[form]("A feed", None, written), "", ADDRESS).postings
    expected = HTML_DESCRIPTIONS[written]
    assert posting.title == expected, (
        f"an {form} posting without a title, described {written!r}, was called "
        f"{posting.title!r}, not {expected!r}: a description is HTML"
    )


description_words = st.lists(
    st.text(st.characters(categories=("L", "N", "P", "S"), exclude_characters="<&"), min_size=1),
    min_size=1,
    max_size=30,
)
plain_descriptions = description_words.map(WORD_GAP.join)
# What stands between words of a description: a gap, or a line break, which no element closes.
BREAKS = (" ", "<br>", "<br/>", " <br /> ")
PICTURE = '<img src="https://feeds.test/a.png" /> '


@st.composite
def descriptions_broken_into_lines(draw: st.DrawFn) -> tuple[str, list[str]]:
    words = draw(description_words)
    written = words[0] + "".join(draw(st.sampled_from(BREAKS)) + word for word in words[1:])
    return (PICTURE if draw(st.booleans()) else "") + written, words


@given(described=descriptions_broken_into_lines(), form=rss_forms)
def test_an_rss_description_of_lines_and_a_picture_is_called_by_its_words(
    described: tuple[str, list[str]], form: str
) -> None:
    description, words = described
    [posting] = read_feed(RSS_WRITERS[form]("A feed", None, description), "", ADDRESS).postings
    called = WORD_GAP.join(words)
    assert is_cut_from(posting.title, called, SUMMARY_TITLE_CHARACTERS), (
        f"an {form} posting without a title, described {description!r}, was called "
        f"{posting.title!r}, not {called!r} cut at {SUMMARY_TITLE_CHARACTERS}: a description "
        "is HTML"
    )


@given(description=plain_descriptions, form=rss_forms)
def test_an_rss_description_with_no_markup_sign_is_called_as_written(
    description: str, form: str
) -> None:
    [posting] = read_feed(RSS_WRITERS[form]("A feed", None, description), "", ADDRESS).postings
    written = WORD_GAP.join(description.split())
    assert is_cut_from(posting.title, written, SUMMARY_TITLE_CHARACTERS), (
        f"an {form} posting without a title, described {description!r}, which holds no < and no "
        f"&, was called {posting.title!r}, not as written, cut at {SUMMARY_TITLE_CHARACTERS}"
    )


@pytest.mark.parametrize("form", sorted(RSS_WRITERS))
@pytest.mark.parametrize("document", DOCUMENTS)
def test_an_rss_description_that_is_a_whole_html_document_reads_as_its_text_or_nothing(
    document: str, form: str
) -> None:
    written = document.format(words="Night trains")
    [posting] = read_feed(RSS_WRITERS[form]("A feed", None, written), "", ADDRESS).postings
    expected = "Night trains" if "{words}" in document else "https://feeds.test/p/1"
    assert posting.title == expected, (
        f"an {form} posting without a title, described {written!r}, was called "
        f"{posting.title!r}, not {expected!r}: a description that is a document is HTML"
    )


@given(text=marked_texts().filter(lambda text: "<" in text.markup), form=rss_forms)
def test_rss_text_that_is_a_fragment_of_common_html_elements_is_read_without_them(
    text: Marked, form: str
) -> None:
    write = RSS_WRITERS[form]
    feed = read_feed(write(text.markup, text.markup, None), "", ADDRESS)
    assert is_cut_from(feed.postings[0].title, text.plain, TITLE_CHARACTERS), (
        f"an {form} posting titled {text.markup!r} was called {feed.postings[0].title!r}, not "
        f"its text {text.plain!r}"
    )
    assert is_cut_from(feed.title, text.plain, TITLE_CHARACTERS), (
        f"an {form} feed titled {text.markup!r} was called {feed.title!r}, not {text.plain!r}"
    )
    [described] = read_feed(write("A feed", None, text.markup), "", ADDRESS).postings
    assert is_cut_from(described.title, text.plain, SUMMARY_TITLE_CHARACTERS), (
        f"an {form} posting described {text.markup!r} was called {described.title!r}, not the "
        f"first {SUMMARY_TITLE_CHARACTERS} characters of its text {text.plain!r}"
    )


class Cut(StrEnum):
    BEFORE_TITLE = "before its title"
    INSIDE_TITLE = "inside its title"
    AFTER_TITLE = "after its title"
    INSIDE_THIRD = "inside its third posting"


@st.composite
def titled_feeds_of_three(draw: st.DrawFn) -> SaidFeed:
    # Feeds with a title and three postings or four, each with an address and a key of its own.
    postings = draw(st.lists(said_postings(), min_size=3, max_size=4))
    said = draw(said_feeds(st.just([addressed_postings(p, n) for n, p in enumerate(postings)])))
    return said.model_copy(update={"title": draw(marked_texts())})


def cut_places(text: str, form: FeedForm) -> dict[Cut, tuple[int, int]]:
    # Where in a written feed each cut may fall, from the first place to the last.
    posting = POSTING_ELEMENTS[form]
    title = text.index("<title", text.index("</image>") if form is FeedForm.RSS else 0)
    title_end = text.index("</title>", title) + len("</title>")
    openings = [n for n in range(len(text)) if text.startswith(f"<{posting}", n)]
    third_end = text.index(f"</{posting}>", openings[2]) + len(f"</{posting}>")
    return {
        Cut.BEFORE_TITLE: (0, title),
        Cut.INSIDE_TITLE: (title + 1, title_end - 1),
        Cut.AFTER_TITLE: (title_end, openings[0]),
        Cut.INSIDE_THIRD: (openings[2] + 1, third_end - 1),
    }


@pytest.mark.parametrize("cut", list(Cut))
@given(
    served=served_feeds(titled_feeds_of_three(), st.sampled_from(XML_FORMS)),
    data=st.data(),
)
def test_a_feed_cut_short_is_a_feed_where_a_title_or_a_posting_was_read_whole(
    cut: Cut, served: ServedFeed, data: st.DataObject
) -> None:
    text = served.content.decode()
    first, last = cut_places(text, served.form)[cut]
    broken = text[: data.draw(st.integers(first, last))].encode()
    what = f"a {served.form} feed cut {cut.value}"
    if cut in (Cut.BEFORE_TITLE, Cut.INSIDE_TITLE):
        refusal(broken, served.declared, served.address, what)
        return
    feed = read_feed(broken, served.declared, served.address)
    expected = expected_postings(served.said, served.form, served.address)
    whole = 0 if cut is Cut.AFTER_TITLE else 2
    keys = [posting.key for posting in feed.postings]
    assert keys == [posting.key for posting in expected[:whole]], (
        f"{what} was read with the postings {keys}, not the {whole} it holds whole"
    )
    assert is_cut_from(
        feed.title, expected_feed_title(served.said, served.form, served.address), TITLE_CHARACTERS
    ), f"{what} was called {feed.title!r}, not by its title"


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
    posting = plain_posting(0, said_plainly("A posting"), "https://feeds.test/p/0").model_copy(
        update={"published": WrittenTime(text=written, meant=None)}
    )
    said = SaidFeed(title=None, site=None, base=None, postings=[posting])
    [read_posting] = read_feed(written_feed(said, form), "", address).postings
    return read_posting.published


def rfc822_text(moment: datetime, zone: str) -> str:
    day = DAY_NAMES[moment.weekday()]
    month = MONTH_NAMES[moment.month - 1]
    return f"{day}, {moment.day:02} {month} {moment.year} {moment.strftime('%H:%M:%S')} {zone}"


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
    expected = expected_feed_title(served.said, served.form, served.address)
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
