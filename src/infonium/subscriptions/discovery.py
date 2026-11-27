import re
from collections.abc import Callable, Mapping
from http import HTTPStatus
from typing import Annotated
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
import trafilatura
from pydantic import BaseModel, Field, StringConstraints, ValidationError

from infonium.document import (
    PAGE_MEDIA_TYPES,
    STATUS_NAMES,
    WORD_GAP,
    declared_media_type,
    error_status,
    error_text,
)
from infonium.subscriptions.feed import (
    ADDRESS_CHARACTERS,
    ALTERNATE,
    TITLE_CUT_SHORT,
    UnreadableFeed,
    WebAddress,
    asked_as,
    is_page,
    web_addresses,
)
from infonium.subscriptions.fetcher import (
    FEED_LIMIT_BYTES,
    FEED_LIMIT_SECONDS,
    MOST_REDIRECTS,
    NOTHING_HIDDEN,
    REFUSAL_LIMIT_BYTES,
    Allowance,
    Answer,
    Changed,
    Fetched,
    LedAway,
    Limit,
    Oversized,
    Unchanged,
    Validators,
    answer_within,
    changed,
    ends_in,
    feed_limit,
    unreached,
)
from infonium.subscriptions.radar import (
    RADAR_RULES_PATH,
    HeldRules,
    RadarRoute,
    held_rules,
    routes_for,
)

# How an address begins that names a route of RSSHub, a bridge that makes feeds of sites
# without one; the route follows. It is told in any case, and kept written as here: a feed of
# RSSHub is kept as its route, and asked at the instance that INFONIUM_RSSHUB_BASE_URL names
# when it is asked, so that the instance may move.
RSSHUB_SCHEME = "rsshub://"
ROUTE_APART = "/"
ROUTE_QUERY = "?"
QUERY_APART = "&"
ROUTE_FRAGMENT = "#"
ROUTE_ADDRESS_PATTERN = rf"^{re.escape(RSSHUB_SCHEME)}\S+$"
RouteAddress = Annotated[
    str, StringConstraints(pattern=ROUTE_ADDRESS_PATTERN, max_length=ADDRESS_CHARACTERS)
]
# A route is written one way, so that one route is one address and one subscription however a
# reader wrote it, or a rule of RSSHub gave it: no fragment, no slash at its start or two in a
# row, and each character as itself where it may stand so, an escape otherwise. White space and
# control characters are escapes, and so is a character that would say what it does not: a
# slash within a part of the path, &, =, + or # within a query, and the percent sign.
ESCAPE_OR_CHARACTER = re.compile(r"((?:%[0-9A-Fa-f]{2})+)|(.)", re.DOTALL)
ESCAPE = "%{:02X}"
ESCAPED_IN_PATH = frozenset("/?#%")
ESCAPED_IN_QUERY = frozenset("&=+#%")
# Where an address leads to no feed by the other rules, the route that RSSHub's own rules give
# for it is tried. The rules of the instance are fetched by each discovery that comes to them,
# as one runs in a process of its own, with one request and no redirect, besides the
# discovery's: 1 MiB, fetched and made ready in 0.2 s. They may take 30 s of what is left of the
# discovery's minute, and hold 8 MiB, which the fetcher makes ready within its memory: beside a
# page of 5 MiB, 8.7 MiB of rules shaped as RSSHub's took 3.4 s and 232 MiB, and 8 MiB of short
# rules of one site 5.9 s and 390 MiB. Rules that hold more are told as RSSHub's. An instance
# that does not answer costs a discovery one refused connection.
RULES_ADDRESS = RSSHUB_SCHEME + RADAR_RULES_PATH.removeprefix(ROUTE_APART)
RULES_LIMIT_BYTES = 8 * 1024**2
RULES_LIMIT_SECONDS = 30.0
# What a request to RSSHub carries of the key the instance asks for, where it is given one:
# RSSHub reads it from the query, and holds its rules to it as well as its routes.
KEY_PARAMETER = "key"
# Where RSSHub's page of an error says what went wrong, and the kind of error it names for a
# request without its key.
RSSHUB_MESSAGE = "(//p[@class='message'])[1]/code//text()"
FULL_STOP = "."
KEY_ERROR = "RejectError"
# Where several of RSSHub's rules match an address equally well, the reader is told their
# routes, this many at most: a Bilibili space is matched by nine alike, and the one of a
# user's videos is the ninth of them.
MOST_ROUTES_TOLD = 12

