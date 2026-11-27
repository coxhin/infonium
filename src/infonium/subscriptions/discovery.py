import re
import time
from functools import lru_cache
from http import HTTPStatus
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
import trafilatura
from pydantic import BaseModel, Field, ValidationError

from infonium.document import OVERDUE_SOURCE, STATUS_NAMES, WORD_GAP
from infonium.intake import CONTENT_TYPE, PAGE_MEDIA_TYPES, UNDECLARED, declared_media_type
from infonium.reasons import failure_reason
from infonium.subscriptions.feed import (
    ADDRESS_CHARACTERS,
    GIVE_A_FEED,
    UnreadableFeed,
    WebAddress,
    is_page,
    web_addresses,
)
from infonium.subscriptions.fetcher import (
    FEED_LIMIT_BYTES,
    FEED_LIMIT_SECONDS,
    Answer,
    Changed,
    Validators,
    answer_of,
    answer_to,
    asked,
    bounded_content,
    changed,
)
from infonium.subscriptions.radar import (
    RADAR_RULES_PATH,
    HeldRules,
    RadarRoute,
    held_rules,
    routes_for,
)

# How an address begins that names a route of RSSHub, a bridge that makes feeds of sites
# without one; the route follows.
RSSHUB_SCHEME = "rsshub://"
ROUTE_APART = "/"
# Where an address leads to no feed by the other rules, the route that RSSHub's own rules give
# for it is tried. The rules of an instance are fetched once a day and kept, as they change only
# with its version; they may hold 20 MiB and take 30 s to fetch, besides the requests and the
# minute of the discovery. An instance that does not answer costs each discovery one refused
# connection, and leaves what it finds as it is.
RULES_KEPT_SECONDS = 24 * 3600
RULES_LIMIT_BYTES = 20 * 1024**2
RULES_LIMIT_SECONDS = 30.0
# What RSSHub's page of an error may hold, and where it says what went wrong.
REFUSAL_LIMIT_BYTES = 1024**2
RSSHUB_MESSAGE = "(//p[@class='message'])[1]/code//text()"

# A page is fetched as a feed is, within less; one discovery within a minute and eight
# requests, each a fetch with the redirects it follows. A fetch is not begun once either is
# spent, and a body is read no further once the minute is over, but a server that falls silent
# is waited for as long as any fetch waits. Of the requests, one is kept for a route of RSSHub
# where nothing else leads to a feed.
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
ALTERNATE = "alternate"
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

# What a reader is told of an address that leads to no feed.
NOT_AN_ADDRESS = (
    "{address} is neither a web address nor a route of RSSHub: give an address that begins with"
    " http:// or https://, or a route as " + RSSHUB_SCHEME + "<route>"
)
NAMES_NO_FEED = (
    "{address} is a web page that names no feed, and none was found where its site would keep"
    " one; " + GIVE_A_FEED
)
# Of named feeds that cannot be read, the reason the first could not is told, as the feed a
# page names first is the one it gives as its own.
NAMED_FEEDS_UNREADABLE = (
    "{address} names feeds that cannot be read, and none was found where its site would keep"
    " one; the first it names: {reason}"
)
UNKEPT_ADDRESS = (
    "{address} leads on to an address of more than {most} characters, which infonium does not"
    " keep; " + GIVE_A_FEED
)
SILENT_RSSHUB = (
    "RSSHub at {instance} did not answer ({reason}); INFONIUM_RSSHUB_BASE_URL names the instance"
)
ROUTE_REFUSED = "RSSHub at {instance} answered the route {route} with {said}"
SAID = "{status}: {message}"
NO_APPLE_FEED = "Apple Podcasts names no feed of the show at {address}; " + GIVE_A_FEED
YOUTUBE_CONSENT = (
    "YouTube answered {address} with its page that asks for consent to cookies, which names no"
    " feed; give the channel's address with its id, as https://www.youtube.com/channel/UC…"
)


class Discovered(BaseModel):
    # The feed an address leads to: where it is served, and the feed as it was fetched there
    # in finding it.
    address: WebAddress
    fetched: Changed


class Allowance:
    # What one discovery may still do: how many requests it may still make, and the time on the
    # monotonic clock by which it ends.
    def __init__(self) -> None:
        self.requests = MOST_REQUESTS
        self.ends = time.monotonic() + DISCOVERY_LIMIT_SECONDS

    def left(self, feeds: list[str], kept: int) -> list[str]:
        # The first of the feeds given that may still be asked for, with as many requests as
        # given kept back for later.
        return feeds[: max(self.requests - kept, 0)]

    def spend(self, address: str) -> None:
        # One request taken, to the address given.
        if time.monotonic() > self.ends:
            raise UnreadableFeed(OVERDUE_SOURCE.format(source=address))
        self.requests -= 1


class ApplePodcastsShow(BaseModel):
    feed: str | None = Field(default=None, alias="feedUrl")


class ApplePodcastsLookup(BaseModel):
    results: list[ApplePodcastsShow] = []


