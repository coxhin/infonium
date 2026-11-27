import codecs
import hashlib
import json
import re
import sys
import unicodedata
import xml.sax
from collections.abc import Iterable
from contextlib import suppress
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from enum import StrEnum
from io import BytesIO
from itertools import islice
from typing import Annotated, Any
from urllib.parse import urljoin, urlsplit
from xml.sax import SAXException, SAXNotSupportedException
from xml.sax.handler import feature_external_ges, feature_namespaces
from xml.sax.xmlreader import InputSource

import feedparser
import filetype
from lxml import etree
from lxml.html import defs
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    SkipValidation,
    StringConstraints,
    TypeAdapter,
)

from infonium.document import (
    CONTENT_TYPE,
    PAGE_MEDIA_TYPES,
    RECORDING_MEDIA_TYPES,
    WEB_SCHEMES,
    WORD_GAP,
    declared_media_type,
)
from infonium.htmlpage import SHOWN_TEXT

# The most characters kept of a posting's key, of a title, and of an address. A feed is another
# site's text: what goes beyond is cut, replaced or left out, as read_feed says.
KEY_CHARACTERS = 200
TITLE_CHARACTERS = 300
ADDRESS_CHARACTERS = 2000
WEB_ADDRESS_PATTERN = r"^(?i:https?)://\S+$"
# What a title cut short ends with.
TITLE_CUT_SHORT = "\N{HORIZONTAL ELLIPSIS}"

PostingKey = Annotated[str, StringConstraints(min_length=1, max_length=KEY_CHARACTERS)]
Title = Annotated[str, StringConstraints(min_length=1, max_length=TITLE_CHARACTERS)]
WebAddress = Annotated[
    str, StringConstraints(pattern=WEB_ADDRESS_PATTERN, max_length=ADDRESS_CHARACTERS)
]

# The most postings kept of one reading of a feed, the first it lists.
MOST_POSTINGS = 2000
# feedparser builds every posting a feed lists before any is kept: 22 MiB of 160,000 postings,
# each with a title, a link, a guid and a date, took it 15 s and 300 MiB, and 20 MiB of three
# million empty ones 12 s. So a feed is read up to the posting after twice as many as are
# kept, and is cut where that one begins; feedparser reads a feed so cut as one that is not
# well-formed. A posting is told by its element's opening, which may stand inside the text of
# another and cut the feed sooner.
READ_POSTINGS = 2 * MOST_POSTINGS
POSTING_OPENING = re.compile(r"<(?:[\w.-]+:)?(?:item|entry)[\s/>]", re.IGNORECASE)
# feedparser also builds every link, enclosure, category, content, author and thumbnail it
# finds, close to a kilobyte each: one posting with 1.3 million links took a gigabyte. So a
# feed is read no further than the element of these kinds after ten for each posting read, and
# of one posting no more than 50 of each are looked at.
READ_LISTED = 10 * READ_POSTINGS
LISTED_OPENING = re.compile(
    r"<(?:[\w.-]+:)?(?:link|enclosure|category|content|author|contributor|thumbnail)[\s/>]",
    re.IGNORECASE,
)
MOST_LISTED = 50
# How much of its summary's text a posting without a title is called by.
SUMMARY_TITLE_CHARACTERS = 80

# A byte order mark names the encoding of what follows it; the codec named reads past it. The
# mark of UTF-32 begins with that of UTF-16, and is looked for first.
BYTE_ORDER_MARKS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
XML_DECLARED_ENCODING = re.compile(rb"\s*<\?xml[^>]*?\sencoding\s*=\s*[\"']([A-Za-z][\w.:-]*)")
DEFAULT_ENCODING = "utf-8"
# The characters that XML allows nowhere. A feed that is not well-formed may hold them, and a
# text may come to hold them where a feed refers to them by number or by a JSON escape, or
# names an encoding such as UTF-7 that decodes to half a pair of surrogates: lxml refuses to
# parse markup that holds one, and a surrogate cannot be written as UTF-8. Each becomes a space.
NOT_IN_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
# Encodings that feeds name for a larger one they are written in, as browsers read them (the
# WHATWG Encoding Standard): a feed declared as GB2312 often holds characters of GBK, and one
# declared as Shift_JIS those of Windows-31J. Each is named as Python's codecs name it.
LARGER_ENCODINGS = {
    "gb2312": "gb18030",
    "gbk": "gb18030",
    "shift_jis": "cp932",
    "euc_kr": "cp949",
    "big5": "big5hkscs",
}

