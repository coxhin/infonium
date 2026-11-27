import codecs
import hashlib
import html
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from email.message import Message
from email.utils import parsedate_to_datetime
from enum import StrEnum
from io import BytesIO
from itertools import islice
from typing import Annotated
from urllib.parse import urljoin, urlsplit

import feedparser
import filetype
from lxml.html import defs, fragment_fromstring
from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    SkipValidation,
    StringConstraints,
    ValidationError,
)

from infonium.document import WORD_GAP
from infonium.htmlpage import SHOWN_TEXT
from infonium.intake import (
    CONTENT_TYPE,
    PAGE_MEDIA_TYPES,
    RECORDING_MEDIA_TYPES,
    declared_media_type,
)
from infonium.reasons import CUT_SHORT

# The most characters kept of a posting's key, of a title, and of an address. A feed is another
# site's text: what goes beyond is cut, replaced or left out, as read_feed says.
KEY_CHARACTERS = 200
TITLE_CHARACTERS = 300
ADDRESS_CHARACTERS = 2000
WEB_ADDRESS_PATTERN = r"^(?i:https?)://\S+$"

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
# around it cannot join into another.
ENTITY_DECLARATION = re.compile(r"<!ENTITY[^>]*>", re.IGNORECASE)
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
# feedparser is told that the feed is UTF-8, the encoding read_feed gives it the feed in. It is
# not told where the feed was served: it would resolve a guid against that address as well as
# the feed's addresses, and a posting's key would change with the address of its feed. It
# resolves against the base the feed gives, and read_feed resolves the rest.
DECODED_FEED = "application/xml; charset=utf-8"

# What a web page opens with, after an XML declaration and comments, and what a feed opens
# with. A page declared as one but opening as a feed is taken for a feed, as some servers
# declare their feeds as pages.
OPENING_CHARACTERS = 4096
PAGE_OPENING = re.compile(
    r"\s*(?:<\?xml[^>]*>\s*)?(?:<!--.*?-->\s*)*<(?:!doctype\s+html|html)\b",
    re.IGNORECASE | re.DOTALL,
)
FEED_OPENING = re.compile(r"\s*(?:<\?xml|<rss|<feed|<rdf:rdf|\{)", re.IGNORECASE)
JSON_OPENINGS = ("{", "[")
XML_OPENING = "<"

# What a reader is told of bytes that hold no feed: what they hold instead, and what to give.
GIVE_A_FEED = "give the address of a feed, or a route of RSSHub, instead"
NOT_A_FEED = "{address} is not a feed but {found}; " + GIVE_A_FEED
FOUND_NOTHING = "an empty answer"
FOUND_PAGE = "a web page"
FOUND_JSON = "JSON that is not a JSON Feed"
FOUND_XML = "XML that is not RSS or Atom"
FOUND_FILE = "a file of the kind {kind}"
FOUND_TEXT = "text that is not RSS, Atom or a JSON Feed"
FOUND_TANGLED = "XML whose elements overlap too much to read it as RSS or Atom"

# The relations of a link that feedparser reports: the page of a posting or of a feed, and a
# file it carries. A link without a relation is an alternate one, which feedparser says.
ALTERNATE = "alternate"
ENCLOSURE = "enclosure"
PLAIN_TEXT = "text/plain"
MARKUP_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# The elements after which the text of markup goes on as a new word.
BREAKING_ELEMENTS = tuple(
    defs.block_tags
    | {"br", "article", "aside", "figcaption", "figure", "footer", "header", "main", "section"}
)

JSON_FEED_VERSION = r"^https?://jsonfeed\.org/version/"
# pydantic's reader of JSON refuses a whole document that writes half a pair of surrogates as
# an escape, which a feed whose writer cut a text inside an emoji writes. Such an escape is
# read as the replacement character. An escape is one where the backslashes before the u are
# odd in number, the others standing for themselves; a whole pair is kept.
SURROGATE_ESCAPE = re.compile(
    r"(?<!\\)((?:\\\\)*)\\u([dD][89abAB][0-9a-fA-F]{2}(?:\\u[dD][c-fC-F][0-9a-fA-F]{2})?"
    r"|[dD][c-fC-F][0-9a-fA-F]{2})"
)
WHOLE_PAIR_ESCAPE_CHARACTERS = 10
REPLACEMENT_ESCAPE = "\\ufffd"

# How a time is read: as RFC 3339 and ISO 8601 write it, then as RFC 822 does, in the zone it
# is written in. feedparser's own reading of the other ways feeds write a time is not used: it
# fills in a year, a month or a day that a time leaves out from the clock, and 71 real feeds
# wrote no time that only it could read.
TIME_READERS = (datetime.fromisoformat, parsedate_to_datetime)


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
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


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
    # neither. Its postings are read one at a time, as far as they are kept.
    version: ParsedString = ""
    feed: ParsedChannel
    entries: SkipValidation[list[object]]


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
    # A JSON Feed, 1.0 or 1.1. A posting that is not written as the format has it is kept as
    # the JSON it is, and left out, so that one malformed posting leaves the others readable.
    version: Annotated[str, StringConstraints(pattern=JSON_FEED_VERSION)]
    title: str | None = None
    home_page_url: str | None = None
    items: list[Annotated[JsonFeedItem | JsonValue, Field(union_mode="left_to_right")]] = []


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
    readable = NUMBERED_REFERENCE.sub(readable_reference, NOT_IN_XML.sub(WORD_GAP, markup))
    fragment = fragment_fromstring(readable, create_parent=True)
    for element in fragment.iter(*BREAKING_ELEMENTS):
        element.tail = WORD_GAP + (element.tail or "")
    return "".join(SHOWN_TEXT(fragment))


