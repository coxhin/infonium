import json
import time
from urllib.parse import urljoin, urlsplit

import httpx
import pytest
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from infonium.document import OVERDUE_SOURCE, OVERSIZE_SOURCE
from infonium.subscriptions.discovery import discovered_feed
from infonium.subscriptions.feed import ADDRESS_CHARACTERS, UnreadableFeed, read_feed
from infonium.subscriptions.fetcher import Changed, Validators
from tests.feed_sites import (
    CONTENT_TYPE,
    HTML,
    LOWERED_DISCOVERY_SECONDS,
    LOWERED_FEED_BYTES,
    LOWERED_PAGE_BYTES,
    LOWERED_PAGE_SECONDS,
    FeedSites,
    answered,
    failing,
    kind_of,
    moved,
    trickling,
)
from tests.feed_writing import youtube_feed

# Judgments of discovered_feed (D1 to D7 and their amendments), finding feeds on stand-in sites
# that each test scripts.

RSSHUB = "https://rsshub.test"
RSSHUB_SETTING = "INFONIUM_RSSHUB_BASE_URL"
RSSHUB_FORM = "rsshub://"
RSS_KIND = "application/rss+xml"
ATOM_KIND = "application/atom+xml"
JSON_FEED_KIND = "application/feed+json"
ETAG = "etag"
LAST_MODIFIED = "last-modified"
# The paths at a site that feeds commonly live at, and the most requests one discovery makes.
COMMON_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml")
MOST_REQUESTS = 7
NAMED_FEEDS_TRIED = 3
OVERRUN_SECONDS = 0.5
EXAMPLES = settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
FAULT_EXAMPLES = settings(
    EXAMPLES, phases=(Phase.explicit, Phase.reuse, Phase.generate), max_examples=10
)
FILLER = "".join(
    f"<p>Paragraph {number} of a made-up page about night trains and the timetables of the "
    "sleepers that run along the coast in winter.</p>"
    for number in range(6)
)


def rss_feed(title: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel><title>{title}</title>'
        "<link>https://blog.test/</link><item><title>A posting</title>"
        f"<link>https://blog.test/p/{title}</link></item></channel></rss>"
    ).encode()


def atom_feed(title: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
        f"<title>{title}</title><entry><id>tag:blog.test,2026:{title}</id><title>A posting</title>"
        f'<link href="https://blog.test/p/{title}"/></entry></feed>'
    ).encode()


def json_feed(title: str) -> bytes:
    items = [{"id": title, "url": f"https://blog.test/p/{title}", "title": "A posting"}]
    said = {"version": "https://jsonfeed.org/version/1.1", "title": title, "items": items}
    return json.dumps(said).encode()


def page_naming(head: str) -> bytes:
    return (
        f"<!doctype html><html><head><title>Night trains</title>{head}</head>"
        f"<body><article><h1>Night trains</h1>{FILLER}</article></body></html>"
    ).encode()


# What an address a page names answers: a feed in one of three forms, or something that does
# not read as a feed.
FEEDS = {
    "RSS": (rss_feed, RSS_KIND),
    "Atom": (atom_feed, ATOM_KIND),
    "JSON": (json_feed, JSON_FEED_KIND),
}
NOT_FEEDS = {
    "a page": (page_naming(""), HTML),
    "JSON of a post": (b'{"id": 12, "title": {"rendered": "Night trains"}}', "application/json"),
    "nothing": (b"", RSS_KIND),
}
MISSING = "missing"


def serve_answer(sites: FeedSites, address: str, said: str) -> None:
    # Serves at the address what is said of it: a feed of a form, titled by its address, or
    # something that is no feed; nothing for what is missing.
    if said in FEEDS:
        write, kind = FEEDS[said]
        sites.serve(address, answered(write(address), kind_of(kind)))
    elif said in NOT_FEEDS:
        content, kind = NOT_FEEDS[said]
        sites.serve(address, answered(content, kind_of(kind)))


def assert_found(
    sites: FeedSites, address: str, rsshub: str, at: str, said: str, what: str
) -> None:
    # The feed the address leads to is the one served at `at`, as it was fetched there.
    write, kind = FEEDS[said]
    discovered = discovered_feed(address, rsshub)
    assert discovered.address == at, (
        f"{what} was found at {discovered.address}, not at its feed {at}"
    )
    expected = Changed(feed=read_feed(write(at), kind, at), validators=Validators())
    assert discovered.fetched == expected, (
        f"{what} was found with {discovered.fetched}, not the feed as fetched at {at}"
    )