# A page is fetched as a feed is, within less; one discovery within a minute and eight
# requests, each redirect among them. A request is not made once either is spent, and the
# exchange under way is given up when the minute is. Of the requests, one is kept for a route
# of RSSHub where nothing else leads to a feed.
PAGE_LIMIT_BYTES = 5 * 1024**2
PAGE_LIMIT_SECONDS = 30.0
DISCOVERY_LIMIT_SECONDS = 60.0
MOST_REQUESTS = 8
KEPT_FOR_RSSHUB = 1

# The feeds a page names by links of its head, of which the first few are tried in the order
# the page names them. A link is taken wherever it stands, as a page that is not well-formed
# may leave its head's links in its body. A link's relations and its kind are read in any case,
# and the kind without its parameters.
NAMED_FEEDS_TRIED = 3
LINKS = "//link[@href]"
BASES = "//base/@href"
RELATIONS = "rel"
KIND = "type"
REFERENCE = "href"
FEED_KINDS = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/feed+json", "application/json"}
)

# Where none of the feeds a page names reads, as where it names none, the feed of its site is
# looked for where feeds commonly are. Of the pages of 10 sites that named none, this found the
# feeds of 3 (danluu.com at /atom.xml, sspai.com at /feed, stratechery.com at /feed), and none
# of paulgraham.com, arxiv.org, reddit.com, bbc.com, 36kr.com, xiaoyuzhoufm.com and
# space.bilibili.com.
COMMON_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml")

# YouTube keeps the feed of a channel and of a playlist at an address of its own, by the
# channel's id or the playlist's. A channel named by its handle or its old names is found
# through its page, which names the feed or carries the channel's id in its canonical link.
# The places where a site's feed commonly is are not tried on YouTube.
YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com"})
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?{query}"
CHANNEL_ID = "channel_id"
PLAYLIST_ID = "playlist_id"
PLAYLIST = "list"
CHANNEL_SHAPE = r"UC[0-9A-Za-z_-]{22}"
CHANNEL_PATH = re.compile(rf"/channel/({CHANNEL_SHAPE})(?:[/?#]|$)")
CANONICAL = "//link[@rel='canonical']/@href"
# Where YouTube asks a person in some regions to agree to its use of cookies, in place of the
# page asked for, before it shows any page.
YOUTUBE_CONSENT_HOST = "consent.youtube.com"
# There, a channel named by its handle or an older name (/@…, /c/…, /user/…) is found through
# yt-dlp, which passes that page by its own means. It is asked for the channel's id by its
# extractor of YouTube's channels alone, which reads one page of the channel and lists none of
# its videos (no entry of the playlist it makes of them is asked for); it keeps no cache folder,
# and gives up a request or makes it again as it does in fetching a media page. Its requests are
# its own, outside the discovery's; its time and memory are the fetcher's. A channel so found
# took 1.0 to 1.4 s, the fetcher holding 90 to 96 MiB at its peak; a handle YouTube does not
# know, 1.4 to 1.7 s and eight requests of yt-dlp's, each failed one made again three times.
NAMED_CHANNEL_PATH = re.compile(r"/(?:@|c/|user/)[^/]+")
CHANNEL_EXTRACTOR = "youtube:tab"
NO_ENTRY = "0"
YTDLP_ERROR = "ERROR:"

# A show on Apple Podcasts keeps its feed where its publisher serves it, which Apple's lookup
# names by the show's number.
APPLE_PODCASTS_HOST = "podcasts.apple.com"
APPLE_SHOW = re.compile(r"/id(\d+)(?:[/?#]|$)")
APPLE_LOOKUP = "https://itunes.apple.com/lookup?id={show}&entity=podcast"

