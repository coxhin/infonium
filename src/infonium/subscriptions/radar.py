import re
from typing import Annotated
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from pydantic import BaseModel, Field, JsonValue, TypeAdapter

# RSSHub publishes, for its browser extension Radar, the rules by which an address of a site
# leads to a route of its own: under the site's domain and a subdomain of it, rules each with a
# title, the page that documents its route, the paths of the site it applies to (sources) and
# the route (target). A source is written as RSSHub's extension reads it: /user/:id takes a
# part of the path as a parameter, /:lang?/news one that may be left out, /*rest the rest of
# the path, and ?key=value a query the address must hold.
RADAR_RULES_PATH = "/api/radar/rules"
# The key of a site's name among its subdomains, the key of the domain itself, and the
# subdomain the domain itself and it stand for one another under, as RSSHub's extension takes
# them.
SITE_NAME = "_name"
WHOLE_DOMAIN = "."
WWW = "www"
HOST_PARTS_APART = "."
PATH_APART = "/"
# A source that names a place within a page, after #, is told by what a page's scripts show,
# and is passed over.
FRAGMENT = "#"
QUERY = "?"
ANY_VALUE = "*"
SOURCE_PART = re.compile(r":(?P<parameter>\w+)(?P<optional>\?)?|\*(?P<rest>\w*)")
# A parameter of a route stands as a part of its path of its own, and may be left out where it
# ends with a question mark.
ROUTE_PARAMETER = re.compile(r"/:(?P<name>\w+)(?P<optional>\?)?")


class RadarRule(BaseModel):
    # A rule as RSSHub publishes it. A route that RSSHub's extension works out by a function is
    # published as no text, and the rule is passed over.
    title: str = ""
    docs: str = ""
    source: list[str] = []
    target: JsonValue = None


RadarSite = dict[str, Annotated[list[RadarRule] | str, Field(union_mode="left_to_right")]]
RADAR_RULES = TypeAdapter(dict[str, RadarSite])


class SourceRule(BaseModel):
    # One source of a rule, made ready to match: the path it matches, as a pattern whose groups
    # are its parameters, how many characters of a path it spells out, the query an address must
    # hold (a value of * holds any), and the rule.
    path: re.Pattern[str]
    spelled: int
    query: dict[str, str]
    title: str
    docs: str
    target: str


class RadarRoute(BaseModel):
    # The route a rule gives for an address: the rule's title, the page that documents it, and
    # the route with the address's parameters in it.
    title: str
    docs: str
    route: str


# The rules held, by domain, then by subdomain.
HeldRules = dict[str, dict[str, list[SourceRule]]]


def source_path_pattern(path: str) -> str:
    # The pattern of a source's path. A parameter that may be left out takes the slash before it
    # with it.
    pattern = ""
    taken = 0
    for found in SOURCE_PART.finditer(path):
        literal = path[taken : found.start()]
        optional = found["optional"] is not None and literal.endswith(PATH_APART)
        if optional:
            literal = literal.removesuffix(PATH_APART)
        pattern += re.escape(literal)
        if found["parameter"] is not None and optional:
            pattern += rf"(?:/(?P<{found['parameter']}>[^/]+))?"
        elif found["parameter"] is not None:
            pattern += rf"(?P<{found['parameter']}>[^/]+?)" + ("?" if found["optional"] else "")
        elif found["rest"]:
            pattern += rf"(?P<{found['rest']}>.+)"
        else:
            pattern += ".*"
        taken = found.end()
    return pattern + re.escape(path[taken:])


def spelled_out(path: str) -> int:
    # How many characters of a path a source's path spells out, its parameters aside.
    return len(SOURCE_PART.sub("", path))


def parameters_of(path: str) -> list[str]:
    return [
        found["parameter"] or found["rest"]
        for found in SOURCE_PART.finditer(path)
        if found["parameter"] or found["rest"]
    ]


