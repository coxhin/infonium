import re
import time
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
    REFUSAL_LIMIT_BYTES,
    Allowance,
    Answer,
    Changed,
    Limit,
    Validators,
    answer_within,
    changed,
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
# without one; the route follows. It is told in any case, and kept written as here, the route
# without slashes at its start or two in a row: a feed of RSSHub is kept as its route, and
# asked at the instance that INFONIUM_RSSHUB_BASE_URL names when it is asked, so that the
# instance may move.
RSSHUB_SCHEME = "rsshub://"
ROUTE_APART = "/"
ROUTE_QUERY = "?"
ROUTE_ADDRESS_PATTERN = rf"^{re.escape(RSSHUB_SCHEME)}\S+$"
RouteAddress = Annotated[
    str, StringConstraints(pattern=ROUTE_ADDRESS_PATTERN, max_length=ADDRESS_CHARACTERS)
]
# Where an address leads to no feed by the other rules, the route that RSSHub's own rules give
# for it is tried. The rules of the instance are fetched by each discovery that comes to them,
# as one runs in a process of its own: 1 MiB, fetched and made ready in 0.2 s. They may hold
# 20 MiB and take 30 s, of what is left of the discovery's minute, by requests besides the
# discovery's. An instance that does not answer costs a discovery one refused connection, and
# leaves what it finds as it is.
RULES_LIMIT = Limit(most_bytes=20 * 1024**2, most_seconds=30.0)
# Where RSSHub's page of an error says what went wrong.
RSSHUB_MESSAGE = "(//p[@class='message'])[1]/code//text()"
FULL_STOP = "."

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
CHANNEL_PATH = re.compile(r"/channel/(UC[\w-]{22})(?:[/?#]|$)")
CANONICAL = "//link[@rel='canonical']/@href"
# Where YouTube asks a person in some regions to agree to its use of cookies, in place of the
# page asked for, before it shows any page.
YOUTUBE_CONSENT_HOST = "consent.youtube.com"

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
SILENT_RSSHUB = (
    "RSSHub at {instance} did not answer ({reason}); INFONIUM_RSSHUB_BASE_URL names the instance"
)
ROUTE_REFUSED = "RSSHub at {instance} answered the route {route} with {said}"
SAID = "{status}: {message}"
NO_APPLE_FEED = "Apple Podcasts names no feed of the show at {address}"
YOUTUBE_CONSENT = (
    "YouTube answered {address} with its page that asks for consent to cookies, which names no"
    " feed; a channel is found by its address with its id, as https://www.youtube.com/channel/UC…"
)


class Discovered(BaseModel):
    # The feed an address leads to: where it is served, a web address or a route of RSSHub,
    # and the feed as it was fetched there in finding it.
    address: WebAddress | RouteAddress
    fetched: Changed


class ApplePodcastsShow(BaseModel):
    feed: str | None = Field(default=None, alias="feedUrl")


class ApplePodcastsLookup(BaseModel):
    results: list[ApplePodcastsShow] = []


def with_advice(sentence: str) -> str:
    return WITH_ADVICE.format(sentence=sentence)


def is_route(address: str) -> bool:
    return address[: len(RSSHUB_SCHEME)].casefold() == RSSHUB_SCHEME


def route_path(route: str) -> str:
    # A route written one way, as a browser would ask for it, with one slash before each part
    # of its path however many it was written with.
    path, mark, query = asked_as(route).partition(ROUTE_QUERY)
    parts = [part for part in path.split(ROUTE_APART) if part]
    return ROUTE_APART + ROUTE_APART.join(parts) + mark + query


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


def channel_feeds(answer: Answer) -> list[str]:
    # The feed of the YouTube channel whose page is the one answered, by the id in its
    # canonical link.
    page = trafilatura.load_html(answer.content)
    canonical = [str(address) for address in page.xpath(CANONICAL)] if page is not None else []
    channels = [found[1] for address in canonical if (found := CHANNEL_PATH.search(address))]
    return [YOUTUBE_FEED.format(query=urlencode({CHANNEL_ID: channel})) for channel in channels]


def discovered_at(address: str, allowance: Allowance) -> Discovered:
    # The feed itself, or the first that reads of the feeds a page names and then of the places
    # where its site would keep one: on YouTube the feed of the channel whose id the page
    # carries, elsewhere the places where feeds commonly are. A feed that cannot be fetched or
    # read is passed over for the next.
    answer = fetched_answer(address, allowance)
    host = urlsplit(answer.address).hostname
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
        return [YOUTUBE_FEED.format(query=urlencode({CHANNEL_ID: channel[1]}))]
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


