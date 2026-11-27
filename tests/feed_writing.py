import hashlib
import json
import unicodedata
from contextlib import suppress
from datetime import UTC, datetime, timedelta, timezone
from enum import StrEnum
from random import Random
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape, quoteattr

import pytest
from hypothesis import HealthCheck, Phase, settings
from hypothesis import strategies as st
from pydantic import AwareDatetime, BaseModel

from infonium.subscriptions.feed import (
    ADDRESS_CHARACTERS,
    KEY_CHARACTERS,
    TITLE_CHARACTERS,
    Feed,
    UnreadableFeed,
    read_feed,
)

# What a feed says, as a test makes it up, written out as RSS 2.0, Atom 1.0 or JSON Feed by
# writers of the tests' own, and what reading it must give back by the requirements of
# read_feed (F2 to F6).

# The characters of a posting's summary or content by which a posting without a title is
# called, and the character a cut title ends with.
SUMMARY_TITLE_CHARACTERS = 80
CUT_SHORT = "…"
WORD_GAP = " "
WEB_SCHEMES = ("http://", "https://")
RECORDING_KINDS = ("audio", "video")
# The category of Unicode of the marks that combine with the letter before them.
MARK_CATEGORY = "M"
XML_DECLARATION = '<?xml version="1.0" encoding="{encoding}"?>\n'
UTF_8 = "utf-8"


class FeedForm(StrEnum):
    RSS = "RSS 2.0"
    ATOM = "Atom 1.0"
    JSON_1 = "JSON Feed 1.0"
    JSON_1_1 = "JSON Feed 1.1"


JSON_FEED_VERSIONS = {
    FeedForm.JSON_1: "https://jsonfeed.org/version/1",
    FeedForm.JSON_1_1: "https://jsonfeed.org/version/1.1",
}
XML_FORMS = (FeedForm.RSS, FeedForm.ATOM)
# The kinds a server declares a feed of each form as, beside those that say nothing of it.
DECLARED_KINDS = {
    FeedForm.RSS: ("application/rss+xml", "application/xml", "text/xml; charset=utf-8"),
    FeedForm.ATOM: ("application/atom+xml", "application/xml", "text/xml"),
    FeedForm.JSON_1: ("application/feed+json", "application/json"),
    FeedForm.JSON_1_1: ("application/feed+json; charset=utf-8", "application/json"),
}
UNTOLD_KINDS = ("", "text/plain", "application/octet-stream")
RSS_NAMESPACES = (
    'xmlns:atom="http://www.w3.org/2005/Atom" '
    'xmlns:content="http://purl.org/rss/1.0/modules/content/"'
)
ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
HTML_TYPE = ' type="html"'


class Marked(BaseModel):
    # A text as a feed writes it, as HTML; the same without its elements, its references kept;
    # and the plain text a reader sees of it: its words with single spaces between them. A text
    # written blank has no plain text.
    markup: str
    unmarked: str
    plain: str


class Enclosure(BaseModel):
    address: str
    kind: str


class WrittenTime(BaseModel):
    # A time as a feed writes it, and the time it says; none where it says none that can be
    # read.
    text: str
    meant: AwareDatetime | None


def markup_of(text: Marked | None) -> str | None:
    return None if text is None else text.markup


def unmarked_of(text: Marked | None) -> str | None:
    return None if text is None else text.unmarked


def unmarked_text(text: str) -> Marked:
    return Marked(markup=text, unmarked=text, plain=WORD_GAP.join(text.split()))


def written_text(time: WrittenTime | None) -> str | None:
    return None if time is None else time.text


class SaidPosting(BaseModel):
    # What a feed says of one posting, its addresses as written. The links beside the page are
    # those that are not it: Atom's related and replies, RSS's comments. The relation of the
    # page is how Atom writes its link: alternate, or none.
    id: str | None
    title: Marked | None
    summary: Marked | None
    content: Marked | None
    page: str | None
    page_relation: str | None
    other_links: list[str]
    enclosures: list[Enclosure]
    published: WrittenTime | None
    updated: WrittenTime | None
    base: str | None


class SaidFeed(BaseModel):
    title: Marked | None
    site: str | None
    base: str | None
    postings: list[SaidPosting]