def refusal(address: str, rsshub: str, what: str) -> str:
    with pytest.raises(UnreadableFeed) as refused:
        discovered = discovered_feed(address, rsshub)
        pytest.fail(f"{what} led to {discovered}, where it leads to no feed")
    return str(refused.value)


# D1: a route of RSSHub.

instances = st.sampled_from(
    (
        "https://rsshub.test",
        "https://rsshub.test/",
        "https://bridge.test/rsshub",
        "http://bridge.test/rsshub/",
    )
)
routes = st.lists(
    st.text("abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=10),
    min_size=1,
    max_size=4,
).map("/".join)
route_queries = st.sampled_from(("", "?limit=5", "?filter=night&limit=2"))
leading_slashes = st.sampled_from(("", "/", "//"))


def instance_route(instance: str, route: str) -> str:
    return f"{instance.rstrip('/')}/{route}"


@EXAMPLES
@given(instance=instances, route=routes, query=route_queries, slashes=leading_slashes)
def test_an_rsshub_route_is_the_feed_at_that_route_of_the_instance(
    feed_sites: FeedSites, instance: str, route: str, query: str, slashes: str
) -> None:
    feed_sites.forget()
    at = instance_route(instance, route + query)
    feed_sites.serve(at, answered(rss_feed(at), kind_of(RSS_KIND)))
    discovered = discovered_feed(f"{RSSHUB_FORM}{slashes}{route}{query}", instance)
    assert feed_sites.fetches == [at], (
        f"the route {slashes}{route}{query} of RSSHub at {instance} was fetched at "
        f"{feed_sites.fetches}, not at {at}, joined with one slash"
    )
    expected = Changed(feed=read_feed(rss_feed(at), RSS_KIND, at), validators=Validators())
    assert discovered.fetched == expected, (
        f"the route {route} of RSSHub was found with {discovered.fetched}, not its feed as fetched"
    )


@pytest.mark.xfail(
    strict=True,
    reason="D1 as amended, not yet built: an rsshub:// address is found at the instance's "
    "address of its route, where it is to be found at rsshub:// and its route",
)
@FAULT_EXAMPLES
@given(instance=instances, route=routes, slashes=leading_slashes)
def test_an_rsshub_route_is_found_at_its_route_written_one_way(
    feed_sites: FeedSites, instance: str, route: str, slashes: str
) -> None:
    feed_sites.forget()
    at = instance_route(instance, route)
    feed_sites.serve(at, answered(rss_feed(at), kind_of(RSS_KIND)))
    discovered = discovered_feed(f"{RSSHUB_FORM}{slashes}{route}", instance)
    assert discovered.address == f"{RSSHUB_FORM}{route}", (
        f"the address {RSSHUB_FORM}{slashes}{route} was found at {discovered.address}, not at "
        f"{RSSHUB_FORM}{route}"
    )


SCHEME_CASES = ("RSSHub://", "RSSHUB://", "rssHub://")


@pytest.mark.xfail(
    strict=True,
    reason="D1 as amended, not yet built: an address whose scheme is rsshub in another case is "
    "refused as neither a web address nor a route",
)
@pytest.mark.parametrize("scheme", SCHEME_CASES)
def test_an_rsshub_route_is_told_by_its_scheme_in_any_case(
    feed_sites: FeedSites, scheme: str
) -> None:
    at = instance_route(RSSHUB, "github/issue/night/trains")
    feed_sites.serve(at, answered(rss_feed(at), kind_of(RSS_KIND)))
    discovered = discovered_feed(f"{scheme}github/issue/night/trains", RSSHUB)
    assert feed_sites.fetches == [at], (
        f"the address {scheme}github/issue/night/trains was fetched at {feed_sites.fetches}, not "
        f"as a route of RSSHub at {at}"
    )
    assert discovered.fetched.feed.title == at, f"the route was found with {discovered}"


@EXAMPLES
@given(
    instance=instances,
    kind=st.sampled_from((httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout)),
)
def test_an_rsshub_instance_that_does_not_answer_is_told_with_its_setting(
    feed_sites: FeedSites, instance: str, kind: type[httpx.TransportError]
) -> None:
    feed_sites.forget()
    feed_sites.serve(instance_route(instance, "night/trains"), failing(kind, []))
    told = refusal(f"{RSSHUB_FORM}night/trains", instance, f"RSSHub at {instance}, silent")
    assert instance.rstrip("/") in told and "RSSHub" in told and RSSHUB_SETTING in told, (
        f"RSSHub at {instance} failing with {kind.__name__} was told as {told!r}, which does not "
        f"say that RSSHub at {instance} did not answer and that {RSSHUB_SETTING} names it"
    )


# D2: a feed.


@EXAMPLES
@given(
    form=st.sampled_from(sorted(FEEDS)),
    path=st.sampled_from(("/feed.xml", "/a/b/atom", "/feeds/all.json")),
    status=st.sampled_from((None, 301, 308)),
    etag=st.sampled_from((None, '"v1"', 'W/"night"')),
    modified=st.sampled_from((None, "Wed, 25 Nov 2026 10:00:00 GMT")),
)
def test_a_feed_is_its_own_feed_found_with_one_fetch(
    feed_sites: FeedSites,
    form: str,
    path: str,
    status: int | None,
    etag: str | None,
    modified: str | None,
) -> None:
    feed_sites.forget()
    asked = f"https://blog.test{path}"
    at = asked if status is None else f"https://feeds.blog.test{path}"
    if status is not None:
        feed_sites.serve(asked, moved(at, status))
    write, kind = FEEDS[form]
    validated = {ETAG: etag, LAST_MODIFIED: modified}
    headers = httpx.Headers(
        {CONTENT_TYPE: kind} | {name: value for name, value in validated.items() if value}
    )
    feed_sites.serve(at, answered(write(at), headers))
    discovered = discovered_feed(asked, RSSHUB)
    assert len(feed_sites.fetches) == 1, (
        f"the {form} feed at {asked} was found with the fetches {feed_sites.fetches}, not one"
    )
    expected = Changed(
        feed=read_feed(write(at), kind, at), validators=Validators(etag=etag, modified=modified)
    )
    assert (discovered.address, discovered.fetched) == (at, expected), (
        f"the {form} feed at {asked}, served at {at}, was found as {discovered}, not at {at} "
        f"with {expected}"
    )


# D3: a page that names its feeds.

FEED_TYPES = (
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "application/json",
)
REL_WORDS = ("feed", "home", "nofollow")
HREF_FORMS = (
    "feed-{n}.xml",
    "/feeds/{n}.atom",
    "../comments/{n}.json",
    "?feed=rss{n}",
    "//other.test/{n}.rss",
    "https://feeds.example.test/{n}",
)
DECOYS = (
    '<link rel="stylesheet" type="text/css" href="/style.css">',
    '<link rel="icon" href="/favicon.ico">',
    '<link rel="alternate" type="text/html" hreflang="fr" href="/fr/">',
    '<link rel="alternate" type="application/pdf" href="/print.pdf">',
    '<link rel="canonical" href="/">',
)
PAGES = ("https://blog.test/2026/11/night-trains/", "https://blog.test/posts/night-trains")
PAGE_BASES = (None, "https://static.blog.test/root/", "/en/")


def any_case(draw: st.DrawFn, text: str) -> str:
    return "".join(draw(st.sampled_from((letter.lower(), letter.upper()))) for letter in text)


class NamedFeed(BaseModel):
    # A link of a page's head to a feed, the address it names, and what that address answers.
    element: str
    href: str
    answer: str


class PageNamingFeeds(BaseModel):
    address: str
    base: str | None
    head: str
    named: list[NamedFeed]


@st.composite
def named_feeds(draw: st.DrawFn, number: int) -> NamedFeed:
    href = draw(st.sampled_from(HREF_FORMS)).format(n=number)
    words = ["alternate", *draw(st.lists(st.sampled_from(REL_WORDS), max_size=2, unique=True))]
    rel = draw(st.sampled_from((" ", "\t", "  "))).join(
        any_case(draw, word) for word in draw(st.permutations(words))
    )
    kind = any_case(draw, draw(st.sampled_from(FEED_TYPES)))
    element = f'<link rel="{rel}" type="{kind}" href="{href}" title="Feed {number}">'
    answer = draw(st.sampled_from([*FEEDS, *NOT_FEEDS, MISSING]))
    return NamedFeed(element=element, href=href, answer=answer)


@st.composite
def pages_naming_feeds(draw: st.DrawFn) -> PageNamingFeeds:
    # A page whose head names feeds among other links, a feed among the first three of them.
    named = [draw(named_feeds(number)) for number in range(draw(st.integers(1, 5)))]
    if not any(feed.answer in FEEDS for feed in named[:NAMED_FEEDS_TRIED]):
        found = draw(st.integers(0, min(len(named), NAMED_FEEDS_TRIED) - 1))
        named[found] = named[found].model_copy(
            update={"answer": draw(st.sampled_from(sorted(FEEDS)))}
        )
    elements = [feed.element for feed in named]
    for decoy in draw(st.lists(st.sampled_from(DECOYS), max_size=3)):
        elements.insert(draw(st.integers(0, len(elements))), decoy)
    base = draw(st.sampled_from(PAGE_BASES))
    head = ("" if base is None else f'<base href="{base}">') + "".join(elements)
    return PageNamingFeeds(address=draw(st.sampled_from(PAGES)), base=base, head=head, named=named)


def named_address(page: PageNamingFeeds, href: str) -> str:
    return urljoin(urljoin(page.address, page.base or ""), href)


@EXAMPLES
@given(page=pages_naming_feeds())
def test_a_page_leads_to_the_first_feed_it_names_that_reads_as_a_feed(
    feed_sites: FeedSites, page: PageNamingFeeds
) -> None:
    feed_sites.forget()
    feed_sites.serve(page.address, answered(page_naming(page.head), kind_of(HTML)))
    for feed in page.named:
        serve_answer(feed_sites, named_address(page, feed.href), feed.answer)
    first = next(n for n, feed in enumerate(page.named) if feed.answer in FEEDS)
    at = named_address(page, page.named[first].href)
    assert_found(
        feed_sites, page.address, RSSHUB, at, page.named[first].answer, f"the page {page.head!r}"
    )
    tried = [page.address, *(named_address(page, feed.href) for feed in page.named[: first + 1])]
    assert feed_sites.fetches == tried, (
        f"finding the feed of the page {page.head!r} fetched {feed_sites.fetches}, not the page "
        f"and the feeds it names in their order up to the first that reads: {tried}"
    )


def test_a_page_has_no_more_than_three_of_the_feeds_it_names_tried(
    feed_sites: FeedSites,
) -> None:
    page = "https://blog.test/"
    named = [f"https://blog.test/broken/{number}.xml" for number in range(4)]
    head = "".join(f'<link rel="alternate" type="{RSS_KIND}" href="{href}">' for href in named)
    feed_sites.serve(page, answered(page_naming(head), kind_of(HTML)))
    feed_sites.serve(named[-1], answered(rss_feed(named[-1]), kind_of(RSS_KIND)))
    refusal(page, RSSHUB, "a page whose first three feeds do not read")
    assert named[-1] not in feed_sites.fetches, (
        f"a page that names four feeds, of which three do not read, had the fourth tried: "
        f"{feed_sites.fetches}"
    )


# D4: shapes known by their address.

channel_ids = st.text(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-", min_size=22, max_size=22
).map(lambda tail: f"UC{tail}")
YOUTUBE_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com")
YOUTUBE_FEED = "https://www.youtube.com/feeds/videos.xml?{query}"
VIDEOS = ["Vid000000A1", "Vid000000B2"]


@EXAMPLES
@given(
    channel=channel_ids,
    host=st.sampled_from(YOUTUBE_HOSTS),
    after=st.sampled_from(("", "/videos", "/featured", "?view=0")),
)
def test_a_youtube_channel_by_its_id_leads_to_youtubes_feed_without_its_page(
    feed_sites: FeedSites, channel: str, host: str, after: str
) -> None:
    feed_sites.forget()
    at = YOUTUBE_FEED.format(query=f"channel_id={channel}")
    feed_sites.serve(at, answered(youtube_feed(channel, VIDEOS), kind_of(ATOM_KIND)))
    address = f"https://{host}/channel/{channel}{after}"
    discovered = discovered_feed(address, RSSHUB)
    assert (feed_sites.fetches, discovered.address) == ([at], at), (
        f"the channel {address} was found at {discovered.address} with the fetches "
        f"{feed_sites.fetches}, not at YouTube's feed {at} alone"
    )


playlist_ids = st.text(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-", min_size=16, max_size=32
).map(lambda tail: f"PL{tail}")


@EXAMPLES
@given(
    playlist=playlist_ids,
    host=st.sampled_from(YOUTUBE_HOSTS),
    shape=st.sampled_from(("/playlist?list={list}", "/watch?v=Vid000000A1&list={list}")),
)
def test_a_youtube_playlist_leads_to_youtubes_feed_of_it_without_its_page(
    feed_sites: FeedSites, playlist: str, host: str, shape: str
) -> None:
    feed_sites.forget()
    at = YOUTUBE_FEED.format(query=f"playlist_id={playlist}")
    feed_sites.serve(
        at, answered(youtube_feed("UCa1b2c3d4e5f6g7h8i9j0kL", VIDEOS), kind_of(ATOM_KIND))
    )
    address = f"https://{host}{shape.format(list=playlist)}"
    discovered = discovered_feed(address, RSSHUB)
    assert (feed_sites.fetches, discovered.address) == ([at], at), (
        f"the playlist {address} was found at {discovered.address} with the fetches "
        f"{feed_sites.fetches}, not at YouTube's feed {at} alone"
    )


APPLE_LOOKUP = "https://itunes.apple.com/lookup?id={show}&entity=podcast"


def lookup_answer(feeds: list[str]) -> bytes:
    results = [{"kind": "podcast", "collectionId": 1, "feedUrl": feed} for feed in feeds]
    return json.dumps({"resultCount": len(results), "results": results}).encode()


@EXAMPLES
@given(
    show=st.integers(1, 10**10).map(str),
    country=st.sampled_from(("us/", "jp/", "cn/", "")),
    slug=st.sampled_from(("night-trains", "%E5%A4%9C%E8%A1%8C%E5%88%97%E8%BB%8A", "a")),
    episode=st.sampled_from(("", "?i=1000612345678")),
)
def test_a_show_on_apple_podcasts_leads_to_the_feed_apples_lookup_gives(
    feed_sites: FeedSites, show: str, country: str, slug: str, episode: str
) -> None:
    feed_sites.forget()
    at = f"https://shows.test/{show}/feed.xml"
    feed_sites.serve(
        APPLE_LOOKUP.format(show=show), answered(lookup_answer([at]), kind_of("text/javascript"))
    )
    feed_sites.serve(at, answered(rss_feed(at), kind_of(RSS_KIND)))
    address = f"https://podcasts.apple.com/{country}podcast/{slug}/id{show}{episode}"
    assert_found(feed_sites, address, RSSHUB, at, "RSS", f"the show {address}")
    assert feed_sites.fetches == [APPLE_LOOKUP.format(show=show), at], (
        f"the show {address} was found with the fetches {feed_sites.fetches}, not Apple's "
        "lookup and the feed alone"
    )


HANDLES = ("@nighttrains", "@夜行列車チャンネル", "c/NightTrains", "user/nighttrains")
CHANNEL = "UCa1b2c3d4e5f6g7h8i9j0kL"
CHANNEL_FEED = YOUTUBE_FEED.format(query=f"channel_id={CHANNEL}")
# What a channel's page carries: the feed it names, or only its canonical link, which holds the
# channel's id.
HANDLE_PAGES = {
    "names its feed": f'<link rel="alternate" type="{RSS_KIND}" title="RSS" href="{CHANNEL_FEED}">'
    f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL}">',
    "carries the channel's id": f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL}">',
}