# A feed is another site's text, and no entity it declares is expanded: an entity that names
# a file or an address is never read, and one that holds others, or a long text, cannot blow
# a small feed up into gigabytes. A declaration is replaced by a space, so that what stood
# around it cannot join into another. A declaration is looked for no further than the next
# one begins, so that the text is read once however many declarations stand unclosed in it.
ENTITY_DECLARATION = re.compile(r"<!ENTITY(?:(?!<!ENTITY)[^>])*>", re.IGNORECASE)
ENTITY_REMOVED = " "
# feedparser fails on a feed that refers by number to a character that no text holds: half a
# pair of surrogates, as some feeds write each half of an emoji, or a number beyond Unicode;
# and Python reads no number of more than 4,300 digits, in feedparser or in html.unescape.
# Such a reference becomes a space before either reads it; a reference of fewer digits than
# any of those has is left as it is.
LONG_REFERENCE = re.compile(r"&#(?:([0-9]{5,})|[xX]([0-9a-fA-F]{4,}));?")
# lxml also refuses the text its parser of HTML makes of markup that refers by number to a
# character XML allows nowhere, as &#11; does, so in markup every such reference becomes a
# space before lxml reads it.
NUMBERED_REFERENCE = re.compile(r"&#(?:([0-9]+)|[xX]([0-9a-fA-F]+));?")
MOST_CODE_POINT_DIGITS = 7
BEYOND_UNICODE = sys.maxunicode + 1
DECIMAL = 10
HEXADECIMAL = 16
# feedparser is told that the feed is UTF-8, the encoding read_feed gives it the feed in, and
# where it was served, against which, and the xml:base of the elements around each, it resolves
# every address of the feed, as XML has it. An id is kept as written: a posting's key does not
# change with a base or the address of its feed. The names below are those of feedparser's
# result, and those of its parsers that read ids and links.
DECODED_FEED = "application/xml; charset=utf-8"
HEADERS = "headers"
VERSION = "version"
FEED = "feed"
ENTRIES = "entries"
BOZO = "bozo"
BOZO_EXCEPTION = "bozo_exception"
ID = "id"
HREF = "href"
LINKS = "links"

# What a web page opens with, after an XML declaration and comments, and what a feed opens
# with. A page declared as one but opening as a feed is taken for a feed, as some servers
# declare their feeds as pages. Comments are taken one after another and never given back, so
# that an opening that is not a page's is told so at once however many comments it begins
# with.
OPENING_CHARACTERS = 4096
PAGE_OPENING = re.compile(
    r"\s*+(?:<\?xml[^>]*+>\s*+)?(?:<!--(?:[^-]|-(?!->))*+-->\s*+)*+<(?:!doctype\s+html|html)\b",
    re.IGNORECASE,
)
FEED_OPENING = re.compile(r"\s*(?:<\?xml|<rss|<feed|<rdf:rdf|\{)", re.IGNORECASE)
JSON_OPENINGS = ("{", "[")
XML_OPENING = "<"

# What a reader is told of bytes that hold no feed: what they hold instead.
NOT_A_FEED = "{address} is not a feed but {found}"
FOUND_NOTHING = "an empty answer"
FOUND_PAGE = "a web page"
FOUND_JSON = "JSON that is not a JSON Feed"
FOUND_XML = "XML that is not RSS or Atom"
FOUND_FILE = "a file of the kind {kind}"
FOUND_TEXT = "text that is not RSS, Atom or a JSON Feed"
FOUND_TANGLED = "XML whose elements overlap too much to read it as RSS or Atom"
FOUND_BROKEN = "RSS or Atom that breaks off, or is broken, before its title and its first posting"