# What a reader is told of an address that leads to no feed. Every such sentence ends with what
# the reader may give instead, written as they would type it.
GIVE_A_FEED = (
    "give the address of a feed, or a route of RSSHub as " + RSSHUB_SCHEME + "<route>, instead"
)
WITH_ADVICE = "{sentence}; " + GIVE_A_FEED
# Where a fault of the instance kept its rules from being asked, the reader is told it after why
# the address leads to no feed, and is advised a feed alone: a route would meet the same fault.
NO_FEED_NOR_ROUTE = (
    "{refusal}; give the address of a feed instead. No route of RSSHub was looked for: {fault}"
)
NOT_AN_ADDRESS = "{address} is neither a web address nor a route of RSSHub"
NAMES_NO_FEED = (
    "{address} is a web page that names no feed, and none was found where its site would keep one"
)
# Of named feeds that cannot be read, the reason the first could not is told, as the feed a
# page names first is the one it gives as its own.
NAMED_FEEDS_UNREADABLE = (
    "{address} names feeds that cannot be read (the first: {reason}), and none was found where"
    " its site would keep one"
)
UNKEPT_ADDRESS = (
    "{address} leads on to an address of more than {most} characters, which infonium does not keep"
)
# Of an address too long to keep, its start is told.
SHOWN_OF_LONG_ADDRESS = 100
LONG_ADDRESS = "{address} is longer than the {most} characters infonium keeps of an address"
ROUTE_REFUSED = "RSSHub at {instance} answered the route {route} with {said}"
SAID = "{status}: {message}"
MANY_ROUTES = (
    "RSSHub has {count} routes for {address}, none of which matches it better than the"
    " others: {routes}{rest}"
)
TOLD_ROUTE = "{address} ({title})"
ROUTES_APART = ", "
MORE_ROUTES = ", and {more} more"
# What a reader is told of the instance itself, or of infonium's settings for it, which no
# other address would mend.
SILENT_RSSHUB = (
    "RSSHub did not answer at {instance} ({reason}); INFONIUM_RSSHUB_BASE_URL names the"
    " instance, and engines/rsshub starts one"
)
FAILED = "{kind}: {said}"
# An instance that no connection reaches, or whose address names no web address, does not
# answer; one that is reached but does not finish its answer fails the route.
NOT_REACHED = (httpx.ConnectError, httpx.ConnectTimeout, httpx.UnsupportedProtocol)
ROUTE_BROKEN_OFF = "RSSHub at {instance} did not finish answering the route {route} ({reason})"
KEY_REFUSED = "RSSHub at {instance} refused the key that INFONIUM_RSSHUB_ACCESS_KEY gives it"
KEY_MISSING = "RSSHub at {instance} asks for a key, and INFONIUM_RSSHUB_ACCESS_KEY gives none"
# The key goes to the instance alone: where the instance redirects to another server and writes
# the key into that address, the redirect is not followed, and the reader is told where it leads.
KEY_KEPT = "{said}; INFONIUM_RSSHUB_BASE_URL names the instance the key is for"
RULES_OUTGROWN = "RSSHub's rules at {instance} hold more than infonium reads"
NO_APPLE_FEED = "Apple Podcasts names no feed of the show at {address}"
YOUTUBE_CONSENT = (
    "YouTube answered {address} with its page that asks for consent to cookies, which names no"
    " feed; a channel is found by its address with its handle or its id, as"
    " https://www.youtube.com/@… or https://www.youtube.com/channel/UC…"
)
# What yt-dlp said is told after the sentence, where it said something.
NO_CHANNEL = "YouTube names no channel at {address}"
TOLD_WITH = "{sentence}: {said}"


class RSSHubFault(UnreadableFeed):
    # What went wrong with the RSSHub instance, or with infonium's settings for it: told as it
    # is, without the advice to give another address.
    pass


class SilentRSSHub(RSSHubFault):
    pass


class Discovered(BaseModel):
    # The feed an address leads to: where it is served, a web address or a route of RSSHub,
    # and the feed as it was fetched there in finding it.
    address: WebAddress | RouteAddress
    fetched: Changed


class ApplePodcastsShow(BaseModel):
    feed: str | None = Field(default=None, alias="feedUrl")


class ApplePodcastsLookup(BaseModel):
    results: list[ApplePodcastsShow] = []