class ExpectedPosting(BaseModel):
    # What reading a posting must give: its key, its address, the plain text its title is cut
    # from and the most characters that title may hold, and its time.
    key: str
    address: str
    called: str
    called_characters: int
    published: AwareDatetime | None


# Texts in any script: words of letters, marks, digits, punctuation and symbols, among them
# the signs that markup writes as references, and letters HTML names.
NAMED_REFERENCES = {
    "é": "eacute",
    "—": "mdash",
    "©": "copy",
    "\N{RIGHT SINGLE QUOTATION MARK}": "rsquo",
    "ü": "uuml",
}
SIGN_REFERENCES = {
    "&": ("&amp;", "&#38;", "&#x26;"),
    "<": ("&lt;", "&#60;", "&#x3C;"),
    ">": ("&gt;", "&#62;", "&#x3e;"),
}
letters = st.characters(categories=("L", "M", "N", "P", "S"), exclude_characters="<>&")
words = st.lists(
    st.one_of(letters, letters, letters, st.sampled_from([*SIGN_REFERENCES, *NAMED_REFERENCES])),
    min_size=1,
    max_size=8,
).map("".join)
GAPS = (" ", "  ", "\n", "\t", " \n\t ")
INLINE_ELEMENTS = (
    ("b", ""),
    ("i", ""),
    ("em", ""),
    ("strong", ""),
    ("code", ""),
    ("span", ' class="note"'),
    ("a", ' href="https://links.test/note"'),
)


@st.composite
def marked_texts(draw: st.DrawFn, said_words: st.SearchStrategy[str] = words) -> Marked:
    # Words, some in inline elements, some of their letters written as character references,
    # with any white space between them; repeated many times at times, to be cut.
    said = draw(st.lists(said_words, min_size=1, max_size=5))
    said *= draw(st.sampled_from((1, 1, 1, 40, 90)))
    chance = Random(draw(st.integers(0, 2**32)))
    pieces = [chance.choice(("", *GAPS))]
    unmarked = pieces[:]
    for word in said:
        written = "".join(
            written_letter(letter, chance.random(), chance.randrange(3)) for letter in word
        )
        gap = chance.choice(GAPS)
        unmarked += [written, gap]
        if chance.random() < 0.3:
            name, attributes = chance.choice(INLINE_ELEMENTS)
            written = f"<{name}{attributes}>{written}</{name}>"
        pieces += [written, gap]
    pieces[-1] = unmarked[-1] = chance.choice(("", *GAPS))
    return Marked(markup="".join(pieces), unmarked="".join(unmarked), plain=WORD_GAP.join(said))


def written_letter(letter: str, chance: float, form: int) -> str:
    # A letter as HTML may write it: a sign of markup always as a reference, any other at times
    # by its number in decimal or hexadecimal or by its name.
    if letter in SIGN_REFERENCES:
        return SIGN_REFERENCES[letter][form]
    if chance > 0.15:
        return letter
    references = [f"&#{ord(letter)};", f"&#x{ord(letter):X};"]
    if letter in NAMED_REFERENCES:
        references.append(f"&{NAMED_REFERENCES[letter]};")
    return references[form % len(references)]


BLANK_TEXTS = (
    Marked(markup="", unmarked="", plain=""),
    Marked(markup=" \n ", unmarked=" \n ", plain=""),
    Marked(markup="<b></b>", unmarked="", plain=""),
)
titles = st.one_of(st.none(), st.sampled_from(BLANK_TEXTS), marked_texts())
summaries = st.one_of(
    st.none(),
    st.sampled_from(BLANK_TEXTS),
    marked_texts().map(lambda text: text.model_copy(update={"markup": f"<p>{text.markup}</p>"})),
)

# Addresses as a feed writes them: absolute, relative to its base, longer than infonium keeps,
# or of a scheme that is not the web's. Addresses with a space in them, and with white space
# around them, are written by the judgment of how an address is kept.
HOSTS = ("feeds.test", "blog.example.test", "news.test", "cdn.test")
SLUG_LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789-"
slugs = st.text(SLUG_LETTERS, min_size=1, max_size=16)
spaced_slugs = st.builds(lambda before, after: f"{before} {after}", slugs, slugs)
PADS = ("", " ", "\n  ", "\t ")