# The relations of a link that feedparser reports: the page of a posting or of a feed, and a
# file it carries. A link without a relation is an alternate one, which feedparser says.
ALTERNATE = "alternate"
# The versions feedparser gives a feed of Atom begin so.
ATOM = "atom"
ENCLOSURE = "enclosure"
PLAIN_TEXT = "text/plain"
MARKUP_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# The elements after which the text of markup goes on as a new word.
BREAKING_ELEMENTS = tuple(
    defs.block_tags
    | {"br", "article", "aside", "figcaption", "figure", "footer", "header", "main", "section"}
)

# An address is taken as a browser takes one: without the white space around it, the tabs and
# line breaks within it, and with a space within it written as %20.
OUTER_SPACE = " \t\n\r\f"
INNER_BREAKS = re.compile(r"[\t\n\r]")
SPACE = " "
WRITTEN_SPACE = "%20"

JSON_FEED_VERSION = r"^https?://jsonfeed\.org/version/"
# A JSON Feed is read by Python's reader of JSON, which builds what the document holds, all its
# postings among it, before any is looked at: it takes less memory for each value than
# pydantic's, and where it runs out of memory raises an error that can be told, where pydantic's
# ends the process. A number is kept as written, as Python reads no whole number of more than
# 4,300 digits, and a posting's id written as a number is taken as text. Half a pair of
# surrogates written as an escape, which a feed whose writer cut a text inside an emoji writes,
# is read as the replacement character, as text that holds it cannot be written out again. An
# escape is one where the backslashes before the u are odd in number, the others standing for
# themselves; a whole pair is kept.
SURROGATE_ESCAPE = re.compile(
    r"(?<!\\)((?:\\\\)*)\\u([dD][89abAB][0-9a-fA-F]{2}(?:\\u[dD][c-fC-F][0-9a-fA-F]{2})?"
    r"|[dD][c-fC-F][0-9a-fA-F]{2})"
)
WHOLE_PAIR_ESCAPE_CHARACTERS = 10
REPLACEMENT_ESCAPE = "\\ufffd"