class NamedChannel(BaseModel):
    # What yt-dlp reports of a channel, under yt-dlp's name: its id.
    channel_id: Annotated[str, StringConstraints(pattern=rf"^{CHANNEL_SHAPE}$")]


def with_advice(sentence: str) -> str:
    return WITH_ADVICE.format(sentence=sentence)


def is_route(address: str) -> bool:
    return address[: len(RSSHUB_SCHEME)].casefold() == RSSHUB_SCHEME


def escaped(character: str) -> str:
    return "".join(ESCAPE.format(byte) for byte in character.encode(errors="surrogateescape"))


def written_one_way(part: str, escaped_here: frozenset[str]) -> str:
    # A part of a route with each of its characters written one way. Escapes are read as UTF-8,
    # and bytes that are not are kept as escapes.
    def one_way(found: re.Match[str]) -> str:
        escapes, character = found.groups()
        if character is not None:
            stands = character.isprintable() and not character.isspace() and character != "%"
            return character if stands else escaped(character)
        read = bytes.fromhex(escapes.replace("%", "")).decode(errors="surrogateescape")
        return "".join(
            written
            if written.isprintable() and not written.isspace() and written not in escaped_here
            else escaped(written)
            for written in read
        )

    return ESCAPE_OR_CHARACTER.sub(one_way, part)


def route_path(route: str) -> str:
    # A route written one way, as a browser would ask for it, with one slash before each part
    # of its path however many it was written with, and without a fragment.
    path, _, query = asked_as(route).partition(ROUTE_FRAGMENT)[0].partition(ROUTE_QUERY)
    parts = [written_one_way(part, ESCAPED_IN_PATH) for part in path.split(ROUTE_APART) if part]
    asked = [
        written_one_way(piece, ESCAPED_IN_QUERY) for piece in query.split(QUERY_APART) if piece
    ]
    written = ROUTE_APART + ROUTE_APART.join(parts)
    return written + ROUTE_QUERY + QUERY_APART.join(asked) if asked else written


def route_of(address: str) -> str:
    return route_path(address[len(RSSHUB_SCHEME) :])


def route_address(route: str) -> str:
    # The address a route is kept under.
    return RSSHUB_SCHEME + route_path(route).removeprefix(ROUTE_APART)


def served_at(address: str, rsshub: str) -> str:
    # The web address a feed's address stands for: a route at the RSSHub instance given, a web
    # address as it is. The discovery and the poll of a feed both ask for the feed here.
    if is_route(address):
        return rsshub.rstrip(ROUTE_APART) + route_of(address)
    return address


def rsshub_query(key: str | None) -> Mapping[str, str]:
    # What every request to the instance carries of its key, which no address shows.
    return {KEY_PARAMETER: key} if key else NOTHING_HIDDEN


def page_limit(status: int, declared: str) -> Limit:
    # A page is held to less than a feed, and of an error only what it may say is read.
    if status != HTTPStatus.OK:
        return Limit(most_bytes=REFUSAL_LIMIT_BYTES, most_seconds=FEED_LIMIT_SECONDS)
    if declared_media_type(declared) in PAGE_MEDIA_TYPES:
        return Limit(most_bytes=PAGE_LIMIT_BYTES, most_seconds=PAGE_LIMIT_SECONDS)
    return Limit(most_bytes=FEED_LIMIT_BYTES, most_seconds=FEED_LIMIT_SECONDS)


def fetched_answer(address: str, allowance: Allowance) -> Answer:
    # What an address answers with, held to a page's bounds where it declares a page and to a
    # feed's otherwise, and to what the discovery may still do.
    answer = answer_within(address, Validators(), allowance, page_limit)
    if answer.status != HTTPStatus.OK:
        raise UnreadableFeed(error_status(answer.address, answer.status))
    return answer


def discovered_of(answer: Answer, asked: str, kept: str) -> Discovered:
    # The feed an answer holds, kept under the address given, where infonium keeps an address
    # so long.
    if len(kept) > ADDRESS_CHARACTERS:
        raise UnreadableFeed(UNKEPT_ADDRESS.format(address=asked, most=ADDRESS_CHARACTERS))
    return Discovered(address=kept, fetched=changed(answer))