def said_by_rsshub(status: int, page: bytes) -> str:
    # What RSSHub answered a route with: the status, and what its page of the error says went
    # wrong, where it says it, as a clause of the sentence the reader is told.
    named = STATUS_NAMES.get(status, str(status))
    tree = trafilatura.load_html(page) if page else None
    message = "".join(tree.xpath(RSSHUB_MESSAGE)) if tree is not None else ""
    said = WORD_GAP.join(message.split()).removesuffix(FULL_STOP)
    return SAID.format(status=named, message=said) if said else named


def routed_feed(address: str, rsshub: str, allowance: Allowance) -> Discovered:
    # The feed at a route of the RSSHub instance at the address given, kept as the route. Where
    # RSSHub answers the route with an error, the reader is told what it said.
    routed = served_at(address, rsshub)
    instance = rsshub.rstrip(ROUTE_APART)
    try:
        answer = answer_within(routed, Validators(), allowance, page_limit)
    except httpx.TransportError as failure:
        reason = unreached(routed, failure)
        raise UnreadableFeed(SILENT_RSSHUB.format(instance=instance, reason=reason)) from failure
    if answer.status != HTTPStatus.OK:
        said = said_by_rsshub(answer.status, answer.content)
        route = route_of(address)
        raise UnreadableFeed(ROUTE_REFUSED.format(instance=instance, route=route, said=said))
    return discovered_of(answer, address, address)


def rules_limit(status: int, declared: str) -> Limit:
    return RULES_LIMIT if status == HTTPStatus.OK else page_limit(status, declared)


def rules_of(instance: str, allowance: Allowance) -> HeldRules:
    # The radar rules of an instance, fetched within their own bounds and what is left of the
    # discovery's minute.
    seconds = min(RULES_LIMIT.most_seconds, allowance.ends - time.monotonic())
    fetching = Allowance(requests=1 + MOST_REDIRECTS, seconds=seconds)
    answer = answer_within(instance + RADAR_RULES_PATH, Validators(), fetching, rules_limit)
    if answer.status != HTTPStatus.OK:
        raise UnreadableFeed(error_status(answer.address, answer.status))
    return held_rules(answer.content)


def radar_routes(address: str, rsshub: str, allowance: Allowance) -> list[RadarRoute]:
    # The routes RSSHub's rules give for an address; none where the instance does not answer,
    # or answers with what is not its rules.
    try:
        rules = rules_of(rsshub.rstrip(ROUTE_APART), allowance)
    except (UnreadableFeed, httpx.HTTPError, httpx.InvalidURL, ValidationError):
        return []
    return routes_for(address, rules)


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


def found_by_rules(address: str, rsshub: str) -> Discovered:
    # The feed an address leads to by the rules above; where none of them leads to a feed, as
    # where the site turns infonium away, the first route that RSSHub's rules give for the
    # address, and where they give none, why the rest found no feed.
    allowance = Allowance(requests=MOST_REQUESTS, seconds=DISCOVERY_LIMIT_SECONDS)
    if len(address) > ADDRESS_CHARACTERS:
        shown = address[:SHOWN_OF_LONG_ADDRESS] + TITLE_CUT_SHORT
        raise UnreadableFeed(LONG_ADDRESS.format(address=shown, most=ADDRESS_CHARACTERS))
    if is_route(address):
        kept = route_address(address[len(RSSHUB_SCHEME) :])
        if not re.fullmatch(ROUTE_ADDRESS_PATTERN, kept):
            raise UnreadableFeed(NOT_AN_ADDRESS.format(address=address))
        return routed_feed(kept, rsshub, allowance)
    if not web_addresses([address], address):
        raise UnreadableFeed(NOT_AN_ADDRESS.format(address=address))
    allowance.kept = KEPT_FOR_RSSHUB
    try:
        return found_at(address, allowance)
    except (UnreadableFeed, httpx.HTTPError, httpx.InvalidURL):
        allowance.kept = 0
        found = radar_routes(address, rsshub, allowance)
        routes = [route_address(route.route) for route in found]
        if not allowance.left(routes):
            raise
    return routed_feed(routes[0], rsshub, allowance)


def discovered_feed(address: str, rsshub: str) -> Discovered:
    # The feed that an address a reader gave leads to: the feed itself, a page that names its
    # feed, a show or a channel whose site keeps its feed elsewhere, or a route of the RSSHub
    # instance at the address given. Shows on Apple Podcasts, and YouTube's channels and
    # playlists named by their ids, are found by their addresses alone. Whichever rule refuses
    # the address, the reader is told what they may give instead. A failure to connect is left
    # as httpx raises it.
    try:
        return found_by_rules(address, rsshub)
    except UnreadableFeed as refusal:
        raise UnreadableFeed(with_advice(str(refusal))) from refusal