@pytest.mark.parametrize("handle", HANDLES)
@pytest.mark.parametrize("carried", sorted(HANDLE_PAGES))
def test_a_youtube_channel_by_its_handle_is_found_through_its_page(
    feed_sites: FeedSites, handle: str, carried: str
) -> None:
    address = f"https://www.youtube.com/{handle}"
    feed_sites.serve(address, answered(page_naming(HANDLE_PAGES[carried]), kind_of(HTML)))
    feed_sites.serve(CHANNEL_FEED, answered(youtube_feed(CHANNEL, VIDEOS), kind_of(ATOM_KIND)))
    discovered = discovered_feed(address, RSSHUB)
    assert discovered.address == CHANNEL_FEED, (
        f"the channel {address}, whose page {carried}, was found at {discovered.address}, not at "
        f"its feed {CHANNEL_FEED}"
    )


CONSENT = "https://consent.youtube.com/m?continue=https%3A%2F%2Fwww.youtube.com%2F%40nighttrains"
CONSENT_PAGE = page_naming("").replace(b"Night trains", b"Before you continue to YouTube")


def test_a_youtube_page_that_asks_for_consent_tells_the_reader_to_give_the_channels_id(
    feed_sites: FeedSites,
) -> None:
    address = "https://www.youtube.com/@nighttrains"
    feed_sites.serve(address, moved(CONSENT, 302))
    feed_sites.serve(CONSENT, answered(CONSENT_PAGE, kind_of(HTML)))
    told = refusal(address, RSSHUB, f"the channel {address} that YouTube answered with consent")
    assert address in told and "channel" in told and " id" in told, (
        f"a channel whose page asks for consent was refused as {told!r}, which does not tell the "
        "reader to give the channel's address with its id"
    )
    assert feed_sites.fetches == [address], (
        f"finding the feed of a channel whose page asks for consent fetched {feed_sites.fetches}, "
        "where YouTube's pages have no paths of feeds tried"
    )