def source_rules(rule: RadarRule) -> list[SourceRule]:
    # The sources of a rule that an address alone can give its route: those within a page's
    # path and query, whose parameters are named once, each as a name a pattern can give a
    # group, and give every parameter the route may not leave out.
    if not isinstance(rule.target, str):
        return []
    needed = {
        found["name"] for found in ROUTE_PARAMETER.finditer(rule.target) if not found["optional"]
    }
    usable = []
    for source in rule.source:
        written, _, query = source.partition(QUERY)
        path = written.removesuffix(PATH_APART) or PATH_APART
        given = parameters_of(path)
        named = len(set(given)) == len(given) and all(name.isidentifier() for name in given)
        if FRAGMENT in source or not named or not needed <= set(given):
            continue
        usable.append(
            SourceRule(
                path=re.compile(source_path_pattern(path)),
                spelled=spelled_out(path),
                query=dict(parse_qsl(query)),
                title=rule.title,
                docs=rule.docs,
                target=rule.target,
            )
        )
    return usable


def held_rules(content: bytes) -> HeldRules:
    # The rules RSSHub publishes, as JSON, made ready to match: each usable source of each rule
    # whose route is written out, in RSSHub's order.
    published = RADAR_RULES.validate_json(content)
    return {
        domain: {
            subdomain: [held for rule in rules for held in source_rules(rule)]
            for subdomain, rules in site.items()
            if subdomain != SITE_NAME and isinstance(rules, list)
        }
        for domain, site in published.items()
    }


def site_rules(host: str, rules: HeldRules) -> list[SourceRule]:
    # The rules of a host: of the longest domain it ends with that RSSHub has rules for, those of
    # the subdomain before it; the domain itself and its www stand for one another.
    parts = host.casefold().split(HOST_PARTS_APART)
    for start in range(len(parts)):
        domain = HOST_PARTS_APART.join(parts[start:])
        if domain in rules:
            subdomain = HOST_PARTS_APART.join(parts[:start]) or WHOLE_DOMAIN
            standing_in = {WWW: WHOLE_DOMAIN, WHOLE_DOMAIN: WWW}.get(subdomain, subdomain)
            site = rules[domain]
            return site.get(subdomain) or site.get(standing_in, [])
    return []


def route_of(rule: SourceRule, parameters: dict[str, str]) -> str:
    # A rule's route with the address's parameters in it; a parameter the address leaves out
    # is left out of the route, as the route allows.
    def given(found: re.Match[str]) -> str:
        value = parameters.get(found["name"])
        return PATH_APART + quote(value, safe="") if value else ""

    return ROUTE_PARAMETER.sub(given, rule.target)


def routes_for(address: str, rules: HeldRules) -> list[RadarRoute]:
    # The routes the rules that an address matches give, the rule whose source spells out more of
    # the address first and, of those that spell out as much, the first in RSSHub's order, which
    # follows the names of its routes: of a GitHub repository's issues, the rule of issues comes
    # before that of comments, whose source takes any part of the path. A path is matched as
    # read, its escapes undone, and without a slash at its end.
    parts = urlsplit(address)
    path = unquote(parts.path).removesuffix(PATH_APART) or PATH_APART
    query = dict(parse_qsl(parts.query))
    matched: list[tuple[int, RadarRoute]] = []
    for rule in site_rules(parts.hostname or "", rules):
        found = rule.path.fullmatch(path)
        held = all(
            name in query and value in (ANY_VALUE, query[name])
            for name, value in rule.query.items()
        )
        if found is not None and held:
            parameters = {name: value for name, value in found.groupdict().items() if value}
            route = RadarRoute(title=rule.title, docs=rule.docs, route=route_of(rule, parameters))
            matched.append((rule.spelled, route))
    kept: dict[str, RadarRoute] = {}
    for _, route in sorted(matched, key=lambda found: -found[0]):
        kept.setdefault(route.route, route)
    return list(kept.values())