def plain_text(given: GivenText) -> str:
    # A text without markup, its character references resolved and its white space collapsed.
    match given.kind:
        case TextKind.MARKUP:
            text = markup_text(given.value)
        case TextKind.PLAIN:
            text = html.unescape(LONG_REFERENCE.sub(readable_reference, given.value))
    return WORD_GAP.join(NOT_IN_XML.sub(WORD_GAP, text).split())


def cut(text: str, most: int) -> str:
    return text if len(text) <= most else text[: most - 1].rstrip() + CUT_SHORT


def resolved(address: str, base: str) -> list[str]:
    # An address resolved against a base. One that urllib cannot take apart, as one with a
    # bracket of an IPv6 address left open, resolves to none.
    try:
        return [urljoin(base, address)]
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


def moment_of(written: str) -> datetime | None:
    # A time that no reader can read, or that lies beyond the years Python counts, is none; one
    # written without a zone is in UTC.
    for reader in TIME_READERS:
        try:
            moment = reader(written)
        except (ValueError, OverflowError):
            continue
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return None


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


def given_text(parsed: ParsedText) -> GivenText:
    kind = TextKind.MARKUP if parsed.type in MARKUP_TYPES else TextKind.PLAIN
    return GivenText(value=parsed.value, kind=kind)


def listing_of_entry(entry: ParsedEntry) -> Listing:
    # A posting's recordings are the enclosures of RSS and Atom, its pages the alternate links
    # of Atom and the link of RSS, then a guid that is a permalink, which feedparser gives as
    # the link where the posting has no other.
    recordings = [
        link.href
        for link in entry.links
        if link.rel == ENCLOSURE and link.type.casefold().startswith(RECORDING_MEDIA_TYPES)
    ]
    pages = [link.href for link in entry.links if link.rel == ALTERNATE]
    return Listing(
        id=entry.id,
        addresses=[*recordings, *pages, entry.link],
        title=given_text(entry.title_detail) if entry.title_detail is not None else None,
        summaries=[given_text(text) for text in [entry.summary_detail, *entry.content] if text],
        times=[entry.published, entry.updated],
    )


def first_postings(text: str) -> str:
    beyond = next(islice(POSTING_OPENING.finditer(text), READ_POSTINGS, None), None)
    return text if beyond is None else text[: beyond.start()]


def xml_feed(text: str, address: str) -> ParsedFeed:
    # feedparser reads RSS 0.9x, 1.0 and 2.0 and Atom, and reads on where a feed is not
    # well-formed. It is handed a stream, as a string would be taken for a file's name or an
    # address to fetch. Where elements overlap, as in <item><link>x</item></link>, it closes an
    # element in a context that does not hold it, and fails with a KeyError or an IndexError of
    # its own, or a TypeError where it takes the text of an element it lost for a number.
    try:
        parsed = feedparser.parse(
            BytesIO(
                LONG_REFERENCE.sub(
                    readable_reference, ENTITY_DECLARATION.sub(ENTITY_REMOVED, first_postings(text))
                ).encode()
            ),
            response_headers={CONTENT_TYPE: DECODED_FEED},
            resolve_relative_uris=False,
            sanitize_html=False,
        )
    except (KeyError, IndexError, TypeError) as failure:
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=FOUND_TANGLED)) from failure
    return ParsedFeed.model_validate(parsed)


def rss_or_atom(parsed: ParsedFeed, address: str) -> Feed:
    channel = parsed.feed
    return feed_of(
        title=given_text(channel.title_detail) if channel.title_detail is not None else None,
        sites=[*[link.href for link in channel.links if link.rel == ALTERNATE], channel.link],
        listings=(listing_of_entry(ParsedEntry.model_validate(entry)) for entry in parsed.entries),
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
        document = JsonFeed.model_validate_json(SURROGATE_ESCAPE.sub(whole_surrogates, text))
    except ValidationError as failure:
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=FOUND_JSON)) from failure
    return feed_of(
        title=GivenText(value=document.title, kind=TextKind.PLAIN) if document.title else None,
        sites=[document.home_page_url or ""],
        listings=(
            listing_of_item(item) for item in document.items if isinstance(item, JsonFeedItem)
        ),
        address=address,
    )


def read_feed(content: bytes, declared: str, address: str) -> Feed:
    # The feed that the bytes served at an address hold, declared as the kind given (the
    # Content-Type of the answer, or nothing).
    text = decoded(content, declared)
    if not text.strip():
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=FOUND_NOTHING))
    kind = filetype.guess(content)
    if kind is not None:
        found = FOUND_FILE.format(kind=kind.mime)
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=found))
    if text.lstrip().startswith(JSON_OPENINGS):
        return json_feed(text, address)
    if is_page(content, declared):
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=FOUND_PAGE))
    parsed = xml_feed(text, address)
    # A feed that is not well-formed is still read as far as feedparser reads it: it is a feed
    # where feedparser recognised its format, or read a title or a posting of it.
    if not (parsed.version or parsed.entries or parsed.feed.title_detail):
        found = FOUND_XML if text.lstrip().startswith(XML_OPENING) else FOUND_TEXT
        raise UnreadableFeed(NOT_A_FEED.format(address=address, found=found))
    return rss_or_atom(parsed, address)