def test_a_youtube_page_that_names_no_feed_has_no_paths_of_feeds_tried(
    feed_sites: FeedSites,
) -> None:
    address = "https://www.youtube.com/@nighttrains"
    feed_sites.serve(address, answered(page_naming(""), kind_of(HTML)))
    for path in COMMON_FEED_PATHS:
        feed_sites.serve(
            f"https://www.youtube.com{path}", answered(rss_feed(path), kind_of(RSS_KIND))
        )
    refusal(address, RSSHUB, f"the channel {address} whose page carries nothing of its feed")
    assert feed_sites.fetches == [address], (
        f"finding the feed of a YouTube page that names none fetched {feed_sites.fetches}, where "
        "YouTube's pages have no paths of feeds tried"
    )


# D5: a site that names no feed.


@EXAMPLES
@given(
    page=st.sampled_from(PAGES),
    path=st.sampled_from(COMMON_FEED_PATHS),
    form=st.sampled_from(sorted(FEEDS)),
    decoys=st.lists(st.sampled_from(DECOYS), max_size=3),
)
def test_a_page_that_names_no_feed_leads_to_a_feed_at_a_path_feeds_commonly_live_at(
    feed_sites: FeedSites, page: str, path: str, form: str, decoys: list[str]
) -> None:
    feed_sites.forget()
    feed_sites.serve(page, answered(page_naming("".join(decoys)), kind_of(HTML)))
    site = urlsplit(page)
    at = f"{site.scheme}://{site.netloc}{path}"
    serve_answer(feed_sites, at, form)
    assert_found(feed_sites, page, RSSHUB, at, form, f"the page {page}, which names no feed")
    assert len(feed_sites.requests) <= MOST_REQUESTS, (
        f"finding the feed at {at} made the requests {feed_sites.asked()}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="D5 as widened, not yet built: a page whose named feeds do not read has no paths of "
    "feeds tried",
)
@FAULT_EXAMPLES
@given(
    page=st.sampled_from(PAGES),
    broken=st.lists(st.sampled_from([*NOT_FEEDS, MISSING]), min_size=1, max_size=3),
)
def test_a_page_whose_feeds_do_not_read_leads_to_a_feed_at_a_path_feeds_commonly_live_at(
    feed_sites: FeedSites, page: str, broken: list[str]
) -> None:
    feed_sites.forget()
    named = [f"https://blog.test/broken/{number}.xml" for number in range(len(broken))]
    head = "".join(f'<link rel="alternate" type="{RSS_KIND}" href="{href}">' for href in named)
    feed_sites.serve(page, answered(page_naming(head), kind_of(HTML)))
    for href, said in zip(named, broken, strict=True):
        serve_answer(feed_sites, href, said)
    site = urlsplit(page)
    common = [f"{site.scheme}://{site.netloc}{path}" for path in COMMON_FEED_PATHS]
    for path in common:
        serve_answer(feed_sites, path, "RSS")
    discovered = discovered_feed(page, RSSHUB)
    assert discovered.address in common, (
        f"a page whose {len(broken)} feeds do not read, at a site that serves a feed at every "
        f"path feeds commonly live at, led to {discovered.address}"
    )