# How a time is read: as RFC 3339 and ISO 8601 write it, then as RFC 822 does, in the zone it
# is written in. feedparser's own reading of the other ways feeds write a time is not used: it
# fills in a year, a month or a day that a time leaves out from the clock, and 71 real feeds
# wrote no time that only it could read. ISO 8601 reads a zone or refuses the time. Zones that
# feeds write in RFC 822's place but not as it writes them, an offset with a colon and GMT with
# an offset, are written as RFC 822 writes them before it reads them; a time whose zone it then
# cannot read, as JST, is none, never the same clock time in UTC, and one written with no zone,
# or with -0000, which RFC 822 gives a time in UTC whose zone is unknown, is in UTC.
COLON_OFFSET = re.compile(r"([+-])(\d{2}):(\d{2})$")
GMT_OFFSET = re.compile(r"(?:GMT|UTC|UT)([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
NO_ZONE = re.compile(r"(?:\d{1,2}:\d{2}(?::\d{2})?|\d{4}|-0000)$")


class Posting(BaseModel):
    # One thing a feed lists for reading: an article, an episode, a video. The key tells it from
    # the feed's other postings and is the same each time the feed is read. The address is what
    # is fetched and read when the posting is admitted to a library: its recording where it
    # carries one, its page otherwise.
    key: PostingKey
    title: Title
    address: WebAddress
    published: AwareDatetime | None


class Feed(BaseModel):
    # What a feed holds when it is read: what it calls itself, the site it says it belongs to,
    # and its postings in the order it lists them, which is the newest first in most feeds.
    title: Title
    site: WebAddress | None
    postings: list[Posting]


class UnreadableFeed(Exception):
    # An address that gives no feed to read, and why, in words a reader is shown.
    pass


class TextKind(StrEnum):
    PLAIN = "plain"
    MARKUP = "markup"


class GivenText(BaseModel):
    # A text as a feed gives it: as it is, or as HTML.
    value: str
    kind: TextKind


class Listing(BaseModel):
    # One posting as a feed lists it, whatever the feed's format: its id, the addresses it may
    # be read at in the order they are preferred (its recordings, then its pages), its title,
    # the texts that summarise it, and the times it was published and last updated, as written.
    id: str
    addresses: list[str]
    title: GivenText | None
    summaries: list[GivenText]
    times: list[str]


def text_or_nothing(value: object) -> object:
    return value if isinstance(value, str) else ""


def mapping_or_nothing(value: object) -> object:
    return value if isinstance(value, dict) else None


def mappings_of(value: object) -> object:
    listed = value[:MOST_LISTED] if isinstance(value, list) else []
    return [item for item in listed if isinstance(item, dict)]


# What feedparser gives is whatever a feed made of it. It leaves a text as None where it lost
# track of an element of a feed that is not well-formed, and keeps an element it does not know
# under the element's own name, so that <links version="2.0"> makes the links of a feed a
# mapping and <links>x</links> a text. A value of another kind than feedparser's own is taken
# for none.
ParsedString = Annotated[str, BeforeValidator(text_or_nothing)]


class ParsedText(BaseModel):
    # A text as feedparser gives it, with the media type it takes it for.
    value: ParsedString = ""
    type: ParsedString = PLAIN_TEXT


class ParsedLink(BaseModel):
    href: ParsedString = ""
    rel: ParsedString = ALTERNATE
    type: ParsedString = ""


ParsedDetail = Annotated[ParsedText | None, BeforeValidator(mapping_or_nothing)]
ParsedTexts = Annotated[list[ParsedText], BeforeValidator(mappings_of)]
ParsedLinks = Annotated[list[ParsedLink], BeforeValidator(mappings_of)]


class ParsedEntry(BaseModel):
    # What feedparser reads of a posting of RSS or Atom, by the names it gives.
    id: ParsedString = ""
    link: ParsedString = ""
    links: ParsedLinks = []
    title_detail: ParsedDetail = None
    summary_detail: ParsedDetail = None
    content: ParsedTexts = []
    published: ParsedString = ""
    updated: ParsedString = ""


class ParsedChannel(BaseModel):
    title_detail: ParsedDetail = None
    link: ParsedString = ""
    links: ParsedLinks = []


class ParsedFeed(BaseModel):
    # What feedparser reads of a feed of RSS or Atom. Its version is empty where it recognised
    # neither. Its postings are read one at a time, as far as they are kept. Where the feed is
    # not well-formed, what its XML parser found wrong is given as a SAXException; feedparser
    # also gives other exceptions here, of the encoding, which leave the feed well-formed.
    version: ParsedString = ""
    feed: ParsedChannel
    entries: SkipValidation[list[dict[str, object]]]
    bozo_exception: SkipValidation[object] = None

    def is_broken(self) -> bool:
        return isinstance(self.bozo_exception, SAXException)


class JsonFeedAttachment(BaseModel):
    url: str | None = None
    mime_type: str | None = None


class JsonFeedItem(BaseModel):
    # A posting of a JSON Feed. Its id may be written as a number, which a reader takes as text.
    model_config = ConfigDict(coerce_numbers_to_str=True)
    id: str | None = None
    url: str | None = None
    external_url: str | None = None
    title: str | None = None
    summary: str | None = None
    content_text: str | None = None
    content_html: str | None = None
    date_published: str | None = None
    date_modified: str | None = None
    attachments: list[JsonFeedAttachment] | None = None


class JsonFeed(BaseModel):
    # A JSON Feed, 1.0 or 1.1. Its postings are read one at a time, and no more than twice as
    # many as are kept.
    version: Annotated[str, StringConstraints(pattern=JSON_FEED_VERSION)]
    title: str | None = None
    home_page_url: str | None = None
    items: SkipValidation[JsonValue] = None


# A posting of a JSON Feed that is not written as the format has it is kept as the JSON it is,
# and left out, so that one malformed posting leaves the others readable.
JSON_FEED_ITEM = TypeAdapter(Annotated[JsonFeedItem | JsonValue, Field(union_mode="left_to_right")])


def declared_charset(declared: str) -> list[str]:
    header = Message()
    header[CONTENT_TYPE] = declared
    charset = header.get_content_charset()
    return [charset] if charset else []


def decoded(content: bytes, declared: str) -> str:
    # The text of a feed, in the encoding its byte order mark names, else the one its XML
    # declaration names, else the charset of the kind it was declared as, else UTF-8. A name
    # that Python knows no text encoding by names none. Bytes that the encoding cannot read
    # become replacement characters, and leave the rest of the feed readable.
    marked = [codec for mark, codec in BYTE_ORDER_MARKS if content.startswith(mark)][:1]
    in_declaration = XML_DECLARED_ENCODING.match(content)
    named = [
        *marked,
        *([in_declaration[1].decode()] if in_declaration else []),
        *declared_charset(declared),
    ]
    for encoding in named:
        try:
            codec = codecs.lookup(encoding).name
            text = content.decode(LARGER_ENCODINGS.get(codec, codec), errors="replace")
        except (LookupError, UnicodeError):
            continue
        return NOT_IN_XML.sub(WORD_GAP, text)
    return NOT_IN_XML.sub(WORD_GAP, content.decode(DEFAULT_ENCODING, errors="replace"))


def is_page(content: bytes, declared: str) -> bool:
    opening = decoded(content[:OPENING_CHARACTERS], declared)
    return bool(PAGE_OPENING.match(opening)) or (
        declared_media_type(declared) in PAGE_MEDIA_TYPES and not FEED_OPENING.match(opening)
    )


def readable_reference(reference: re.Match[str]) -> str:
    decimal, hexadecimal = reference.groups()
    digits = (decimal or hexadecimal).lstrip("0")
    base = DECIMAL if decimal else HEXADECIMAL
    code = int(digits or "0", base) if len(digits) <= MOST_CODE_POINT_DIGITS else BEYOND_UNICODE
    readable = code <= sys.maxunicode and not NOT_IN_XML.match(chr(code))
    return reference[0] if readable else WORD_GAP


def markup_text(markup: str) -> str:
    # The text that markup shows, read as a browser reads it: a fragment, a whole document, a
    # document that holds nothing, or nothing at all, which shows no text.
    readable = NUMBERED_REFERENCE.sub(readable_reference, NOT_IN_XML.sub(WORD_GAP, markup))
    page = etree.HTML(readable)
    if page is None:
        return ""
    for element in page.iter(*BREAKING_ELEMENTS):
        element.tail = WORD_GAP + (element.tail or "")
    return "".join(SHOWN_TEXT(page))


def plain_text(given: GivenText) -> str:
    # A text without markup and its white space collapsed: markup's text with its character
    # references resolved, plain text as it is written.
    match given.kind:
        case TextKind.MARKUP:
            text = markup_text(given.value)
        case TextKind.PLAIN:
            text = given.value
    return WORD_GAP.join(NOT_IN_XML.sub(WORD_GAP, text).split())


def cut(text: str, most: int) -> str:
    # A text cut short ends with an ellipsis. It is cut before a letter whose marks would be cut
    # from it: the marks that combine with a letter follow it.
    if len(text) <= most:
        return text
    end = most - 1
    while end > 0 and unicodedata.category(text[end]).startswith("M"):
        end -= 1
    return text[:end].rstrip() + TITLE_CUT_SHORT


def asked_as(written: str) -> str:
    # An address as a browser asks for it.
    taken = INNER_BREAKS.sub("", written.strip(OUTER_SPACE))
    return taken.replace(SPACE, WRITTEN_SPACE)


def resolved(address: str, base: str) -> list[str]:
    # An address resolved against a base. One that urllib cannot take apart, as one with a
    # bracket of an IPv6 address left open, resolves to none.
    try:
        return [urljoin(base, asked_as(address))]
    except ValueError:
        return []


def web_addresses(given: list[str], base: str) -> list[str]:
    # The web addresses among those given, resolved against a base; an empty one is none.
    return [
        address
        for written in given
        if written
        for address in resolved(written, base)
        if len(address) <= ADDRESS_CHARACTERS
        and re.fullmatch(WEB_ADDRESS_PATTERN, address)
        and not NOT_IN_XML.search(address)
    ]


def rfc822_zoned(written: str) -> str:
    # A time with its zone written as RFC 822 writes one where it is written otherwise.
    def gmt(found: re.Match[str]) -> str:
        sign, hours, minutes = found.groups()
        return f"{sign}{int(hours):02d}{minutes or '00'}"

    return GMT_OFFSET.sub(gmt, COLON_OFFSET.sub(r"\1\2\3", written.strip()))


def iso_moment(written: str) -> list[datetime]:
    try:
        moment = datetime.fromisoformat(written)
    except (ValueError, OverflowError):
        return []
    return [moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)]


