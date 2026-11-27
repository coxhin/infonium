import json
import re
import string
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Any
from urllib.parse import quote, quote_plus, unquote, urljoin, urlsplit

import httpx
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from yt_dlp.utils import DownloadError

from infonium.document import OVERDUE_SOURCE, OVERSIZE_SOURCE
from infonium.subscriptions.discovery import discovered_feed, polled_route, served_at
from infonium.subscriptions.feed import ADDRESS_CHARACTERS, UnreadableFeed, read_feed
from infonium.subscriptions.fetcher import Changed, Validators, fetched_feed
from tests.feed_server import REJECTED, RSSHUB_ERROR
from tests.feed_sites import (
    CONTENT_TYPE,
    HTML,
    LOWERED_DISCOVERY_SECONDS,
    LOWERED_FEED_BYTES,
    LOWERED_FEED_SECONDS,
    LOWERED_PAGE_BYTES,
    LOWERED_PAGE_SECONDS,
    Answer,
    FeedSites,
    answered,
    broken_off,
    failing,
    kind_of,
    moved,
    trickling,
)
from tests.feed_writing import FAULT_EXAMPLES, RSS_KIND, youtube_feed
from tests.yt_dlp_standin import channel_answer

# Judgments of discovered_feed (D1 to D7 and their amendments), finding feeds on stand-in sites
# that each test scripts.
# A reading raises no warning of any kind (G16), in a poll and a discovery too.
pytestmark = pytest.mark.filterwarnings("error")

RSSHUB = "https://rsshub.test"
RSSHUB_SETTING = "INFONIUM_RSSHUB_BASE_URL"
RSSHUB_FORM = "rsshub://"
ATOM_KIND = "application/atom+xml"
JSON_FEED_KIND = "application/feed+json"
ETAG = "etag"
LAST_MODIFIED = "last-modified"
# The paths at a site that feeds commonly live at, and the most requests one discovery makes.
COMMON_FEED_PATHS = ("/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/index.xml")
MOST_REQUESTS = 8
NAMED_FEEDS_TRIED = 3
OVERRUN_SECONDS = 0.6
EXAMPLES = settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture])
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


def assert_found(address: str, rsshub: str, at: str, said: str, what: str) -> None:
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