def feed_at(address: str, allowance: Allowance) -> Discovered:
    answer = fetched_answer(address, allowance)
    return discovered_of(answer, address, answer.address)


def named_feeds(answer: Answer) -> list[str]:
    # The feeds a page names, in its order, each once, resolved against the page's base and
    # its address.
    page = trafilatura.load_html(answer.content)
    if page is None:
        return []
    base = web_addresses([str(based).strip() for based in page.xpath(BASES)][:1], answer.address)
    named = [
        str(link.get(REFERENCE)).strip()
        for link in page.xpath(LINKS)
        if ALTERNATE in str(link.get(RELATIONS, "")).casefold().split()
        and declared_media_type(str(link.get(KIND, ""))) in FEED_KINDS
    ]
    return list(dict.fromkeys(web_addresses(named, (base or [answer.address])[0])))


def channel_feed(channel: str) -> str:
    # The feed YouTube keeps of the channel whose id is given.
    return YOUTUBE_FEED.format(query=urlencode({CHANNEL_ID: channel}))


def channel_feeds(answer: Answer) -> list[str]:
    # The feed of the YouTube channel whose page is the one answered, by the id in its
    # canonical link.
    page = trafilatura.load_html(answer.content)
    canonical = [str(address) for address in page.xpath(CANONICAL)] if page is not None else []
    channels = [found[1] for address in canonical if (found := CHANNEL_PATH.search(address))]
    return [channel_feed(channel) for channel in channels]


def is_named_channel(address: str) -> bool:
    # Whether an address is of a YouTube channel named by its handle or an older name.
    parts = urlsplit(address)
    return parts.hostname in YOUTUBE_HOSTS and bool(NAMED_CHANNEL_PATH.match(parts.path))


def named_channel(address: str) -> str:
    # The id of the channel that an address names by its handle or an older name, as yt-dlp
    # finds it. Where yt-dlp fails, or what it reports holds no id of a channel's shape, the
    # address is refused, with what yt-dlp said on one line, without its prefix and colours.
    # Loaded here, as loading yt-dlp at the fetcher's start would cost every poll 0.09 s.
    import yt_dlp.utils

    from infonium.media import REQUEST_RETRIES, SILENCE_LIMIT_SECONDS, UNCOLOURED

    refused = NO_CHANNEL.format(address=address)
    try:
        with yt_dlp.YoutubeDL(
            # yt-dlp takes False for no cache folder, where its stubs allow only a folder's path.
            {  # type: ignore[arg-type]
                "allowed_extractors": [re.escape(CHANNEL_EXTRACTOR)],
                "playlist_items": NO_ENTRY,
                "cachedir": False,
                "socket_timeout": SILENCE_LIMIT_SECONDS,
                "extractor_retries": REQUEST_RETRIES,
                "color": {"stderr": UNCOLOURED, "stdout": UNCOLOURED},
                "quiet": True,
            }
        ) as finder:
            found = finder.extract_info(address, download=False)
    except yt_dlp.utils.DownloadError as failure:
        uncoloured = yt_dlp.utils.remove_terminal_sequences(str(failure)).strip()
        said = WORD_GAP.join(uncoloured.removeprefix(YTDLP_ERROR).split())
        told = TOLD_WITH.format(sentence=refused, said=said) if said else refused
        raise UnreadableFeed(told) from failure
    try:
        return NamedChannel.model_validate(found).channel_id
    except ValidationError as failure:
        raise UnreadableFeed(refused) from failure