def rfc822_moment(written: str) -> list[datetime]:
    text = rfc822_zoned(written)
    try:
        moment = parsedate_to_datetime(text)
    except (ValueError, OverflowError):
        return []
    if moment.tzinfo is not None:
        return [moment]
    return [moment.replace(tzinfo=UTC)] if NO_ZONE.search(text) else []


def moment_of(written: str) -> datetime | None:
    # A time that no reader can read, or that lies beyond the years Python counts, is none.
    return next(iter(iso_moment(written) or rfc822_moment(written)), None)


def title_of(listing: Listing, address: str) -> str:
    # A posting without a title is called by the start of the first of its summaries that
    # holds text, and one without either by its address.
    titled = cut(plain_text(listing.title), TITLE_CHARACTERS) if listing.title is not None else ""
    summarised = (
        cut(text, SUMMARY_TITLE_CHARACTERS) for text in map(plain_text, listing.summaries) if text
    )
    return titled or next(summarised, cut(address, TITLE_CHARACTERS))


def posting_of(listing: Listing, address: str) -> Posting:
    given = NOT_IN_XML.sub(WORD_GAP, listing.id).strip() or address
    key = given if len(given) <= KEY_CHARACTERS else hashlib.sha256(given.encode()).hexdigest()
    moments = (moment for moment in map(moment_of, listing.times) if moment is not None)
    return Posting(
        key=key, title=title_of(listing, address), address=address, published=next(moments, None)
    )