def padded(addresses: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    return st.tuples(st.sampled_from(PADS), addresses, st.sampled_from(PADS)).map("".join)


def absolute_addresses_of(slugs_written: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    return st.builds(
        lambda scheme, host, slug: f"{scheme}://{host}/{slug}",
        st.sampled_from(WEB_SCHEMES).map(lambda scheme: scheme.removesuffix("://")),
        st.sampled_from(HOSTS),
        slugs_written,
    )


absolute_addresses = absolute_addresses_of(slugs)
RELATIVE_FORMS = (
    "posts/{slug}",
    "/posts/{slug}",
    "../{slug}",
    "./{slug}.html",
    "{slug}?page=2",
    "//other.test/{slug}",
)


def relative_addresses_of(slugs_written: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    return st.builds(
        lambda form, slug: form.format(slug=slug), st.sampled_from(RELATIVE_FORMS), slugs_written
    )


relative_addresses = relative_addresses_of(slugs)
LONG_ADDRESS_HEAD = "https://feeds.test/"
# Where an address or an id is resolved to tell whether any base would change it.
UNBASED = "https://cdn.test/"
long_addresses = st.sampled_from(
    (ADDRESS_CHARACTERS - 1, ADDRESS_CHARACTERS, ADDRESS_CHARACTERS + 1, ADDRESS_CHARACTERS + 500)
).map(lambda length: LONG_ADDRESS_HEAD + "a" * (length - len(LONG_ADDRESS_HEAD)))
NOT_WEB_ADDRESSES = (
    "mailto:editor@feeds.test",
    "ftp://files.test/episode.mp3",
    "urn:isbn:9780000000002",
    "javascript:void(0)",
    "tel:+15550100",
    "magnet:?xt=urn:btih:c12fe1c06bba254a9dc9f519b335aa7c1367a88a",
)
not_web_addresses = st.sampled_from(NOT_WEB_ADDRESSES)
pages = st.one_of(
    st.none(), absolute_addresses, relative_addresses, long_addresses, not_web_addresses
)
BASES = ("https://base.test/root/", "https://feeds.test/blog/", "/blog/", "sub/", "./sub/")
# Bases that climb above the feed's folder, which test_a_base_that_climbs_is_resolved_as_written
# judges.
CLIMBING_BASES = ("../up/", "../", "../../a/b/")
bases = st.one_of(st.none(), st.sampled_from(BASES))
RECORDING_TYPES = ("audio/mpeg", "audio/x-m4a", "video/mp4", "Audio/MPEG", "audio/ogg; codecs=opus")
OTHER_TYPES = (
    "image/jpeg",
    "application/pdf",
    "text/html",
    "application/x-shockwave-flash",
    "application/octet-stream",
)
enclosures = st.builds(
    Enclosure,
    address=st.one_of(absolute_addresses, relative_addresses, not_web_addresses),
    kind=st.sampled_from(RECORDING_TYPES + OTHER_TYPES),
)

# Ids: a few that postings share, any text, and ids longer than a key is kept. A posting that
# has no address is given an id that is no web address, relative or absolute.
SHARED_IDS = ("tag:feeds.test,2026:1", "post-12", "https://feeds.test/p/1")
LONG_ID_HEAD = "tag:feeds.test,2026:"
LEFT_OUT_ID_HEAD = "tag:feeds.test,2026:left-out-"
ids = st.one_of(
    st.none(),
    st.sampled_from(SHARED_IDS),
    st.text(
        st.one_of(
            st.sampled_from("abcdefghijklmnopqrstuvwxyz0123456789:/._-?=&#"),
            st.characters(categories=("L", "N")),
        ),
        min_size=1,
        max_size=30,
    ),
    st.integers(KEY_CHARACTERS - 1, KEY_CHARACTERS + 40).map(
        lambda length: LONG_ID_HEAD + "x" * (length - len(LONG_ID_HEAD))
    ),
)

# Times as RFC 3339, ISO 8601 and RFC 822 write them, with and without a zone, and times
# written in no such way.
ZONE_NAMES = {
    "GMT": 0,
    "UT": 0,
    "EST": -5,
    "EDT": -4,
    "CST": -6,
    "CDT": -5,
    "MST": -7,
    "MDT": -6,
    "PST": -8,
    "PDT": -7,
}
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
UNREADABLE_TIMES = (
    "",
    "soon after dinner",
    "yesterday",
    "13/12/2003 10:00",
    "2003-13-45T10:00:00Z",
    "Tue, 31 Feb 2003 10:00:00 GMT",
    "twenty past nine",
)
moments = st.datetimes(min_value=datetime(1990, 1, 1), max_value=datetime(2090, 12, 31))
offsets = st.integers(-48, 56).map(lambda quarters: timedelta(minutes=15 * quarters))


def numeric_zone(offset: timedelta, apart: str) -> str:
    minutes = round(offset.total_seconds() // 60)
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02}{apart}{abs(minutes) % 60:02}"


@st.composite
def rfc3339_times(draw: st.DrawFn) -> WrittenTime:
    moment, offset = draw(moments), draw(offsets)
    fraction = draw(st.sampled_from(("", ".3", ".000", ".123456")))
    meant = moment.replace(
        microsecond=round(float(f"0{fraction}") * 1_000_000) if fraction else 0,
        tzinfo=timezone(offset),
    )
    zone = "Z" if not offset and draw(st.booleans()) else numeric_zone(offset, ":")
    gap = draw(st.sampled_from(("T", " ")))
    return WrittenTime(text=meant.strftime(f"%Y-%m-%d{gap}%H:%M:%S{fraction}{zone}"), meant=meant)


@st.composite
def iso_times_without_zone(draw: st.DrawFn) -> WrittenTime:
    moment = draw(moments).replace(microsecond=0)
    if draw(st.booleans()):
        day = moment.replace(hour=0, minute=0, second=0, tzinfo=UTC)
        return WrittenTime(text=day.strftime("%Y-%m-%d"), meant=day)
    return WrittenTime(text=moment.strftime("%Y-%m-%dT%H:%M:%S"), meant=moment.replace(tzinfo=UTC))


@st.composite
def rfc822_times(draw: st.DrawFn) -> WrittenTime:
    moment = draw(moments).replace(microsecond=0)
    named = draw(st.sampled_from([None, *ZONE_NAMES]))
    offset = timedelta(hours=ZONE_NAMES[named]) if named else draw(offsets)
    zone = named or draw(st.sampled_from((numeric_zone(offset, ""), "-0000", "")))
    if zone in ("-0000", ""):
        offset = timedelta(0)
    meant = moment.replace(tzinfo=timezone(offset))
    day = draw(st.sampled_from((f"{DAY_NAMES[moment.weekday()]}, ", "")))
    clock = "%H:%M:%S" if draw(st.booleans()) or meant.second else "%H:%M"
    month = MONTH_NAMES[moment.month - 1]
    text = f"{day}{moment.day:02} {month} {moment.year} {moment.strftime(clock)} {zone}"
    return WrittenTime(text=text.strip(), meant=meant)


readable_times = st.one_of(rfc3339_times(), iso_times_without_zone(), rfc822_times())
unreadable_times = st.builds(WrittenTime, text=st.sampled_from(UNREADABLE_TIMES), meant=st.none())
written_times = st.one_of(readable_times, unreadable_times)


def browser_form(address: str) -> str:
    # An address as a browser asks for it: without the white space around it, and a space in
    # it written %20.
    return address.strip().replace(" ", "%20")


def is_web_address(address: str) -> bool:
    return address.lower().startswith(WEB_SCHEMES)


def is_relative(address: str) -> bool:
    return not is_web_address(address) and address not in NOT_WEB_ADDRESSES


def is_kept_address(address: str) -> bool:
    return is_web_address(address) and len(address) <= ADDRESS_CHARACTERS


def is_recording(enclosure: Enclosure) -> bool:
    return enclosure.kind.split("/")[0].strip().lower() in RECORDING_KINDS


def is_addressed(posting: SaidPosting) -> bool:
    # Whether a posting has an address infonium keeps, whatever base it is resolved against:
    # a relative address resolves to a web address of the base's scheme.
    written = [enclosure.address for enclosure in posting.enclosures if is_recording(enclosure)]
    written += [] if posting.page is None else [posting.page]
    return any(
        is_relative(browser_form(address)) or is_kept_address(browser_form(address))
        for address in written
    )


def clear_of_bases(posting: SaidPosting, base: str | None) -> SaidPosting:
    # A posting whose enclosures and id are written so that no base can change them where a
    # base applies: its relative enclosures made absolute, a relative id made a tag. The
    # judgments test_an_enclosures_relative_address_is_resolved_against_the_base_the_feed_gives
    # and test_a_postings_key_is_its_id_as_written_under_any_base judge those cases.
    if base is None and posting.base is None:
        return posting
    absolute = [
        enclosure.model_copy(update={"address": urljoin(UNBASED, browser_form(enclosure.address))})
        if is_relative(browser_form(enclosure.address))
        else enclosure
        for enclosure in posting.enclosures
    ]
    relative_id = posting.id is not None and urljoin(UNBASED, posting.id) != posting.id
    tagged = f"{LONG_ID_HEAD}{posting.id}" if relative_id else posting.id
    return posting.model_copy(update={"enclosures": absolute, "id": tagged})


@st.composite
def said_postings(draw: st.DrawFn) -> SaidPosting:
    # A posting whose time published cannot be read is given no time updated that can, so
    # that which of the two is its time is not in doubt. A base of a posting's own is written
    # by test_a_postings_base_applies_to_it_alone_and_within_the_feeds alone.
    published = draw(st.none() | written_times)
    updated = draw(
        st.none() | (unreadable_times if published and published.meant is None else written_times)
    )
    return SaidPosting(
        id=draw(ids),
        title=draw(titles),
        summary=draw(summaries),
        content=draw(summaries),
        page=draw(pages),
        page_relation=draw(st.sampled_from(("alternate", None))),
        other_links=draw(st.lists(absolute_addresses, max_size=2)),
        enclosures=draw(st.lists(enclosures, max_size=3)),
        published=published,
        updated=updated,
        base=None,
    )


@st.composite
def said_feeds(
    draw: st.DrawFn, postings: st.SearchStrategy[list[SaidPosting]] | None = None
) -> SaidFeed:
    # A feed's postings that have no address keep an id of their own, so that which of two
    # postings with one key is kept is never a posting that is left out.
    said = draw(st.lists(said_postings(), max_size=4) if postings is None else postings)
    base = draw(bases)
    return SaidFeed(
        title=draw(titles),
        site=draw(st.one_of(st.none(), absolute_addresses, relative_addresses)),
        base=base,
        postings=[
            clear_of_bases(posting, base)
            if is_addressed(posting) or posting.id is None
            else clear_of_bases(posting, base).model_copy(
                update={"id": f"{LEFT_OUT_ID_HEAD}{number}"}
            )
            for number, posting in enumerate(said)
        ],
    )


def addressed_postings(posting: SaidPosting, number: int) -> SaidPosting:
    # A posting made to have an address of its own and a key no other posting has.
    return posting.model_copy(
        update={"id": f"tag:feeds.test,2026:{number}", "page": f"https://feeds.test/p/{number}"}
    )


def addressed_feeds() -> st.SearchStrategy[SaidFeed]:
    # Feeds whose every posting has an address and a key of its own.
    return said_feeds(
        st.lists(said_postings(), max_size=4).map(
            lambda postings: [addressed_postings(posting, n) for n, posting in enumerate(postings)]
        )
    )


forms = st.sampled_from(FeedForm)
feed_addresses = st.builds(
    lambda scheme, host, path: f"{scheme}://{host}{path}",
    st.sampled_from(("http", "https")),
    st.sampled_from(("feeds.test", "blog.example.test", "news.test")),
    st.sampled_from(("/feed.xml", "/a/b/rss", "/feed/?format=atom", "/")),
)


def declared_kinds(form: FeedForm) -> st.SearchStrategy[str]:
    return st.sampled_from(DECLARED_KINDS[form] + UNTOLD_KINDS)


def keyed_postings(postings: list[SaidPosting]) -> list[SaidPosting]:
    # Postings given ids of their own, so that none is left out for its key.
    return [
        posting.model_copy(update={"id": f"tag:feeds.test,2026:{number}"})
        for number, posting in enumerate(postings)
    ]


keyed_feeds = said_feeds(st.lists(said_postings(), max_size=4).map(keyed_postings))


class ServedFeed(BaseModel):
    # A feed as a server sends it: what it says, written in a form, declared as a kind, at an
    # address.
    said: SaidFeed
    form: FeedForm
    content: bytes
    declared: str
    address: str


@st.composite
def served_feeds(
    draw: st.DrawFn,
    feeds: st.SearchStrategy[SaidFeed] | None = None,
    in_forms: st.SearchStrategy[FeedForm] = forms,
) -> ServedFeed:
    said = draw(said_feeds() if feeds is None else feeds)
    form = draw(in_forms)
    return ServedFeed(
        said=said,
        form=form,
        content=written_feed(said, form),
        declared=draw(declared_kinds(form)),
        address=draw(feed_addresses),
    )


def youtube_feed(channel: str, videos: list[str]) -> bytes:
    # A feed in the shape YouTube gives a channel's: each video's page as its alternate link and
    # the video as media:content of a Flash type, with no enclosure.
    entries = "".join(
        f"<entry><id>yt:video:{video}</id><yt:videoId>{video}</yt:videoId>"
        f"<yt:channelId>{channel}</yt:channelId><title>A made-up video {number}</title>"
        f'<link rel="alternate" href="https://www.youtube.com/watch?v={video}"/>'
        "<author><name>A made-up channel</name>"
        f"<uri>https://www.youtube.com/channel/{channel}</uri></author>"
        f"<published>2026-11-{number + 1:02}T10:00:00+00:00</published>"
        f"<updated>2026-11-{number + 2:02}T10:00:00+00:00</updated>"
        f"<media:group><media:title>A made-up video {number}</media:title>"
        f'<media:content url="https://www.youtube.com/v/{video}?version=3" '
        'type="application/x-shockwave-flash" width="640" height="390"/>'
        f'<media:thumbnail url="https://i1.ytimg.com/vi/{video}/hqdefault.jpg" width="480" '
        'height="360"/><media:description>Made up for the tests.</media:description>'
        "</media:group></entry>"
        for number, video in enumerate(videos)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
        f'xmlns:media="http://search.yahoo.com/mrss/" xmlns="{ATOM_NAMESPACE}">'
        f'<link rel="self" href="https://www.youtube.com/feeds/videos.xml?channel_id={channel}"/>'
        f"<id>yt:channel:{channel.removeprefix('UC')}</id><yt:channelId>{channel}</yt:channelId>"
        "<title>A made-up channel</title>"
        f'<link rel="alternate" href="https://www.youtube.com/channel/{channel}"/>'
        f"<published>2020-01-01T00:00:00+00:00</published>{entries}</feed>"
    ).encode()


# The writers.


def written_feed(feed: SaidFeed, form: FeedForm) -> bytes:
    match form:
        case FeedForm.RSS:
            return (XML_DECLARATION.format(encoding=UTF_8) + rss_body(feed)).encode()
        case FeedForm.ATOM:
            return (XML_DECLARATION.format(encoding=UTF_8) + atom_body(feed)).encode()
        case _:
            return json_feed(feed, form).encode()


def element(name: str, text: str | None, attributes: str = "") -> str:
    return "" if text is None else f"<{name}{attributes}>{escape(text)}</{name}>"


def based(base: str | None) -> str:
    return "" if base is None else f" xml:base={quoteattr(base)}"


def rss_body(feed: SaidFeed) -> str:
    # The channel's image, with a title and a link of its own, stands before the channel's.
    image = (
        "<image><url>https://images.test/logo.png</url><title>The logo of the channel</title>"
        "<link>https://images.test/</link></image>"
    )
    channel = (
        f"{image}{element('title', markup_of(feed.title))}{element('link', feed.site)}"
        "<description>Made up for infonium's tests</description>"
        f"{''.join(rss_item(posting) for posting in feed.postings)}"
    )
    return (
        f'<rss version="2.0" {RSS_NAMESPACES}><channel{based(feed.base)}>{channel}</channel></rss>'
    )


def rss_item(posting: SaidPosting) -> str:
    enclosed = "".join(
        f"<enclosure url={quoteattr(enclosure.address)} type={quoteattr(enclosure.kind)} "
        'length="1024"/>'
        for enclosure in posting.enclosures
    )
    content = "" if posting.content is None else posting.content.markup
    if "]]>" in content:
        raise ValueError("a content written in CDATA holds the end of CDATA")
    parts = (
        element("title", markup_of(posting.title)),
        "".join(element("comments", link) for link in posting.other_links),
        element("link", posting.page),
        element("guid", posting.id, ' isPermaLink="false"'),
        element("description", markup_of(posting.summary)),
        ""
        if posting.content is None
        else f"<content:encoded><![CDATA[{content}]]></content:encoded>",
        enclosed,
        element("pubDate", written_text(posting.published)),
        element("atom:updated", written_text(posting.updated)),
    )
    return f"<item{based(posting.base)}>{''.join(parts)}</item>"


def atom_link(address: str, relation: str | None, kind: str = "") -> str:
    named = "" if relation is None else f" rel={quoteattr(relation)}"
    typed = f" type={quoteattr(kind)}" if kind else ""
    return f"<link{named}{typed} href={quoteattr(address)}/>"


def atom_body(feed: SaidFeed) -> str:
    head = (
        f"{element('title', markup_of(feed.title), HTML_TYPE)}"
        "<id>urn:uuid:3b1f0c52-1d7e-4f43-9a51-8f2b6c1d0e7a</id>"
        "<updated>2026-11-27T00:00:00Z</updated><author><name>A made-up writer</name></author>"
        f"{'' if feed.site is None else atom_link(feed.site, 'alternate', 'text/html')}"
        f"{atom_link('https://feeds.test/self.atom', 'self')}"
    )
    entries = "".join(atom_entry(posting) for posting in feed.postings)
    return f'<feed xmlns="{ATOM_NAMESPACE}"{based(feed.base)}>{head}{entries}</feed>'


def atom_entry(posting: SaidPosting) -> str:
    parts = (
        element("id", posting.id),
        element("title", markup_of(posting.title), HTML_TYPE),
        "".join(
            atom_link(link, relation)
            for link, relation in zip(posting.other_links, ("related", "replies"), strict=False)
        ),
        "" if posting.page is None else atom_link(posting.page, posting.page_relation),
        "".join(
            atom_link(enclosure.address, "enclosure", enclosure.kind)
            for enclosure in posting.enclosures
        ),
        element("summary", markup_of(posting.summary), HTML_TYPE),
        element("content", markup_of(posting.content), HTML_TYPE),
        element("published", written_text(posting.published)),
        element("updated", written_text(posting.updated)),
    )
    return f"<entry{based(posting.base)}>{''.join(parts)}</entry>"


def json_feed(feed: SaidFeed, form: FeedForm) -> str:
    # JSON Feed 1.0 is written with every character beyond ASCII escaped, 1.1 as it is. Its
    # titles and summaries are written without elements, as the format's text is plain; its
    # content is HTML.
    items = [
        {
            name: value
            for name, value in (
                ("id", posting.id),
                ("url", posting.page),
                ("title", unmarked_of(posting.title)),
                ("summary", unmarked_of(posting.summary)),
                ("content_html", markup_of(posting.content)),
                ("date_published", written_text(posting.published)),
                ("date_modified", written_text(posting.updated)),
                (
                    "attachments",
                    [
                        {"url": enclosure.address, "mime_type": enclosure.kind}
                        for enclosure in posting.enclosures
                    ]
                    or None,
                ),
            )
            if value is not None
        }
        for posting in feed.postings
    ]
    said = {
        name: value
        for name, value in (
            ("version", JSON_FEED_VERSIONS[form]),
            ("title", unmarked_of(feed.title)),
            ("home_page_url", feed.site),
            ("items", items),
        )
        if value is not None
    }
    return json.dumps(said, ensure_ascii=form == FeedForm.JSON_1)


# What reading must give.


def postings_base(feed: SaidFeed, form: FeedForm, address: str) -> str:
    # The base the feed gives its postings: its xml:base, resolved against its own address.
    # JSON Feed has none.
    return urljoin(address, feed.base or "") if form in XML_FORMS else address


def posting_address(posting: SaidPosting, base: str) -> str | None:
    recordings = [
        urljoin(base, browser_form(enclosure.address))
        for enclosure in posting.enclosures
        if is_recording(enclosure)
        and is_web_address(urljoin(base, browser_form(enclosure.address)))
    ]
    page = None if posting.page is None else urljoin(base, browser_form(posting.page))
    return recordings[0] if recordings else page


def key_of(text: str) -> str:
    return text if len(text) <= KEY_CHARACTERS else hashlib.sha256(text.encode()).hexdigest()


def calling(posting: SaidPosting, address: str) -> tuple[str, int]:
    texts = (
        (posting.title, TITLE_CHARACTERS),
        (posting.summary, SUMMARY_TITLE_CHARACTERS),
        (posting.content, SUMMARY_TITLE_CHARACTERS),
    )
    return next(
        ((text.plain, characters) for text, characters in texts if text and text.plain),
        (address, TITLE_CHARACTERS),
    )


def expected_postings(feed: SaidFeed, form: FeedForm, address: str) -> list[ExpectedPosting]:
    base = postings_base(feed, form, address)
    expected: dict[str, ExpectedPosting] = {}
    for posting in feed.postings:
        posting_base = urljoin(base, posting.base or "") if form in XML_FORMS else base
        resolved = posting_address(posting, posting_base)
        if resolved is None or not is_kept_address(resolved):
            continue
        key = key_of(posting.id if posting.id is not None else resolved)
        called, characters = calling(posting, resolved)
        times = (posting.published, posting.updated)
        published = next((time.meant for time in times if time and time.meant), None)
        expected.setdefault(
            key,
            ExpectedPosting(
                key=key,
                address=resolved,
                called=called,
                called_characters=characters,
                published=published,
            ),
        )
    return list(expected.values())


def expected_feed_title(feed: SaidFeed, address: str) -> str:
    return feed.title.plain if feed.title and feed.title.plain else str(urlsplit(address).hostname)


def expected_site(feed: SaidFeed, form: FeedForm, address: str) -> str | None:
    base = urljoin(address, feed.base or "") if form in XML_FORMS else address
    return None if feed.site is None else urljoin(base, browser_form(feed.site))


def cut_titles(plain: str, ends: list[int]) -> set[str]:
    # The titles a plain text cut at each of the ends given reads as: its beginning up to the
    # end with an ellipsis as its last character, a space the cut leaves at the end gone or not.
    return {plain[:end] + CUT_SHORT for end in ends} | {
        plain[:end].rstrip() + CUT_SHORT for end in ends
    }


def unparted_end(plain: str, end: int) -> int:
    # The latest end at or before the one given that parts no letter from the marks that
    # combine with it.
    while end > 0 and unicodedata.category(plain[end]).startswith(MARK_CATEGORY):
        end -= 1
    return end


def is_cut_from(read: str, plain: str, characters: int) -> bool:
    # Whether a title read is the plain text given, or, where that is longer than the
    # characters a title may hold, its beginning cut to them with an ellipsis as its last
    # character, as late as the cut can be without parting a letter from its marks.
    if len(plain) <= characters:
        return read == plain
    return read in cut_titles(plain, [unparted_end(plain, characters - len(CUT_SHORT))])


def is_cut_near(read: str, plain: str, characters: int) -> bool:
    # As is_cut_from, but the cut may also part a letter from its marks: the judgments of what a
    # title is hold it to this, and test_a_title_is_never_cut_between_a_letter_and_the_marks_
    # that_combine_with_it alone to where the cut falls among marks.
    if len(plain) <= characters:
        return read == plain
    end = characters - len(CUT_SHORT)
    return read in cut_titles(plain, [end, unparted_end(plain, end)])


def same_time(read: datetime | None, meant: datetime | None) -> bool:
    # Two times are the same when they are none both, or the same moment in the same zone.
    if read is None or meant is None:
        return read is meant
    return read == meant and read.utcoffset() == meant.utcoffset()


# Reading what was written, as the judgments of read_feed do.

ADDRESS = "https://feeds.test/feed.xml"
RSS_KIND = "application/rss+xml"
# A judgment of a fault that is known, marked as an expected failure, reports the first example
# that fails without shrinking it, which would take the time of a dozen judgments. Judgments that
# poll and find feeds share their stand-in sites, a fixture, across examples.
FAULT_EXAMPLES = settings(
    phases=(Phase.explicit, Phase.reuse, Phase.generate),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


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