def discovered_at(address: str, allowance: Allowance) -> Discovered:
    # The feed itself, or the first that reads of the feeds a page names and then of the places
    # where its site would keep one: on YouTube the feed of the channel whose id the page
    # carries, elsewhere the places where feeds commonly are. A feed that cannot be fetched or
    # read is passed over for the next. Where YouTube answers with its page that asks for
    # consent, the feed is that of the channel the address names by its handle or an older
    # name, found through yt-dlp; any other address is refused there.
    answer = fetched_answer(address, allowance)
    host = urlsplit(answer.address).hostname
    if host == YOUTUBE_CONSENT_HOST and is_named_channel(address):
        return feed_at(channel_feed(named_channel(address)), allowance)
    if host == YOUTUBE_CONSENT_HOST:
        raise UnreadableFeed(YOUTUBE_CONSENT.format(address=address))
    if not is_page(answer.content, answer.declared):
        return discovered_of(answer, address, answer.address)
    named = named_feeds(answer)[:NAMED_FEEDS_TRIED]
    elsewhere = (
        channel_feeds(answer)
        if host in YOUTUBE_HOSTS
        else [urljoin(answer.address, path) for path in COMMON_FEED_PATHS]
    )
    reasons: list[str] = []
    for feed in allowance.left([*named, *elsewhere]):
        try:
            return feed_at(feed, allowance)
        except UnreadableFeed as refusal:
            reasons.append(str(refusal))
        except (httpx.HTTPError, httpx.InvalidURL) as failure:
            reasons.append(unreached(feed, failure))
    if named:
        raise UnreadableFeed(NAMED_FEEDS_UNREADABLE.format(address=address, reason=reasons[0]))
    raise UnreadableFeed(NAMES_NO_FEED.format(address=address))


def youtube_feed(address: str) -> list[str]:
    # The feed YouTube keeps of a playlist or a channel that an address names by its id.
    parts = urlsplit(address)
    playlists = parse_qs(parts.query).get(PLAYLIST, [])
    channel = CHANNEL_PATH.match(parts.path)
    if playlists:
        return [YOUTUBE_FEED.format(query=urlencode({PLAYLIST_ID: playlists[0]}))]
    if channel:
        return [channel_feed(channel[1])]
    return []


def apple_podcasts_feed(address: str, show: str, allowance: Allowance) -> Discovered:
    lookup = fetched_answer(APPLE_LOOKUP.format(show=show), allowance)
    try:
        shows = ApplePodcastsLookup.model_validate_json(lookup.content).results
    except ValidationError as failure:
        raise UnreadableFeed(NO_APPLE_FEED.format(address=address)) from failure
    feeds = web_addresses([show.feed for show in shows if show.feed], lookup.address)
    if not feeds:
        raise UnreadableFeed(NO_APPLE_FEED.format(address=address))
    return feed_at(feeds[0], allowance)


def rsshub_message(page: bytes) -> str:
    # What RSSHub's page of an error says went wrong, where it says it, as a clause.
    tree = trafilatura.load_html(page) if page else None
    message = "".join(tree.xpath(RSSHUB_MESSAGE)) if tree is not None else ""
    return WORD_GAP.join(message.split()).removesuffix(FULL_STOP)


def said_by_rsshub(status: int, page: bytes) -> str:
    # What RSSHub answered a route with: the status, and what its page of the error says.
    named = STATUS_NAMES.get(status, str(status))
    said = rsshub_message(page)
    return SAID.format(status=named, message=said) if said else named


def asked_of_rsshub(
    address: str,
    rsshub: str,
    key: str | None,
    validators: Validators,
    allowance: Allowance,
    limit_of: Callable[[int, str], Limit],
) -> Answer:
    # What the RSSHub instance at the address given answers a route's address with, asked with
    # its key. An instance that does not answer, one that refuses the key, and one that would
    # lead the key away to another server, are told as what they are; the address with the key
    # is in no sentence.
    instance = rsshub.rstrip(ROUTE_APART)
    asked = served_at(address, rsshub)
    try:
        answer = answer_within(asked, validators, allowance, limit_of, rsshub_query(key))
    except LedAway as failure:
        raise RSSHubFault(KEY_KEPT.format(said=failure)) from failure
    except NOT_REACHED as failure:
        reason = FAILED.format(kind=type(failure).__name__, said=error_text(failure))
        raise SilentRSSHub(SILENT_RSSHUB.format(instance=instance, reason=reason)) from failure
    except httpx.TransportError as failure:
        reason = FAILED.format(kind=type(failure).__name__, said=error_text(failure))
        route = route_of(address)
        said = ROUTE_BROKEN_OFF.format(instance=instance, route=route, reason=reason)
        raise UnreadableFeed(said) from failure
    refused = answer.status == HTTPStatus.FORBIDDEN
    if refused and rsshub_message(answer.content).startswith(KEY_ERROR):
        raise RSSHubFault((KEY_REFUSED if key else KEY_MISSING).format(instance=instance))
    return answer