def feed_of(
    title: GivenText | None, sites: list[str], listings: Iterable[Listing], address: str
) -> Feed:
    # Addresses are resolved against the feed's own. A posting with no web address to read it
    # at is left out, and of two postings with one key the first is kept.
    postings: dict[str, Posting] = {}
    for listing in listings:
        addresses = web_addresses(listing.addresses, address)
        if not addresses:
            continue
        posting = posting_of(listing, addresses[0])
        postings.setdefault(posting.key, posting)
        if len(postings) == MOST_POSTINGS:
            break
    called = cut(plain_text(title), TITLE_CHARACTERS) if title is not None else ""
    return Feed(
        title=called or cut(urlsplit(address).hostname or address, TITLE_CHARACTERS),
        site=next(iter(web_addresses(sites, address)), None),
        postings=list(postings.values()),
    )


def given_text(parsed: ParsedText, version: str) -> GivenText:
    # A text of Atom is plain or markup as its type says; one of RSS carries markup by custom.
    plain = version.startswith(ATOM) and parsed.type not in MARKUP_TYPES
    return GivenText(value=parsed.value, kind=TextKind.PLAIN if plain else TextKind.MARKUP)


def listing_of_entry(entry: ParsedEntry, version: str) -> Listing:
    # A posting's recordings are the enclosures of RSS and Atom, its pages the alternate links
    # of Atom and the link of RSS, then a guid that is a permalink, which feedparser gives as
    # the link where the posting has no other: that is taken only where it is a web address
    # itself, and never resolved against the feed's.
    recordings = [
        link.href
        for link in entry.links
        if link.rel == ENCLOSURE and link.type.casefold().startswith(RECORDING_MEDIA_TYPES)
    ]
    pages = [link.href for link in entry.links if link.rel == ALTERNATE]
    from_guid = entry.link == entry.id
    linked = entry.link if not from_guid or entry.link.casefold().startswith(WEB_SCHEMES) else ""
    return Listing(
        id=entry.id,
        addresses=[*recordings, *pages, linked],
        title=given_text(entry.title_detail, version) if entry.title_detail is not None else None,
        summaries=[
            given_text(text, version) for text in [entry.summary_detail, *entry.content] if text
        ],
        times=[entry.published, entry.updated],
    )