# D6: no feed.

NO_FEED = {
    "a page that names no feed": (page_naming(""), HTML),
    "a picture": (b"\x89PNG\r\n\x1a\n" + bytes(64), "image/png"),
    "a document of PDF": (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + bytes(64), "application/pdf"),
}


@pytest.mark.parametrize("found", sorted(NO_FEED))
def test_an_address_that_leads_to_no_feed_is_told_with_what_it_is_and_what_to_give(
    feed_sites: FeedSites, found: str
) -> None:
    address = "https://blog.test/about"
    content, kind = NO_FEED[found]
    feed_sites.serve(address, answered(content, kind_of(kind)))
    told = refusal(address, RSSHUB, f"{found} at {address}")
    assert address in told and "feed" in told and "RSSHub" in told, (
        f"{found} was refused as {told!r}, which does not name the address and say that the "
        "feed's own address or a route of RSSHub can be given instead"
    )


def test_a_page_that_names_no_feed_is_told_as_such(feed_sites: FeedSites) -> None:
    address = "https://blog.test/about"
    feed_sites.serve(address, answered(page_naming(""), kind_of(HTML)))
    told = refusal(address, RSSHUB, f"a page at {address} that names no feed")
    picture = "https://blog.test/logo.png"
    feed_sites.serve(picture, answered(NO_FEED["a picture"][0], kind_of("image/png")))
    told_of_picture = refusal(picture, RSSHUB, f"a picture at {picture}")
    assert "page" in told and told.replace(address, "") != told_of_picture.replace(picture, ""), (
        f"a page that names no feed was refused as {told!r} and a picture as {told_of_picture!r}: "
        "what the address is is not told"
    )