def route_refused(address: str, rsshub: str, answer: Answer) -> UnreadableFeed:
    said = said_by_rsshub(answer.status, answer.content)
    instance = rsshub.rstrip(ROUTE_APART)
    return UnreadableFeed(
        ROUTE_REFUSED.format(instance=instance, route=route_of(address), said=said)
    )


def routed_feed(address: str, rsshub: str, key: str | None, allowance: Allowance) -> Discovered:
    # The feed at a route of the RSSHub instance at the address given, kept as the route. Where
    # RSSHub answers the route with an error, the reader is told what it said.
    answer = asked_of_rsshub(address, rsshub, key, Validators(), allowance, page_limit)
    if answer.status != HTTPStatus.OK:
        raise route_refused(address, rsshub, answer)
    return discovered_of(answer, address, address)


def polled_route(address: str, validators: Validators, rsshub: str, key: str | None) -> Fetched:
    # One poll of a route, as fetched_feed polls a feed, at the RSSHub instance given at the
    # time of the poll, with its key; what RSSHub answers with an error is told as for a
    # discovery.
    allowance = Allowance(requests=1 + MOST_REDIRECTS, ends=ends_in(FEED_LIMIT_SECONDS))
    answer = asked_of_rsshub(address, rsshub, key, validators, allowance, feed_limit)
    if answer.status == HTTPStatus.NOT_MODIFIED:
        return Unchanged()
    if answer.status != HTTPStatus.OK:
        raise route_refused(address, rsshub, answer)
    return changed(answer)


def rules_limit(status: int, declared: str) -> Limit:
    if status == HTTPStatus.OK:
        return Limit(most_bytes=RULES_LIMIT_BYTES, most_seconds=RULES_LIMIT_SECONDS)
    return page_limit(status, declared)


def rules_of(rsshub: str, key: str | None, ends: float) -> HeldRules:
    # The radar rules of the instance, fetched with one request within their own bounds and
    # what is left of the discovery's minute. Rules that hold more than the fetcher makes ready
    # are told as RSSHub's.
    allowance = Allowance(requests=1, ends=min(ends, ends_in(RULES_LIMIT_SECONDS)))
    outgrown = RSSHubFault(RULES_OUTGROWN.format(instance=rsshub.rstrip(ROUTE_APART)))
    try:
        answer = asked_of_rsshub(RULES_ADDRESS, rsshub, key, Validators(), allowance, rules_limit)
    except Oversized as failure:
        raise outgrown from failure
    if answer.status != HTTPStatus.OK:
        raise UnreadableFeed(error_status(answer.address, answer.status))
    try:
        return held_rules(answer.content)
    except MemoryError as failure:
        raise outgrown from failure


def radar_routes(address: str, rsshub: str, key: str | None, ends: float) -> list[RadarRoute]:
    # The routes RSSHub's rules give for an address; none where the instance answers with what
    # is not its rules. An instance that does not answer, a key it refuses, and rules that hold
    # more than infonium reads, are told.
    try:
        rules = rules_of(rsshub, key, ends)
    except RSSHubFault:
        raise
    except (UnreadableFeed, httpx.HTTPError, httpx.InvalidURL, ValidationError):
        return []
    return routes_for(address, rules)


def best_routes(address: str, found: list[RadarRoute]) -> list[str]:
    # The route of the rule that matches an address better than every other; where several
    # match it equally well, the reader is told their routes, to give the one they want.
    best = [route for route in found if route.spelled == found[0].spelled] if found else []
    if len(best) > 1:
        told = [
            TOLD_ROUTE.format(address=route_address(route.route), title=route.title)
            for route in best[:MOST_ROUTES_TOLD]
        ]
        more = len(best) - len(told)
        rest = MORE_ROUTES.format(more=more) if more else ""
        routes = ROUTES_APART.join(told)
        raise UnreadableFeed(
            MANY_ROUTES.format(count=len(best), address=address, routes=routes, rest=rest)
        )
    return [route_address(route.route) for route in best]