def refusal(address: str, rsshub: str, what: str, key: str | None = None) -> str:
    with pytest.raises(UnreadableFeed) as refused:
        discovered = discovered_feed(address, rsshub, key)
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
    assert feed_sites.asked() == [at], (
        f"the route {slashes}{route}{query} of RSSHub at {instance} was fetched at "
        f"{feed_sites.asked()}, not at {at}, joined with one slash"
    )
    expected = Changed(feed=read_feed(rss_feed(at), RSS_KIND, at), validators=Validators())
    assert discovered.fetched == expected, (
        f"the route {route} of RSSHub was found with {discovered.fetched}, not its feed as fetched"
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


@pytest.mark.parametrize("scheme", SCHEME_CASES)
def test_an_rsshub_route_is_told_by_its_scheme_in_any_case(
    feed_sites: FeedSites, scheme: str
) -> None:
    at = instance_route(RSSHUB, "github/issue/night/trains")
    feed_sites.serve(at, answered(rss_feed(at), kind_of(RSS_KIND)))
    discovered = discovered_feed(f"{scheme}github/issue/night/trains", RSSHUB)
    assert feed_sites.asked() == [at], (
        f"the address {scheme}github/issue/night/trains was fetched at {feed_sites.asked()}, not "
        f"as a route of RSSHub at {at}"
    )
    assert discovered.fetched.feed.title == at, f"the route was found with {discovered}"
    assert discovered.address == f"{RSSHUB_FORM}github/issue/night/trains", (
        f"the address {scheme}github/issue/night/trains was found at {discovered.address}, not at "
        f"{RSSHUB_FORM}github/issue/night/trains"
    )


web_addresses = st.builds(
    lambda scheme, host, path: f"{scheme}://{host}/{path}",
    st.sampled_from(("http", "https")),
    st.sampled_from(("blog.test", "feeds.example.test")),
    routes,
)


@given(
    address=web_addresses,
    instance=instances,
    route=routes,
    slashes=leading_slashes,
    scheme=st.sampled_from((RSSHUB_FORM, *SCHEME_CASES)),
)
def test_the_web_address_a_feeds_address_stands_for_is_itself_or_its_route_at_the_instance(
    address: str, instance: str, route: str, slashes: str, scheme: str
) -> None:
    assert served_at(address, instance) == address, (
        f"the web address {address} was taken to stand for {served_at(address, instance)}"
    )
    written = f"{scheme}{slashes}{route}"
    assert served_at(written, instance) == instance_route(instance, route), (
        f"the route {written} was taken to stand for {served_at(written, instance)}, not the "
        f"route at RSSHub at {instance}, joined with one slash: {instance_route(instance, route)}"
    )


# N7 and N12: an instance that does not answer, at every step that asks it.
SILENT_KINDS = (httpx.ConnectError, httpx.ConnectTimeout)
RSSHUB_ENGINE = "engines/rsshub"


def assert_told_rsshub_silent(told: str, instance: str, what: str) -> None:
    assert all(
        said in told for said in (instance.rstrip("/"), "RSSHub", RSSHUB_SETTING, RSSHUB_ENGINE)
    ), (
        f"{what} was told as {told!r}, which does not say that RSSHub did not answer at "
        f"{instance}, with {RSSHUB_SETTING} and {RSSHUB_ENGINE}"
    )
    assert RSSHUB_FORM not in told, (
        f"{what} was told as {told!r}, which advises giving a route of RSSHub that cannot be read"
    )


@EXAMPLES
@given(instance=instances, kind=st.sampled_from(SILENT_KINDS))
def test_an_rsshub_instance_that_does_not_answer_a_discovery_of_a_route_is_told_as_such(
    feed_sites: FeedSites, instance: str, kind: type[httpx.TransportError]
) -> None:
    feed_sites.forget()
    feed_sites.serve(instance_route(instance, "night/trains"), failing(kind, []))
    what = f"a discovery of a route, RSSHub at {instance} failing with {kind.__name__}"
    told = refusal(f"{RSSHUB_FORM}night/trains", instance, what)
    assert_told_rsshub_silent(told, instance, what)


@EXAMPLES
@given(instance=instances, kind=st.sampled_from(SILENT_KINDS))
def test_an_rsshub_instance_that_does_not_answer_a_poll_of_a_route_is_told_as_such(
    feed_sites: FeedSites, instance: str, kind: type[httpx.TransportError]
) -> None:
    feed_sites.forget()
    feed_sites.serve(instance_route(instance, "night/trains"), failing(kind, []))
    what = f"a poll of a route, RSSHub at {instance} failing with {kind.__name__}"
    with pytest.raises(UnreadableFeed) as refused:
        polled = polled_route(f"{RSSHUB_FORM}night/trains", Validators(), instance, None)
        pytest.fail(f"{what} gave {polled}")
    assert_told_rsshub_silent(str(refused.value), instance, what)


# What an instance answers a route with: RSSHub's page of an error (tests/feed_server.py), and
# an answer it breaks off.
ROUTE_FAILURE = "The night line north is not in the timetable"
NIGHT_ROUTE = "/nighttrains/line/north"
ROUTE_STEPS: dict[str, Callable[[str], object]] = {
    "a discovery": lambda route: discovered_feed(route, RSSHUB),
    "a poll": lambda route: polled_route(route, Validators(), RSSHUB, None),
}


@pytest.mark.parametrize("step", sorted(ROUTE_STEPS))
def test_a_route_rsshub_answers_with_an_error_is_told_with_the_route_and_what_rsshub_answered(
    feed_sites: FeedSites, step: str
) -> None:
    failed = RSSHUB_ERROR.format(message=ROUTE_FAILURE, route=NIGHT_ROUTE).encode()
    feed_sites.serve(f"{RSSHUB}{NIGHT_ROUTE}", answered(failed, kind_of(HTML), 503))
    with pytest.raises(UnreadableFeed) as refused:
        made = ROUTE_STEPS[step](f"{RSSHUB_FORM}{NIGHT_ROUTE.lstrip('/')}")
        pytest.fail(f"{step} of a route that RSSHub fails gave {made}")
    told = str(refused.value)
    assert NIGHT_ROUTE.lstrip("/") in told and (ROUTE_FAILURE in told or "503" in told), (
        f"{step} of a route RSSHub answered 503 was told as {told!r}, which does not name the "
        f"route and what RSSHub answered (503, {ROUTE_FAILURE!r})"
    )


@pytest.mark.parametrize("step", sorted(ROUTE_STEPS))
def test_a_route_rsshub_does_not_finish_answering_is_told_as_such(
    feed_sites: FeedSites, step: str
) -> None:
    opening = rss_feed("x").removesuffix(b"</channel></rss>")
    feed_sites.serve(f"{RSSHUB}{NIGHT_ROUTE}", broken_off(opening, RSS_KIND, httpx.ReadError, []))
    with pytest.raises(UnreadableFeed) as refused:
        made = ROUTE_STEPS[step](f"{RSSHUB_FORM}{NIGHT_ROUTE.lstrip('/')}")
        pytest.fail(f"{step} of a route that RSSHub breaks off gave {made}")
    told = str(refused.value)
    assert NIGHT_ROUTE.lstrip("/") in told and RSSHUB in told and "finish" in told, (
        f"{step} of a route RSSHub broke off was told as {told!r}, not as RSSHub at {RSSHUB} "
        "not finishing its answer of the route"
    )


# N4: a route is written one way.
ROUTE_LETTERS = "abcdefghijklmnopqrstuvwxyz0123456789-_@夜行列车東京"
route_segments = st.text(ROUTE_LETTERS, min_size=1, max_size=8)
query_values = st.text("abcdefghijklmnopqrstuvwxyz0123456789夜行", min_size=1, max_size=6)


class RouteSaid(BaseModel):
    # A route by its segments and the query it carries, before it is spelled.
    segments: list[str]
    query: list[tuple[str, str]]


said_routes = st.builds(
    RouteSaid,
    segments=st.lists(route_segments, min_size=1, max_size=4),
    query=st.lists(st.tuples(st.sampled_from(("limit", "filter", "q")), query_values), max_size=2),
)


@st.composite
def spellings(draw: st.DrawFn, said: RouteSaid) -> str:
    # One way a reader may write the route: any character raw or escaped, in either case of hex,
    # slashes doubled, a slash at the end, the scheme in any case, and a fragment.
    def spelled(text: str) -> str:
        return "".join(
            letter
            if not draw(st.booleans())
            else "".join(
                draw(st.sampled_from(("%{:02X}", "%{:02x}"))).format(byte)
                for byte in letter.encode()
            )
            for letter in text
        )

    apart = draw(st.sampled_from(("/", "//")))
    path = apart.join(spelled(segment) for segment in said.segments)
    query = "&".join(f"{name}={spelled(value)}" for name, value in said.query)
    scheme = draw(st.sampled_from(("rsshub://", "RSSHub://", "rsshub:///", "RSSHUB:////")))
    ending = draw(st.sampled_from(("", "/")))
    fragment = draw(st.sampled_from(("", "#top", "#夜")))
    return f"{scheme}{path}{ending}{'?' + query if query else ''}{fragment}"


def found_route(sites: FeedSites, written: str) -> str:
    sites.serve_everywhere_at(
        httpx.URL(RSSHUB).host, answered(rss_feed("a route"), kind_of(RSS_KIND))
    )
    return discovered_feed(written, RSSHUB).address


@EXAMPLES
@given(data=st.data(), said=said_routes)
def test_two_spellings_of_one_route_are_one_address_and_writing_it_again_changes_nothing(
    feed_sites: FeedSites, data: st.DataObject, said: RouteSaid
) -> None:
    feed_sites.forget()
    first, second = data.draw(spellings(said)), data.draw(spellings(said))
    found = found_route(feed_sites, first)
    assert found_route(feed_sites, second) == found, (
        f"the route spelled {first!r} and {second!r} was found at two addresses: {found!r} and "
        f"{found_route(feed_sites, second)!r}"
    )
    assert found_route(feed_sites, found) == found, (
        f"the route found at {found!r} was found, written again, at "
        f"{found_route(feed_sites, found)!r}"
    )
    assert "#" not in found, f"the route {first!r} was found with its fragment: {found!r}"


ONE_ROUTE = {
    "a handle raw and escaped": (
        "rsshub://youtube/user/@veritasium",
        "rsshub://youtube/user/%40veritasium",
        "RSSHub:////youtube//user/%40veritasium/",
    ),
    "Chinese raw and escaped": (
        "rsshub://bilibili/user/夜行列车",
        "rsshub://bilibili/user/%E5%A4%9C%E8%A1%8C%E5%88%97%E8%BD%A6",
    ),
    "a query with a fragment": (
        "rsshub://night/trains?limit=2#frag",
        "rsshub://night/trains?limit=2",
    ),
}


@pytest.mark.parametrize("case", sorted(ONE_ROUTE))
def test_the_ruled_spellings_of_one_route_are_one_address(feed_sites: FeedSites, case: str) -> None:
    found = {written: found_route(feed_sites, written) for written in ONE_ROUTE[case]}
    assert len(set(found.values())) == 1, f"{case} were found at several addresses: {found}"


def test_a_plus_and_its_escape_in_a_routes_query_are_two_addresses(feed_sites: FeedSites) -> None:
    plus = found_route(feed_sites, "rsshub://night/trains?q=north+south")
    escaped = found_route(feed_sites, "rsshub://night/trains?q=north%2Bsouth")
    assert plus != escaped and plus.endswith("?q=north+south"), (
        f"a route's query with + and with %2B was found at {plus!r} and {escaped!r}, where they "
        "are two addresses and the query is kept"
    )


# RSSHub's rules: after everything else found no feed, the rules the instance publishes for the
# address's site (lib/api/radar/rules/utils.ts): by the site's domain, then its subdomain ("."
# for none), a list of rules, each with the paths it matches and the route it leads to.

RULES = f"{RSSHUB}/api/radar/rules"
RADAR_DOCS = "https://docs.rsshub.test/routes/nighttrains"
RULES_KIND = "application/json"
RULES_LIMIT_BYTES = 8 * 1024**2


def rule(title: str, source: str, target: str) -> dict[str, str]:
    return {"title": title, "docs": RADAR_DOCS, "source": source, "target": target}


def radar_rules(subdomain: str, rules: list[dict[str, str]]) -> bytes:
    listed = [rule | {"source": [rule["source"]]} for rule in rules]
    return json.dumps({"nighttrains.test": {"_name": "Night trains", subdomain: listed}}).encode()


LINE_RULES = [
    rule("Station", "/station/:name", "/nighttrains/station/:name"),
    rule("Line", "/line/:line", "/nighttrains/line/:line"),
]
SITE_HOSTS = {".": "nighttrains.test", "www": "www.nighttrains.test"}
NIGHT_PAGE = "https://nighttrains.test/line/north"


def serve_a_page_with_no_feed(sites: FeedSites, page: str, rules: bytes) -> None:
    sites.serve(page, answered(page_naming(""), kind_of(HTML)))
    sites.serve(RULES, answered(rules, kind_of(RULES_KIND)))


def asked_of_rsshub(sites: FeedSites) -> list[str]:
    return [asked for asked in sites.asked() if asked.startswith(RSSHUB)]


@EXAMPLES
@given(subdomain=st.sampled_from(sorted(SITE_HOSTS)), line=routes.map(lambda r: r.split("/")[0]))
def test_an_address_one_of_rsshubs_rules_matches_leads_to_that_routes_feed_kept_as_a_route(
    feed_sites: FeedSites, subdomain: str, line: str
) -> None:
    feed_sites.forget()
    page = f"https://{SITE_HOSTS[subdomain]}/line/{line}"
    serve_a_page_with_no_feed(feed_sites, page, radar_rules(subdomain, LINE_RULES))
    at = instance_route(RSSHUB, f"nighttrains/line/{line}")
    feed_sites.serve(at, answered(rss_feed(at), kind_of(RSS_KIND)))
    discovered = discovered_feed(page, RSSHUB)
    route = f"{RSSHUB_FORM}nighttrains/line/{line}"
    assert discovered.address == route, (
        f"the page {page}, which names no feed and which RSSHub's rule of lines matches, was found "
        f"at {discovered.address}, not at the route {route}"
    )
    expected = Changed(feed=read_feed(rss_feed(at), RSS_KIND, at), validators=Validators())
    assert discovered.fetched == expected, (
        f"the route {route} was found with {discovered.fetched}, not its feed as fetched at {at}"
    )


def test_of_rules_that_match_alike_the_one_that_matches_best_is_tried(
    feed_sites: FeedSites,
) -> None:
    rules = [
        rule("By line", "/line/:line", "/nighttrains/byline/:line"),
        rule("North", "/line/north", "/nighttrains/north"),
    ]
    serve_a_page_with_no_feed(feed_sites, NIGHT_PAGE, radar_rules(".", rules))
    for target in ("/nighttrains/byline/north", "/nighttrains/north"):
        feed_sites.serve(f"{RSSHUB}{target}", answered(rss_feed(target), kind_of(RSS_KIND)))
    discovered = discovered_feed(NIGHT_PAGE, RSSHUB)
    assert discovered.address == f"{RSSHUB_FORM}nighttrains/north", (
        f"of a rule of the page's own path and a rule of any line, the page was found at "
        f"{discovered.address}, not at the route of the rule that matches it better"
    )


MOST_ROUTES_TOLD = 12


def equal_rules(count: int) -> list[dict[str, str]]:
    return [
        rule(f"Way {number}", "/line/:line", f"/nighttrains/way{number}/:line")
        for number in range(count)
    ]


@pytest.mark.parametrize("count", [2, 5, MOST_ROUTES_TOLD + 2])
def test_where_rules_match_alike_the_refusal_names_their_routes_and_nothing_is_asked_of_them(
    feed_sites: FeedSites, count: int
) -> None:
    serve_a_page_with_no_feed(feed_sites, NIGHT_PAGE, radar_rules(".", equal_rules(count)))
    told = refusal(NIGHT_PAGE, RSSHUB, f"a page {count} rules match alike")
    named = [
        number
        for number in range(count)
        if f"{RSSHUB_FORM}nighttrains/way{number}/north" in told and f"Way {number}" in told
    ]
    assert len(named) == min(count, MOST_ROUTES_TOLD), (
        f"a page that {count} rules match alike was refused as {told!r}, which names the routes "
        f"{named} with their rules' names, not {min(count, MOST_ROUTES_TOLD)} of them"
    )
    rest = count - len(named)
    unnamed = re.sub(r"rsshub://\S+|Way \d+", "", told)
    assert not rest or re.search(rf"\b{rest}\b", unnamed), (
        f"a page that {count} rules match alike was refused as {told!r}, which does not count "
        f"the {rest} routes it does not name"
    )
    assert asked_of_rsshub(feed_sites) == [RULES], (
        f"a discovery of a page that {count} rules match alike asked RSSHub "
        f"{asked_of_rsshub(feed_sites)}, more than its rules"
    )


def test_a_discovery_that_reaches_a_route_of_rsshubs_rules_makes_no_more_requests_than_allowed(
    feed_sites: FeedSites,
) -> None:
    # The site's own means have seven requests, the route one; the rules one more, besides.
    serve_a_page_with_no_feed(feed_sites, NIGHT_PAGE, radar_rules(".", LINE_RULES))
    at = instance_route(RSSHUB, "nighttrains/line/north")
    feed_sites.serve(at, answered(rss_feed(at), kind_of(RSS_KIND)))
    discovered = discovered_feed(NIGHT_PAGE, RSSHUB)
    of_the_site = [asked for asked in feed_sites.asked() if not asked.startswith(RSSHUB)]
    of_rsshub = asked_of_rsshub(feed_sites)
    assert discovered.address == f"{RSSHUB_FORM}nighttrains/line/north", (
        f"the page {NIGHT_PAGE} was found at {discovered.address}, after {feed_sites.asked()}"
    )
    assert len(of_the_site) <= MOST_REQUESTS - 1 and of_rsshub == [RULES, at], (
        f"finding the route of {NIGHT_PAGE} asked its site {of_the_site} and RSSHub {of_rsshub}: "
        "more than seven of the site, or other than the rules once and the route once"
    )


def test_rules_served_behind_a_redirect_are_not_followed(feed_sites: FeedSites) -> None:
    elsewhere = f"{RSSHUB}/elsewhere/rules"
    feed_sites.serve(NIGHT_PAGE, answered(page_naming(""), kind_of(HTML)))
    feed_sites.serve(RULES, moved(elsewhere))
    feed_sites.serve(elsewhere, answered(radar_rules(".", LINE_RULES), kind_of(RULES_KIND)))
    at = instance_route(RSSHUB, "nighttrains/line/north")
    feed_sites.serve(at, answered(rss_feed(at), kind_of(RSS_KIND)))
    refusal(NIGHT_PAGE, RSSHUB, "a page whose rules RSSHub serves behind a redirect")
    assert asked_of_rsshub(feed_sites) == [RULES], (
        f"a discovery whose rules are served behind a redirect asked RSSHub "
        f"{asked_of_rsshub(feed_sites)}, not its rules once"
    )


@pytest.mark.parametrize("kind", SILENT_KINDS)
def test_an_rsshub_instance_that_does_not_answer_a_discovery_at_its_rules_is_told_as_such(
    feed_sites: FeedSites, kind: type[httpx.TransportError]
) -> None:
    feed_sites.serve(NIGHT_PAGE, answered(page_naming(""), kind_of(HTML)))
    feed_sites.serve(RULES, failing(kind, []))
    what = f"a discovery that reached the rules of RSSHub failing with {kind.__name__}"
    told = refusal(NIGHT_PAGE, RSSHUB, what)
    assert asked_of_rsshub(feed_sites) == [RULES], (
        f"{what} asked RSSHub {asked_of_rsshub(feed_sites)}, not its rules once"
    )
    assert_told_rsshub_silent(told, RSSHUB, what)


def test_rules_of_more_than_infonium_reads_are_told_as_rsshubs(feed_sites: FeedSites) -> None:
    padding = json.dumps({"padding": " " * RULES_LIMIT_BYTES})
    serve_a_page_with_no_feed(feed_sites, NIGHT_PAGE, padding.encode())
    told = refusal(NIGHT_PAGE, RSSHUB, "a page whose site RSSHub's rules of 8 MiB and more cover")
    assert "rules" in told and RSSHUB in told and f"{NIGHT_PAGE} holds more" not in told, (
        f"rules of more than {RULES_LIMIT_BYTES} bytes were told as {told!r}, not as RSSHub's "
        "rules holding more than infonium reads"
    )


def test_a_route_of_rsshubs_rules_that_fails_is_told_with_the_route_and_what_rsshub_answered(
    feed_sites: FeedSites,
) -> None:
    serve_a_page_with_no_feed(feed_sites, NIGHT_PAGE, radar_rules(".", LINE_RULES))
    failed = RSSHUB_ERROR.format(message=ROUTE_FAILURE, route=NIGHT_ROUTE).encode()
    feed_sites.serve(f"{RSSHUB}{NIGHT_ROUTE}", answered(failed, kind_of(HTML), 503))
    told = refusal(NIGHT_PAGE, RSSHUB, f"the page {NIGHT_PAGE}, whose route RSSHub fails")
    assert NIGHT_ROUTE.lstrip("/") in told and (ROUTE_FAILURE in told or "503" in told), (
        f"a route of RSSHub's rules that failed was told as {told!r}, which does not name the "
        f"route {NIGHT_ROUTE} and what RSSHub answered (503, {ROUTE_FAILURE!r})"
    )


# T3: a fault of the instance that kept its rules from being asked is told after why the address
# leads to no feed, and after the advice to give the address of a feed.
KEY_SETTING = "INFONIUM_RSSHUB_ACCESS_KEY"
FEED_ADVICE = "address of a feed"


def key_rejected(request: httpx.Request) -> httpx.Response:
    # RSSHub's answer to a request without its key (lib/middleware/access-control.ts).
    path = request.url.path
    page = RSSHUB_ERROR.format(message=REJECTED.format(path=path), route=path).encode()
    return httpx.Response(403, headers={CONTENT_TYPE: HTML}, content=iter([page]))


OVERGROWN_RULES = json.dumps({"padding": " " * RULES_LIMIT_BYTES}).encode()
# Each fault: how the instance answers its rules, the key given, and the words that tell it.
RULES_FAULTS: dict[str, tuple[Answer, str | None, tuple[str, ...]]] = {
    "RSSHub not answering (ConnectError)": (
        failing(httpx.ConnectError, []),
        None,
        (RSSHUB, RSSHUB_SETTING, RSSHUB_ENGINE),
    ),
    "RSSHub not answering (ConnectTimeout)": (
        failing(httpx.ConnectTimeout, []),
        None,
        (RSSHUB, RSSHUB_SETTING, RSSHUB_ENGINE),
    ),
    "a key refused": (key_rejected, "a-key-of-another-instance", (KEY_SETTING,)),
    "a key missing": (key_rejected, None, (KEY_SETTING,)),
    "rules of more than 8 MiB": (
        answered(OVERGROWN_RULES, kind_of(RULES_KIND)),
        None,
        ("rules", RSSHUB),
    ),
}


def rules_asked_with(sites: FeedSites) -> list[str | None]:
    # The key each request for the rules carried, if any.
    return [
        request.url.params.get("key")
        for request in sites.requests
        if str(request.url.copy_with(query=None)) == RULES
    ]


@pytest.mark.parametrize("fault", sorted(RULES_FAULTS))
def test_a_fault_of_rsshub_at_its_rules_is_told_after_why_the_address_leads_to_no_feed(
    feed_sites: FeedSites, fault: str
) -> None:
    # The instance answers every address it is asked, so that the rules are answered where a
    # request with the key asks for them.
    answer, key, said = RULES_FAULTS[fault]
    instance = httpx.URL(RSSHUB).host
    feed_sites.serve(NIGHT_PAGE, answered(page_naming(""), kind_of(HTML)))
    feed_sites.serve_everywhere_at(instance, answered(b"{}", kind_of(RULES_KIND)))
    without_a_rule = refusal(NIGHT_PAGE, RSSHUB, "a page no rule of RSSHub matches", key)
    reason = without_a_rule.split(";")[0]
    feed_sites.forget()
    feed_sites.serve(NIGHT_PAGE, answered(page_naming(""), kind_of(HTML)))
    feed_sites.serve_everywhere_at(instance, answer)
    told = refusal(NIGHT_PAGE, RSSHUB, f"a page whose rules met {fault}", key)
    assert rules_asked_with(feed_sites) == [key], (
        f"a page whose rules met {fault} asked RSSHub {asked_of_rsshub(feed_sites)}, not its "
        f"rules once with the key {key}"
    )
    at_reason = told.find(reason)
    at_advice = told.find(FEED_ADVICE, at_reason + len(reason))
    at_fault = [told.find(word, at_advice) for word in said]
    assert at_reason >= 0 and at_advice > at_reason and min(at_fault) > at_advice, (
        f"a page whose rules met {fault} was told as {told!r}, not first why it leads to no feed "
        f"({reason!r}), then that the {FEED_ADVICE} may be given, then the fault ({said})"
    )
    assert RSSHUB_FORM not in told, f"a page whose rules met {fault} was told {told!r}: a route"


def test_a_site_not_reached_beside_a_silent_rsshub_is_left_as_httpx_raises_it(
    feed_sites: FeedSites,
) -> None:
    raised: list[httpx.TransportError] = []
    feed_sites.serve(NIGHT_PAGE, failing(httpx.ConnectError, raised))
    feed_sites.serve(RULES, failing(httpx.ConnectError, []))
    with pytest.raises(httpx.ConnectError) as failed:
        discovered_feed(NIGHT_PAGE, RSSHUB)
    assert raised and failed.value is raised[0], (
        f"a site not reached, beside a silent RSSHub, was told as {failed.value!r}, not as the "
        "failure of its connection that httpx raised"
    )


# T4: the key stays with the instance. A key with characters a query escapes.
KEY = "q+uorvex/zindlap-ü9146"
ELSEWHERE = "https://login.test:8443"
ELSEWHERE_HOST = "login.test"
NIGHT_ROUTE_ADDRESS = f"{RSSHUB_FORM}night/trains"


def written_keys() -> tuple[str, ...]:
    # The key as it is, as an address writes it as it is (its letter outside ASCII escaped, as an
    # address must), escaped once, twice, and as a form escapes it.
    once = quote(KEY, safe="")
    return (KEY, quote(KEY, safe="+/"), once, quote(once, safe=""), quote_plus(KEY))


def redirected(location_of: Callable[[httpx.Request], str]) -> Answer:
    # A redirect whose address the request decides, sent as bytes, so that it may hold any letter.
    def answer(request: httpx.Request) -> httpx.Response:
        location = location_of(request).encode()
        return httpx.Response(302, headers=[(b"location", location)], content=iter([]))

    return answer


# Addresses of another server that hold the key: under key or another name, as it is, escaped
# once or twice, or inside the address asked, as a login wall writes it.
KEY_LOCATIONS: dict[str, Callable[[httpx.Request], str]] = {
    "under key, escaped once": lambda r: f"{ELSEWHERE}/sso?key={quote(KEY, safe='')}",
    "under another name": lambda r: f"{ELSEWHERE}/sso?token={quote(KEY, safe='')}",
    "written as it is": lambda r: f"{ELSEWHERE}/sso?key={quote(KEY, safe='+/')}",
    "escaped twice": lambda r: f"{ELSEWHERE}/sso?key={quote(quote(KEY, safe=''), safe='')}",
    "inside the address asked": lambda r: f"{ELSEWHERE}/sso?rd={quote(str(r.url), safe='')}",
}
KEYED_STEPS: dict[str, Callable[[], object]] = {
    "a discovery": lambda: discovered_feed(NIGHT_ROUTE_ADDRESS, RSSHUB, KEY),
    "a poll": lambda: polled_route(NIGHT_ROUTE_ADDRESS, Validators(), RSSHUB, KEY),
}


def serve_elsewhere(sites: FeedSites) -> None:
    sites.serve_everywhere_at(ELSEWHERE_HOST, answered(rss_feed("elsewhere"), kind_of(RSS_KIND)))


def asked_elsewhere(sites: FeedSites) -> list[httpx.Request]:
    return [request for request in sites.requests if request.url.host == ELSEWHERE_HOST]


@pytest.mark.parametrize("step", sorted(KEYED_STEPS))
@pytest.mark.parametrize("location", sorted(KEY_LOCATIONS))
def test_a_route_redirected_to_another_server_by_an_address_that_holds_the_key_is_not_followed(
    feed_sites: FeedSites, location: str, step: str
) -> None:
    feed_sites.serve_everywhere_at(httpx.URL(RSSHUB).host, redirected(KEY_LOCATIONS[location]))
    serve_elsewhere(feed_sites)
    what = f"{step} of a route that RSSHub redirects elsewhere with the key {location}"
    with pytest.raises(UnreadableFeed) as refused:
        made = KEYED_STEPS[step]()
        pytest.fail(f"{what} gave {made}")
    told = str(refused.value)
    assert not asked_elsewhere(feed_sites), (
        f"{what} asked the other server {[str(r.url) for r in asked_elsewhere(feed_sites)]}"
    )
    assert ELSEWHERE in told and f"{ELSEWHERE}/" not in told and f"{ELSEWHERE}?" not in told, (
        f"{what} was told as {told!r}, which does not name the other server by its scheme, "
        "host and port alone"
    )
    assert "night/trains" in told and "instance" in told and RSSHUB_SETTING in told, (
        f"{what} was told as {told!r}, which does not say that the address asked redirects there, "
        f"that the key is for the instance alone, and that {RSSHUB_SETTING} names it"
    )
    assert RSSHUB_FORM not in told and not any(key in told for key in written_keys()), (
        f"{what} was told as {told!r}, which advises a route or holds the key"
    )


@pytest.mark.parametrize("step", sorted(KEYED_STEPS))
def test_a_route_redirected_to_another_server_without_the_key_is_followed_without_it(
    feed_sites: FeedSites, step: str
) -> None:
    feed_sites.serve_everywhere_at(
        httpx.URL(RSSHUB).host, redirected(lambda r: f"{ELSEWHERE}/feeds/night.xml")
    )
    serve_elsewhere(feed_sites)
    KEYED_STEPS[step]()
    elsewhere = [str(request.url) for request in asked_elsewhere(feed_sites)]
    assert elsewhere and not any(key in asked for asked in elsewhere for key in written_keys()), (
        f"{step} of a route that RSSHub redirects elsewhere without the key asked the other "
        f"server {elsewhere}: not followed, or followed with the key"
    )


@pytest.mark.parametrize("step", sorted(KEYED_STEPS))
def test_a_route_redirected_within_the_instance_carries_the_key(
    feed_sites: FeedSites, step: str
) -> None:
    def instance(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/night/trains":
            return redirected(lambda r: f"{RSSHUB}/night/trains/moved")(request)
        return answered(rss_feed("moved"), kind_of(RSS_KIND))(request)

    feed_sites.serve_everywhere_at(httpx.URL(RSSHUB).host, instance)
    KEYED_STEPS[step]()
    moved_to = [r for r in feed_sites.requests if r.url.path == "/night/trains/moved"]
    assert moved_to and all(r.url.params.get("key") == KEY for r in moved_to), (
        f"{step} of a route that RSSHub redirects within itself asked "
        f"{[str(r.url) for r in moved_to]}, not with the key"
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
    once = [asked] if status is None else [asked, at]
    assert feed_sites.asked() == once, (
        f"the {form} feed at {asked} was found with the requests {feed_sites.asked()}, not one "
        f"fetch of it: {once}"
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
    assert_found(page.address, RSSHUB, at, page.named[first].answer, f"the page {page.head!r}")
    tried = [page.address, *(named_address(page, feed.href) for feed in page.named[: first + 1])]
    assert feed_sites.asked() == tried, (
        f"finding the feed of the page {page.head!r} fetched {feed_sites.asked()}, not the page "
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
    assert named[-1] not in feed_sites.asked(), (
        f"a page that names four feeds, of which three do not read, had the fourth tried: "
        f"{feed_sites.asked()}"
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
    assert (feed_sites.asked(), discovered.address) == ([at], at), (
        f"the channel {address} was found at {discovered.address} with the fetches "
        f"{feed_sites.asked()}, not at YouTube's feed {at} alone"
    )
    assert not feed_sites.yt_dlp.asked, f"the channel {address} had yt-dlp asked"


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
    assert (feed_sites.asked(), discovered.address) == ([at], at), (
        f"the playlist {address} was found at {discovered.address} with the fetches "
        f"{feed_sites.asked()}, not at YouTube's feed {at} alone"
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
    assert_found(address, RSSHUB, at, "RSS", f"the show {address}")
    assert feed_sites.asked() == [APPLE_LOOKUP.format(show=show), at], (
        f"the show {address} was found with the fetches {feed_sites.asked()}, not Apple's "
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
    assert not feed_sites.yt_dlp.asked, (
        f"the channel {address}, whose page YouTube answered, had yt-dlp asked (Y2)"
    )


CONSENT = "https://consent.youtube.com/m?continue=https%3A%2F%2Fwww.youtube.com%2F%40nighttrains"
CONSENT_PAGE = page_naming("").replace(b"Night trains", b"Before you continue to YouTube")


def common_paths_asked(sites: FeedSites) -> list[str]:
    return [asked for asked in sites.asked() if urlsplit(asked).path in COMMON_FEED_PATHS]


def serve_consent(sites: FeedSites) -> None:
    # YouTube answers every address on its hosts with its page that asks for consent to cookies.
    for host in YOUTUBE_HOSTS:
        sites.serve_everywhere_at(host, moved(CONSENT, 302))
    sites.serve(CONSENT, answered(CONSENT_PAGE, kind_of(HTML)))


def serve_channel_feed(sites: FeedSites, channel: str) -> str:
    at = YOUTUBE_FEED.format(query=f"channel_id={channel}")
    sites.serve(at, answered(youtube_feed(channel, VIDEOS), kind_of(ATOM_KIND)))
    return at


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
    assert not common_paths_asked(feed_sites), (
        f"finding the feed of a YouTube page that names none fetched {feed_sites.asked()}, where "
        "YouTube's pages have no paths of feeds tried"
    )
    assert not feed_sites.yt_dlp.asked, (
        f"the channel {address}, whose page YouTube answered naming no feed, had yt-dlp asked (Y2)"
    )


# Y1: a channel named by its handle or an older name, which YouTube answers with its page that
# asks for consent, is found through yt-dlp, at YouTube's own feed of the id yt-dlp gives.
NAME_CHARACTERS = string.ascii_letters + string.digits
handles = st.text(
    st.sampled_from([*NAME_CHARACTERS, "_", "-", ".", *"夜行列車チャンネルéü"]),
    min_size=3,
    max_size=30,
).map(lambda handle: f"@{handle}")
older_names = st.tuples(
    st.sampled_from(("c", "user")),
    st.text(st.sampled_from(NAME_CHARACTERS), min_size=1, max_size=20),
).map("/".join)
# What may follow a channel's name in its address.
AFTER_NAMES = ("", "/", "/videos", "/featured", "?view=0", "/videos?view=0&sort=dd")


@EXAMPLES
@given(
    channel=channel_ids,
    host=st.sampled_from(YOUTUBE_HOSTS),
    name=st.one_of(handles, older_names),
    after=st.sampled_from(AFTER_NAMES),
    escaped=st.booleans(),
)
def test_a_youtube_channel_by_its_handle_that_asks_for_consent_is_found_through_yt_dlp(
    feed_sites: FeedSites, channel: str, host: str, name: str, after: str, escaped: bool
) -> None:
    feed_sites.forget()
    serve_consent(feed_sites)
    at = serve_channel_feed(feed_sites, channel)
    feed_sites.yt_dlp.said = lambda asked: channel_answer(asked, channel)
    address = f"https://{host}/{quote(name, safe='@/') if escaped else name}{after}"
    discovered = discovered_feed(address, RSSHUB)
    assert discovered.address == at and at in feed_sites.asked(), (
        f"the channel {address}, which YouTube answered with consent and yt-dlp names {channel}, "
        f"was found at {discovered.address} with the fetches {feed_sites.asked()}, not at "
        f"YouTube's feed {at}"
    )
    assert len(feed_sites.yt_dlp.asked) == 1, (
        f"finding the channel {address} asked yt-dlp {feed_sites.yt_dlp.asked}, not once (Y3)"
    )
    assert len(feed_sites.requests) <= MOST_REQUESTS, (
        f"finding the channel {address} made the requests {feed_sites.asked()}"
    )


def test_a_channel_found_by_its_handle_and_by_its_id_is_found_at_one_address(
    feed_sites: FeedSites,
) -> None:
    serve_consent(feed_sites)
    at = serve_channel_feed(feed_sites, CHANNEL)
    feed_sites.yt_dlp.said = lambda asked: channel_answer(asked, CHANNEL)
    by_handle = discovered_feed("https://www.youtube.com/@nighttrains", RSSHUB).address
    by_id = discovered_feed(f"https://www.youtube.com/channel/{CHANNEL}", RSSHUB).address
    assert by_handle == by_id == at, (
        f"the channel {CHANNEL} was found at {by_handle} by its handle and at {by_id} by its id, "
        f"not at its feed {at} by both"
    )


# Y2: yt-dlp is asked only then: not at a poll, and not for another address of YouTube's that
# asks for consent, which is refused as today, telling the reader to give the channel's id.
POLLED = {
    "a channel's feed": CHANNEL_FEED,
    "a channel's address by its handle, which asks for consent": (
        "https://www.youtube.com/@nighttrains"
    ),
}


@pytest.mark.parametrize("polled", sorted(POLLED))
def test_a_poll_never_asks_yt_dlp(feed_sites: FeedSites, polled: str) -> None:
    serve_consent(feed_sites)
    serve_channel_feed(feed_sites, CHANNEL)
    feed_sites.yt_dlp.said = lambda asked: channel_answer(asked, CHANNEL)
    with suppress(UnreadableFeed):
        fetched_feed(POLLED[polled], Validators())
    assert not feed_sites.yt_dlp.asked, f"a poll of {polled} asked yt-dlp"


OTHER_YOUTUBE_ADDRESSES = {
    "a video": "https://www.youtube.com/watch?v=Vid000000A1",
    "a short": "https://m.youtube.com/shorts/Vid000000A1",
    "the home page": "https://www.youtube.com/",
    "a search": "https://youtube.com/results?search_query=nighttrains",
    "a bare name": "https://www.youtube.com/nighttrains",
    "a bare name that begins with c": "https://www.youtube.com/cnighttrains",
    "a bare name that begins with user": "https://www.youtube.com/usernighttrains",
}


@pytest.mark.parametrize("other", sorted(OTHER_YOUTUBE_ADDRESSES))
def test_another_youtube_address_that_asks_for_consent_is_refused_as_today_without_yt_dlp(
    feed_sites: FeedSites, other: str
) -> None:
    address = OTHER_YOUTUBE_ADDRESSES[other]
    serve_consent(feed_sites)
    serve_channel_feed(feed_sites, CHANNEL)
    feed_sites.yt_dlp.said = lambda asked: channel_answer(asked, CHANNEL)
    told = refusal(address, RSSHUB, f"{other} at {address}, which YouTube answered with consent")
    assert address in told and "channel" in told and " id" in told, (
        f"{other} whose page asks for consent was refused as {told!r}, which does not tell the "
        "reader to give the channel's address with its id"
    )
    assert not feed_sites.yt_dlp.asked, f"{other} at {address} had yt-dlp asked"
    assert not common_paths_asked(feed_sites), (
        f"finding the feed of {other} whose page asks for consent fetched {feed_sites.asked()}, "
        "where YouTube's pages have no paths of feeds tried"
    )


# Y3: yt-dlp is asked once, for the channel, with no cookies, no cache and no download.


@pytest.mark.parametrize("name", HANDLES)
def test_yt_dlp_is_asked_once_for_the_channel_with_no_cookies_no_cache_and_no_download(
    feed_sites: FeedSites, name: str
) -> None:
    address = f"https://www.youtube.com/{name}/videos"
    serve_consent(feed_sites)
    serve_channel_feed(feed_sites, CHANNEL)
    feed_sites.yt_dlp.said = lambda asked: channel_answer(asked, CHANNEL)
    discovered_feed(address, RSSHUB)
    asked = feed_sites.yt_dlp.asked
    assert len(asked) == 1, f"finding the channel {address} asked yt-dlp {asked}, not once"
    at = urlsplit(asked[0].address)
    assert at.hostname in YOUTUBE_HOSTS and unquote(at.path).startswith(f"/{name}"), (
        f"finding the channel {address} asked yt-dlp for {asked[0].address}, not for the channel"
    )
    assert not asked[0].download, f"finding the channel {address} had yt-dlp download"
    options = feed_sites.yt_dlp.options
    assert options and not any(
        given.get("cookiefile") or given.get("cookiesfrombrowser") for given in options
    ), f"yt-dlp was asked with the options {options}, which name cookies"
    # Without a cachedir of False, yt-dlp keeps its cache in a folder of its own choosing.
    assert all(given.get("cachedir") is False for given in options), (
        f"yt-dlp was asked with the options {options}, which leave it a cache folder"
    )


# Y4: where yt-dlp names no channel, the address is refused as naming none, with what yt-dlp
# said, and RSSHub's rules and the advice follow as for any refusal.
FEED_PATH = "/feeds/videos.xml"
COLOUR = re.compile(r"\x1b\[[0-9;]*m")
YT_DLP_FAILURES = {
    "a channel that does not exist": (
        "\x1b[0;31mERROR:\x1b[0m [youtube:tab] @nochannel: Unable to download webpage: HTTP "
        "Error 404: Not Found (caused by <HTTPError 404: Not Found>)"
    ),
    "an answer on two lines": (
        "ERROR: [youtube:tab] @nochannel: Unable to download API page: HTTP Error 429: Too Many "
        "Requests\n(caused by <HTTPError 429: Too Many Requests>)"
    ),
    "nothing said": "",
}


def said_lines(message: str) -> list[str]:
    # What yt-dlp said, line by line, without its colours and its prefix.
    plain = COLOUR.sub("", message).removeprefix("ERROR:")
    return [line.strip() for line in plain.splitlines() if line.strip()]


def assert_told_no_channel(sites: FeedSites, told: str, address: str, said: list[str]) -> None:
    what = f"the handle {address}, at which yt-dlp names no channel,"
    asked = sites.yt_dlp.asked
    assert len(asked) == 1, f"{what} asked yt-dlp {asked}, not once"
    at_no_channel = told.find("no channel")
    assert address in told and "YouTube" in told and at_no_channel >= 0, (
        f"{what} was refused as {told!r}, not as YouTube naming no channel at it"
    )
    at_said = [told.find(line, at_no_channel) for line in said]
    assert all(at > at_no_channel for at in at_said), (
        f"{what} was refused as {told!r}, without what yt-dlp said ({said}) after it"
    )
    assert not any(sign in told for sign in ("\n", "\r", "\x1b", "ERROR")), (
        f"{what} was refused as {told!r}: not on one line, or with yt-dlp's prefix or colours"
    )
    assert told.find(FEED_ADVICE, max([at_no_channel, *at_said])) > 0 and RSSHUB_FORM in told, (
        f"{what} was refused as {told!r}, which the advice does not follow"
    )
    rules_after = [str(r.url) for r in sites.requests[asked[0].after_requests :]]
    assert RULES in rules_after, (
        f"{what} had RSSHub's rules asked in {sites.asked()}, not after yt-dlp"
    )
    assert not [heard for heard in sites.asked() if urlsplit(heard).path == FEED_PATH], (
        f"{what} had a feed of YouTube fetched: {sites.asked()}"
    )


@pytest.mark.parametrize("failure", sorted(YT_DLP_FAILURES))
def test_a_handle_at_which_yt_dlp_fails_is_refused_as_naming_no_channel_with_what_it_said(
    feed_sites: FeedSites, failure: str
) -> None:
    address = "https://www.youtube.com/@nochannel"
    serve_consent(feed_sites)
    feed_sites.serve(RULES, answered(b"{}", kind_of(RULES_KIND)))
    message = YT_DLP_FAILURES[failure]

    def fails(asked: str) -> Mapping[str, Any] | None:
        raise DownloadError(message)

    feed_sites.yt_dlp.said = fails
    told = refusal(address, RSSHUB, f"a handle at which yt-dlp failed with {failure}")
    assert_told_no_channel(feed_sites, told, address, said_lines(message))


# What yt-dlp may give in place of a channel's id: UC and 22 more of A-Z, a-z, 0-9, _ and -.
NO_ID, NO_ANSWER = object(), object()
NO_CHANNEL_IDS: dict[str, object] = {
    "no channel_id": NO_ID,
    "a channel_id of None": None,
    "an empty channel_id": "",
    "21 characters after UC": "UC" + "a" * 21,
    "23 characters after UC": "UC" + "a" * 23,
    "HC in place of UC": "HC" + CHANNEL[2:],
    "uc in small letters": "uc" + CHANNEL[2:],
    "a character outside an id's": CHANNEL[:-1] + ".",
    "a slash inside": CHANNEL[:10] + "/" + CHANNEL[11:],
    "a line break after it": CHANNEL + "\n",
    "a number": 42,
    "no answer at all": NO_ANSWER,
}


def answer_without_a_channel(given: object) -> Callable[[str], Mapping[str, Any] | None]:
    def said(asked: str) -> Mapping[str, Any] | None:
        if given is NO_ANSWER:
            return None
        answer = channel_answer(asked, CHANNEL) | {"id": "@nochannel"}
        del answer["channel_id"]
        return answer if given is NO_ID else answer | {"channel_id": given}

    return said


@pytest.mark.parametrize("given", sorted(NO_CHANNEL_IDS))
def test_a_handle_at_which_yt_dlp_gives_no_id_of_a_channels_shape_is_refused(
    feed_sites: FeedSites, given: str
) -> None:
    address = "https://www.youtube.com/@nochannel"
    serve_consent(feed_sites)
    serve_channel_feed(feed_sites, CHANNEL)
    feed_sites.serve(RULES, answered(b"{}", kind_of(RULES_KIND)))
    feed_sites.yt_dlp.said = answer_without_a_channel(NO_CHANNEL_IDS[given])
    told = refusal(address, RSSHUB, f"a handle at which yt-dlp gives {given}")
    assert_told_no_channel(feed_sites, told, address, [])


YOUTUBE_RULES = json.dumps(
    {
        "youtube.com": {
            "_name": "YouTube",
            "www": [
                {
                    "title": "Channel",
                    "docs": RADAR_DOCS,
                    "source": ["/:handle"],
                    "target": "/youtube/user/:handle",
                }
            ],
        }
    }
).encode()


def test_rsshubs_rules_are_tried_after_yt_dlp_names_no_channel(feed_sites: FeedSites) -> None:
    address = "https://www.youtube.com/@nighttrains"
    serve_consent(feed_sites)

    def instance(request: httpx.Request) -> httpx.Response:
        if str(request.url) == RULES:
            return answered(YOUTUBE_RULES, kind_of(RULES_KIND))(request)
        return answered(rss_feed("A channel through RSSHub"), kind_of(RSS_KIND))(request)

    def fails(asked: str) -> Mapping[str, Any] | None:
        raise DownloadError("ERROR: [youtube:tab] @nighttrains: This channel does not exist.")

    feed_sites.serve_everywhere_at(httpx.URL(RSSHUB).host, instance)
    feed_sites.yt_dlp.said = fails
    discovered = discovered_feed(address, RSSHUB)
    assert discovered.address.startswith(f"{RSSHUB_FORM}youtube/user/"), (
        f"the handle {address}, at which yt-dlp names no channel and a rule of RSSHub matches, "
        f"was found at {discovered.address}, not at the rule's route"
    )
    asked = feed_sites.yt_dlp.asked
    assert len(asked) == 1 and RULES in [
        str(r.url) for r in feed_sites.requests[asked[0].after_requests :]
    ], f"the handle {address} asked yt-dlp {asked} and fetched {feed_sites.asked()}"


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
    assert_found(page, RSSHUB, at, form, f"the page {page}, which names no feed")
    assert len(feed_sites.requests) <= MOST_REQUESTS, (
        f"finding the feed at {at} made the requests {feed_sites.asked()}"
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
    # yt-dlp, unscripted, fails at every handle.
    serve_consent(sites)
    return {
        "a page that names no feed": ("https://blog.test/about", RSSHUB),
        "a picture": ("https://blog.test/logo.png", RSSHUB),
        "a show Apple names no feed of": ("https://podcasts.apple.com/us/podcast/x/id7", RSSHUB),
        "a channel whose page asks for consent": ("https://www.youtube.com/nighttrains", RSSHUB),
        "a handle yt-dlp names no channel at": ("https://www.youtube.com/@nochannel", RSSHUB),
        "an address of no web": ("ftp://files.test/feed.xml", RSSHUB),
    }


REFUSED_KINDS = (
    "a page that names no feed",
    "a picture",
    "a show Apple names no feed of",
    "a channel whose page asks for consent",
    "a handle yt-dlp names no channel at",
    "an address of no web",
)


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
    assert told.startswith(OVERSIZE_SOURCE.format(source=page)), (
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
    assert told.startswith(OVERDUE_SOURCE.format(source=page)), (
        f"a page that trickles beyond the time allowed was refused as {told!r}"
    )
    assert took <= LOWERED_PAGE_SECONDS + 1.0, (
        f"a page that trickles was given up after {took:.1f}s"
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
        feed_sites.serve(href, trickling(b"<html><body>", HTML, 0.9 * LOWERED_FEED_SECONDS))
    began = time.monotonic()
    refusal(page, RSSHUB, "a page whose feeds trickle and do not read")
    took = time.monotonic() - began
    assert took <= LOWERED_DISCOVERY_SECONDS + OVERRUN_SECONDS, (
        f"one discovery went on for {took:.1f} seconds, beyond the {LOWERED_DISCOVERY_SECONDS} "
        "allowed in all"
    )


LONG_ADDRESS = "https://feeds.test/" + "a" * ADDRESS_CHARACTERS


def test_an_address_that_leads_to_one_longer_than_a_feeds_address_may_be_leads_to_no_feed(
    feed_sites: FeedSites,
) -> None:
    feed_sites.serve("https://blog.test/feed", moved(LONG_ADDRESS))
    feed_sites.serve(LONG_ADDRESS, answered(rss_feed("long"), kind_of(RSS_KIND)))
    refusal("https://blog.test/feed", RSSHUB, f"a feed moved to {len(LONG_ADDRESS)} characters")