def refusals(sites: FeedSites) -> dict[str, tuple[str, str]]:
    # Addresses that lead to no feed, each with the instance of RSSHub it is found with, served.
    sites.serve("https://blog.test/about", answered(page_naming(""), kind_of(HTML)))
    sites.serve(
        "https://blog.test/logo.png", answered(NO_FEED["a picture"][0], kind_of("image/png"))
    )
    sites.serve(
        APPLE_LOOKUP.format(show="7"), answered(lookup_answer([]), kind_of("text/javascript"))
    )
    sites.serve("https://www.youtube.com/@nighttrains", moved(CONSENT, 302))
    sites.serve(CONSENT, answered(CONSENT_PAGE, kind_of(HTML)))
    sites.serve(instance_route(RSSHUB, "night/trains"), failing(httpx.ConnectError, []))
    return {
        "a page that names no feed": ("https://blog.test/about", RSSHUB),
        "a picture": ("https://blog.test/logo.png", RSSHUB),
        "a show Apple names no feed of": ("https://podcasts.apple.com/us/podcast/x/id7", RSSHUB),
        "a channel whose page asks for consent": ("https://www.youtube.com/@nighttrains", RSSHUB),
        "a route of an RSSHub that does not answer": (f"{RSSHUB_FORM}night/trains", RSSHUB),
        "an address of no web": ("ftp://files.test/feed.xml", RSSHUB),
    }