def fetched_answer(address: str, allowance: Allowance) -> Answer:
    # What an address answers with, held to a page's bounds where it declares a page and to a
    # feed's otherwise, and to the time the discovery ends by.
    allowance.spend(address)
    began = time.monotonic()
    ends = allowance.ends
    with answer_to(address, Validators()) as response:
        page = (
            declared_media_type(response.headers.get(CONTENT_TYPE, UNDECLARED)) in PAGE_MEDIA_TYPES
        )
        most_bytes = PAGE_LIMIT_BYTES if page else FEED_LIMIT_BYTES
        most_seconds = PAGE_LIMIT_SECONDS if page else FEED_LIMIT_SECONDS
        content = bounded_content(response, address, most_bytes, min(ends, began + most_seconds))
        return answer_of(response, content)


def discovered_of(answer: Answer, asked: str) -> Discovered:
    # The feed an answer holds, where infonium keeps the address it came from.
    if not web_addresses([answer.address], answer.address):
        raise UnreadableFeed(UNKEPT_ADDRESS.format(address=asked, most=ADDRESS_CHARACTERS))
    return Discovered(address=answer.address, fetched=changed(answer))


def feed_at(address: str, allowance: Allowance) -> Discovered:
    return discovered_of(fetched_answer(address, allowance), address)


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
        return discovered_of(answer, address)
    named = named_feeds(answer)[:NAMED_FEEDS_TRIED]
    elsewhere = (
        channel_feeds(answer)
        if host in YOUTUBE_HOSTS
        else [urljoin(answer.address, path) for path in COMMON_FEED_PATHS]
    )
    reasons: list[str] = []
    for feed in allowance.left([*named, *elsewhere], KEPT_FOR_RSSHUB):
        try:
            return feed_at(feed, allowance)
        except (UnreadableFeed, httpx.HTTPError, httpx.InvalidURL) as failure:
            reasons.append(failure_reason(failure))
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
    # wrong, where it says it.
    named = STATUS_NAMES.get(status, str(status))
    tree = trafilatura.load_html(page) if page else None
    said = WORD_GAP.join("".join(tree.xpath(RSSHUB_MESSAGE)).split()) if tree is not None else ""
    return SAID.format(status=named, message=said) if said else named


def routed_feed(route: str, rsshub: str, allowance: Allowance) -> Discovered:
    # The feed at a route of the RSSHub instance at the address given, joined to it with one
    # slash. Where RSSHub answers the route with an error, the reader is told what it said.
    instance = rsshub.rstrip(ROUTE_APART)
    path = ROUTE_APART + route.lstrip(ROUTE_APART)
    routed = instance + path
    allowance.spend(routed)
    began = time.monotonic()
    try:
        with asked(routed, Validators()) as response:
            status = response.status_code
            most_bytes = FEED_LIMIT_BYTES if status == HTTPStatus.OK else REFUSAL_LIMIT_BYTES
            ends = min(allowance.ends, began + FEED_LIMIT_SECONDS)
            answer = answer_of(response, bounded_content(response, routed, most_bytes, ends))
    except httpx.TransportError as failure:
        reason = failure_reason(failure)
        raise UnreadableFeed(SILENT_RSSHUB.format(instance=rsshub, reason=reason)) from failure
    if status != HTTPStatus.OK:
        said = said_by_rsshub(status, answer.content)
        raise UnreadableFeed(ROUTE_REFUSED.format(instance=instance, route=path, said=said))
    return discovered_of(answer, routed)


@lru_cache(maxsize=1)
def rules_of(instance: str, period: int) -> HeldRules:
    # The radar rules of an instance, kept for the period they were fetched in, by its number:
    # lru_cache keeps what a call returns and nothing of one that fails, so an instance that
    # does not answer is asked again by the next discovery.
    address = instance + RADAR_RULES_PATH
    with answer_to(address, Validators()) as response:
        ends = time.monotonic() + RULES_LIMIT_SECONDS
        content = bounded_content(response, address, RULES_LIMIT_BYTES, ends)
    return held_rules(content)


def radar_routes(address: str, rsshub: str) -> list[RadarRoute]:
    # The route RSSHub's rules give for an address; none where the instance does not answer, or
    # answers with what is not its rules.
    period = int(time.time() // RULES_KEPT_SECONDS)
    try:
        rules = rules_of(rsshub.rstrip(ROUTE_APART), period)
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


def discovered_feed(address: str, rsshub: str) -> Discovered:
    # The feed that an address a reader gave leads to: the feed itself, a page that names its
    # feed, a show or a channel whose site keeps its feed elsewhere, or a route of the RSSHub
    # instance at the address given. Shows on Apple Podcasts, and YouTube's channels and
    # playlists named by their ids, are found by their addresses alone. Where none of these
    # leads to a feed, as where the site turns infonium away, the route that RSSHub's rules
    # give for the address is tried, and where they give none the reader is told why the rest
    # found no feed.
    allowance = Allowance()
    if address.startswith(RSSHUB_SCHEME):
        return routed_feed(address.removeprefix(RSSHUB_SCHEME), rsshub, allowance)
    if not web_addresses([address], address):
        raise UnreadableFeed(NOT_AN_ADDRESS.format(address=address))
    try:
        return found_at(address, allowance)
    except (UnreadableFeed, httpx.HTTPError, httpx.InvalidURL):
        routes = [found.route for found in radar_routes(address, rsshub)]
        if not allowance.left(routes, 0):
            raise
    return routed_feed(routes[0], rsshub, allowance)