def opening_after(text: str, opening: re.Pattern[str], most: int) -> int:
    # Where the element after the most of a kind that are read begins; the text's end where
    # there are no more.
    beyond = next(islice(opening.finditer(text), most, None), None)
    return len(text) if beyond is None else beyond.start()


def read_part(text: str) -> str:
    # The part of a feed that is read: up to the posting after those read, and up to the
    # element of a list after those read.
    end = min(
        opening_after(text, POSTING_OPENING, READ_POSTINGS),
        opening_after(text, LISTED_OPENING, READ_LISTED),
    )
    return text[:end]


def parser_keeping_ids(parser_class: Any) -> Any:
    # One of feedparser's parsers, but that it keeps an id as written, where it would resolve
    # one it takes for a permalink against the base, and resolves the address of an RSS
    # enclosure against the base in effect where it stands, as it does that of every link.
    def pop(parser: Any, element: str, strip_whitespace: int = 1) -> Any:
        if element != ID:
            return parser_class.pop(parser, element, strip_whitespace)
        permalink, parser.guidislink = parser.guidislink, False
        try:
            return parser_class.pop(parser, element, strip_whitespace)
        finally:
            parser.guidislink = permalink

    def start_enclosure(parser: Any, attributes: Any) -> None:
        parser_class._start_enclosure(parser, attributes)
        enclosure = parser._get_context()[LINKS][-1]
        if HREF in enclosure:
            enclosure[HREF] = parser.resolve_uri(enclosure[HREF])

    overrides = {"pop": pop, "_start_enclosure": start_enclosure}
    return type(f"{parser_class.__name__}KeepingIds", (parser_class,), overrides)


STRICT_PARSER = parser_keeping_ids(feedparser.api.StrictFeedParser)
LOOSE_PARSER = parser_keeping_ids(feedparser.api.LooseFeedParser)


def feedparser_result(data: bytes, base: str) -> Any:
    # What feedparser reads of a feed's bytes, driven as feedparser.parse drives it, with the
    # parsers above and the feed's own address as the base of its addresses: first by its
    # parser of XML, which reads no external entity, then, where the feed is not well-formed,
    # by its loose parser.
    held = feedparser.util.FeedParserDict
    result = held(bozo=False, entries=[], feed=held(), headers={CONTENT_TYPE: DECODED_FEED})
    data = feedparser.encodings.convert_to_utf8(result[HEADERS], data, result)
    result[VERSION], data, entities = feedparser.sanitizer.replace_doctype(data)
    parser = STRICT_PARSER(base, None, DEFAULT_ENCODING)
    parser.resolve_relative_uris = parser.sanitize_html = False
    reader = xml.sax.make_parser(feedparser.api.PREFERRED_XML_PARSERS)
    reader.setFeature(feature_namespaces, True)
    with suppress(SAXNotSupportedException):
        reader.setFeature(feature_external_ges, False)
    reader.setContentHandler(parser)
    reader.setErrorHandler(parser)
    source = InputSource()
    source.setByteStream(BytesIO(data))
    try:
        reader.parse(source)
    except SAXException as failure:
        result[BOZO] = True
        result[BOZO_EXCEPTION] = parser.exc or failure
        parser = LOOSE_PARSER(base, None, DEFAULT_ENCODING, entities)
        parser.resolve_relative_uris = parser.sanitize_html = False
        parser.feed(data.decode(DEFAULT_ENCODING, errors="replace"))
    result[FEED] = parser.feeddata
    result[ENTRIES] = parser.entries
    result[VERSION] = result[VERSION] or parser.version
    return result


def xml_feed(text: str, address: str) -> ParsedFeed:
    # feedparser reads RSS 0.9x, 1.0 and 2.0 and Atom, and reads on where a feed is not
    # well-formed. Where elements overlap, as in <item><link>x</item></link>, it closes an
    # element in a context that does not hold it, and fails with a KeyError or an IndexError of
    # its own, or a TypeError where it takes the text of an element it lost for a number.
    readable = LONG_REFERENCE.sub(
        readable_reference, ENTITY_DECLARATION.sub(ENTITY_REMOVED, read_part(text))
    )
    try:
        parsed = feedparser_result(readable.encode(), address)
    except (KeyError, IndexError, TypeError) as failure:
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=FOUND_TANGLED)) from failure
    return ParsedFeed.model_validate(parsed)