def routes_by_rules(
    address: str, rsshub: str, key: str | None, ends: float, refusal: Exception
) -> list[str]:
    # The route RSSHub's rules give for an address that its site's own means refused, for the
    # reason given. Where a fault of the instance kept its rules from being asked, the reader is
    # told why the address leads to no feed, and the fault. Where the site itself was not
    # reached, an instance that does not answer gives no route, and another fault is told alone.
    try:
        return best_routes(address, radar_routes(address, rsshub, key, ends))
    except RSSHubFault as fault:
        if isinstance(refusal, UnreadableFeed):
            said = NO_FEED_NOR_ROUTE.format(refusal=refusal, fault=fault)
            raise RSSHubFault(said) from fault
        if isinstance(fault, SilentRSSHub):
            return []
        raise


def found_at(address: str, allowance: Allowance) -> Discovered:
    # The feed an address leads to by its site's own means: a show on Apple Podcasts, a channel
    # or a playlist of YouTube named by its id, the feed itself, or a page that leads to one.
    parts = urlsplit(address)
    host = (parts.hostname or "").casefold()
    shows = APPLE_SHOW.findall(parts.path) if host == APPLE_PODCASTS_HOST else []
    kept = youtube_feed(address) if host in YOUTUBE_HOSTS else []
    if shows:
        return apple_podcasts_feed(address, shows[-1], allowance)
    if kept:
        return feed_at(kept[0], allowance)
    return discovered_at(address, allowance)


def found_by_rules(address: str, rsshub: str, key: str | None) -> Discovered:
    # The feed an address leads to by the rules above; where none of them leads to a feed, as
    # where the site turns infonium away, the route of the rule of RSSHub that matches the
    # address best, and where none matches it, why the rest found no feed; where a fault of the
    # instance kept its rules from being asked, that as well. The site's own means and RSSHub's
    # route have requests of their own, and the minute in common.
    ends = ends_in(DISCOVERY_LIMIT_SECONDS)
    if len(address) > ADDRESS_CHARACTERS:
        shown = address[:SHOWN_OF_LONG_ADDRESS] + TITLE_CUT_SHORT
        raise UnreadableFeed(LONG_ADDRESS.format(address=shown, most=ADDRESS_CHARACTERS))
    if is_route(address):
        kept = route_address(address[len(RSSHUB_SCHEME) :])
        if not re.fullmatch(ROUTE_ADDRESS_PATTERN, kept):
            raise UnreadableFeed(NOT_AN_ADDRESS.format(address=address))
        return routed_feed(kept, rsshub, key, Allowance(requests=MOST_REQUESTS, ends=ends))
    if not web_addresses([address], address):
        raise UnreadableFeed(NOT_AN_ADDRESS.format(address=address))
    try:
        site = Allowance(requests=MOST_REQUESTS - KEPT_FOR_RSSHUB, ends=ends)
        return found_at(address, site)
    except (UnreadableFeed, httpx.HTTPError, httpx.InvalidURL) as refusal:
        routes = routes_by_rules(address, rsshub, key, ends, refusal)
        if not routes:
            raise
    return routed_feed(routes[0], rsshub, key, Allowance(requests=KEPT_FOR_RSSHUB, ends=ends))


def discovered_feed(address: str, rsshub: str, key: str | None = None) -> Discovered:
    # The feed that an address a reader gave leads to: the feed itself, a page that names its
    # feed, a show or a channel whose site keeps its feed elsewhere, or a route of the RSSHub
    # instance at the address given, asked with its key where it is given one. Shows on Apple
    # Podcasts, and YouTube's channels and playlists named by their ids, are found by their
    # addresses alone. Whichever rule refuses the address, the reader is told what they may give
    # instead; where the instance itself is at fault, that is told, and no route is advised. A
    # failure to connect is left as httpx raises it.
    try:
        return found_by_rules(address, rsshub, key)
    except RSSHubFault:
        raise
    except UnreadableFeed as refusal:
        raise UnreadableFeed(with_advice(str(refusal))) from refusal