FORM_NOT_SHOWN = pytest.mark.xfail(
    strict=True,
    reason="D6 as amended, not yet built: the refusal names RSSHub but does not show the form "
    "rsshub://",
)
REFUSED_KINDS = [
    pytest.param("a page that names no feed", marks=FORM_NOT_SHOWN),
    pytest.param("a picture", marks=FORM_NOT_SHOWN),
    pytest.param("a show Apple names no feed of", marks=FORM_NOT_SHOWN),
    pytest.param("a channel whose page asks for consent", marks=FORM_NOT_SHOWN),
    pytest.param("a route of an RSSHub that does not answer", marks=FORM_NOT_SHOWN),
    "an address of no web",
]


@pytest.mark.parametrize("refused", REFUSED_KINDS)
def test_every_refusal_of_discovery_shows_the_form_of_a_route_of_rsshub(
    feed_sites: FeedSites, refused: str
) -> None:
    address, rsshub = refusals(feed_sites)[refused]
    told = refusal(address, rsshub, refused)
    assert RSSHUB_FORM in told, (
        f"{refused} was refused as {told!r}, which does not show the form {RSSHUB_FORM}"
    )


# D7: bounds.


def test_a_page_beyond_the_bytes_allowed_is_oversize(feed_sites: FeedSites) -> None:
    page = "https://blog.test/huge"
    padding = b"<!--" + b" " * LOWERED_PAGE_BYTES + b"-->"
    feed_sites.serve(page, answered(page_naming("") + padding, kind_of(HTML)))
    told = refusal(page, RSSHUB, f"a page of more than {LOWERED_PAGE_BYTES} bytes")
    assert told == OVERSIZE_SOURCE.format(source=page), (
        f"a page beyond the bytes allowed was refused as {told!r}"
    )