def rss_or_atom(parsed: ParsedFeed, address: str) -> Feed:
    # A posting is read from what feedparser holds of it, and never through its mapping of
    # names, which gives a posting's updated time as its published one where it has none and
    # warns that it will stop.
    channel = parsed.feed
    version = parsed.version
    entries = (ParsedEntry.model_validate(dict.copy(entry)) for entry in parsed.entries)
    return feed_of(
        title=given_text(channel.title_detail, version) if channel.title_detail else None,
        sites=[*[link.href for link in channel.links if link.rel == ALTERNATE], channel.link],
        listings=(listing_of_entry(entry, version) for entry in entries),
        address=address,
    )


def listing_of_item(item: JsonFeedItem) -> Listing:
    recordings = [
        attachment.url
        for attachment in item.attachments or []
        if attachment.url
        and (attachment.mime_type or "").casefold().startswith(RECORDING_MEDIA_TYPES)
    ]
    summaries = [
        (item.summary, TextKind.PLAIN),
        (item.content_text, TextKind.PLAIN),
        (item.content_html, TextKind.MARKUP),
    ]
    return Listing(
        id=item.id or "",
        addresses=[*recordings, item.url or "", item.external_url or ""],
        title=GivenText(value=item.title, kind=TextKind.PLAIN) if item.title else None,
        summaries=[GivenText(value=value, kind=kind) for value, kind in summaries if value],
        times=[written for written in [item.date_published, item.date_modified] if written],
    )


def whole_surrogates(escape: re.Match[str]) -> str:
    backslashes, surrogates = escape.groups()
    whole = len(surrogates) == WHOLE_PAIR_ESCAPE_CHARACTERS
    return escape[0] if whole else backslashes + REPLACEMENT_ESCAPE


def json_feed(text: str, address: str) -> Feed:
    try:
        written = json.loads(SURROGATE_ESCAPE.sub(whole_surrogates, text), parse_int=str)
        document = JsonFeed.model_validate(written)
    except (ValueError, RecursionError) as failure:
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=FOUND_JSON)) from failure
    listed = document.items[:READ_POSTINGS] if isinstance(document.items, list) else []
    read = (JSON_FEED_ITEM.validate_python(item) for item in listed)
    return feed_of(
        title=GivenText(value=document.title, kind=TextKind.PLAIN) if document.title else None,
        sites=[document.home_page_url or ""],
        listings=(listing_of_item(item) for item in read if isinstance(item, JsonFeedItem)),
        address=address,
    )


def read_feed(content: bytes, declared: str, address: str) -> Feed:
    # The feed that the bytes served at an address hold, declared as the kind given (the
    # Content-Type of the answer, or nothing).
    text = decoded(content, declared)
    if not text.strip():
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=FOUND_NOTHING))
    if text.lstrip().startswith(JSON_OPENINGS):
        return json_feed(text, address)
    if is_page(content, declared):
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=FOUND_PAGE))
    parsed = xml_feed(text, address)
    # A feed that is not well-formed is still read as far as feedparser reads it: it is a feed
    # where feedparser read a title or a posting of it. One of which it read neither, as one
    # that breaks off before them, is no feed, so that its poll fails and is made again, and
    # the subscription keeps what it read last. A well-formed feed is a feed where feedparser
    # recognised its format, postings or none. What else the bytes hold is told by the kind of
    # file they look like, where they look like one.
    read = parsed.entries or parsed.feed.title_detail
    if parsed.version and parsed.is_broken() and not read:
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=FOUND_BROKEN))
    if not (parsed.version or read):
        kind = filetype.guess(content)
        written = FOUND_XML if text.lstrip().startswith(XML_OPENING) else FOUND_TEXT
        found = FOUND_FILE.format(kind=kind.mime) if kind is not None else written
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=found))
    return rss_or_atom(parsed, address)