def test_a_feed_beyond_the_bytes_a_page_may_hold_is_found(feed_sites: FeedSites) -> None:
    # A feed may hold more than a page: it is fetched as a feed.
    at = "https://blog.test/feed.xml"
    padding = b"<!--" + b" " * ((LOWERED_PAGE_BYTES + LOWERED_FEED_BYTES) // 2) + b"-->"
    content = rss_feed(at).replace(b"</channel>", padding + b"</channel>")
    feed_sites.serve(at, answered(content, kind_of(RSS_KIND)))
    discovered = discovered_feed(at, RSSHUB)
    assert discovered.address == at, f"a feed of {len(content)} bytes was found as {discovered}"


def test_a_page_that_trickles_beyond_the_time_allowed_is_overdue(feed_sites: FeedSites) -> None:
    page = "https://blog.test/slow"
    feed_sites.serve(
        page, trickling(b"<!doctype html><html><body>", HTML, 5 * LOWERED_PAGE_SECONDS)
    )
    began = time.monotonic()
    told = refusal(page, RSSHUB, "a page that trickles")
    took = time.monotonic() - began
    assert told == OVERDUE_SOURCE.format(source=page), (
        f"a page that trickles beyond the time allowed was refused as {told!r}"
    )
    assert took <= LOWERED_PAGE_SECONDS + 1.0, (
        f"a page that trickles was given up after {took:.1f}s"
    )


@pytest.mark.xfail(
    strict=True,
    reason="D7 as amended, not yet built: a redirect is not counted among the requests one "
    "discovery may make",
)
def test_one_discovery_makes_no_more_requests_than_allowed_redirects_counted(
    feed_sites: FeedSites,
) -> None:
    # A page reached through two redirects names three feeds, each through a redirect, none of
    # which reads, at a site with no feed at the paths feeds commonly live at.
    page = "https://blog.test/page"
    feed_sites.serve("https://blog.test/", moved("https://blog.test/moved"))
    feed_sites.serve("https://blog.test/moved", moved(page))
    named = [f"https://blog.test/feeds/{number}" for number in range(NAMED_FEEDS_TRIED)]
    head = "".join(f'<link rel="alternate" type="{RSS_KIND}" href="{href}">' for href in named)
    feed_sites.serve(page, answered(page_naming(head), kind_of(HTML)))
    for href in named:
        feed_sites.serve(href, moved(f"{href}/moved"))
        feed_sites.serve(f"{href}/moved", answered(page_naming(""), kind_of(HTML)))
    refusal("https://blog.test/", RSSHUB, "a page whose feeds do not read")
    assert len(feed_sites.requests) <= MOST_REQUESTS, (
        f"one discovery made {len(feed_sites.requests)} requests, redirects counted: "
        f"{feed_sites.asked()}"
    )


def test_one_discovery_takes_no_longer_than_allowed_in_all(feed_sites: FeedSites) -> None:
    # A page and the feeds it names each take less than a fetch may, and all of them together
    # more than a discovery may.
    page = "https://blog.test/page"
    named = [f"https://blog.test/feeds/{number}.xml" for number in range(NAMED_FEEDS_TRIED)]
    head = "".join(f'<link rel="alternate" type="{RSS_KIND}" href="{href}">' for href in named)
    opening = page_naming(head)
    feed_sites.serve(page, trickling(opening, HTML, 0.9 * LOWERED_PAGE_SECONDS))
    for href in named:
        feed_sites.serve(href, trickling(b"<html><body>", HTML, 0.9 * LOWERED_PAGE_SECONDS))
    began = time.monotonic()
    refusal(page, RSSHUB, "a page whose feeds trickle and do not read")
    took = time.monotonic() - began
    assert took <= LOWERED_DISCOVERY_SECONDS + OVERRUN_SECONDS, (
        f"one discovery went on for {took:.1f} seconds, beyond the {LOWERED_DISCOVERY_SECONDS} "
        "allowed in all"
    )


LONG_ADDRESS = "https://feeds.test/" + "a" * ADDRESS_CHARACTERS


@pytest.mark.xfail(
    strict=True,
    reason="being mended: a feed moved to an address longer than a feed's address may be ends "
    "in pydantic's ValidationError of Discovered, not in UnreadableFeed",
)
def test_an_address_that_leads_to_one_longer_than_a_feeds_address_may_be_leads_to_no_feed(
    feed_sites: FeedSites,
) -> None:
    feed_sites.serve("https://blog.test/feed", moved(LONG_ADDRESS))
    feed_sites.serve(LONG_ADDRESS, answered(rss_feed("long"), kind_of(RSS_KIND)))
    refusal("https://blog.test/feed", RSSHUB, f"a feed moved to {len(LONG_ADDRESS)} characters")
