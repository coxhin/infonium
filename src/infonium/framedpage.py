import re
from base64 import b64encode
from urllib.parse import urljoin, urlsplit

import trafilatura
from lxml import etree, html
from pydantic import BaseModel

from infonium.blockholders import STYLE_ATTRIBUTE, placed_blocks, unread_parts
from infonium.document import NO_TEXT, WEB_SCHEMES, Block, BlockNumber, UnreadableSource
from infonium.htmlpage import IS_HIDDEN, drop_dialogs
from infonium.library import Link, Origin, Upload

# What a page holds that would run, send or load another page where it is shown. The headers
# the page is served with forbid all of this to the browser; taking it out of the page itself
# is the second line of defence, which holds wherever the page is shown without those
# headers. A script is removed with what it holds. An object or an applet is replaced by its
# fallback content, which a browser shows where it cannot show the object; so is an embed,
# which holds nothing in a browser, but which the parser takes for an element that holds
# what follows it. A frame keeps its box but not its page.
SCRIPT = "script"
STYLE_ELEMENT = "style"
FOREIGN_CONTENT = ("svg", "math")
# What a page holds for a browser without scripts is shown, as such a browser shows it: its
# stylesheets and the pictures of its article. A picture in it that is there only to count
# the visit is not: one of a pixel or none, by its attributes or its style, one that is
# hidden, and one with neither a size nor a description, which a counter needs no more than
# it needs to be seen.
SCRIPTLESS_PICTURES = etree.XPath("//noscript//img")
PICTURE_SIZE = re.compile(r"(?:^|[;\s])(?:width|height)\s*:\s*[01](?:px)?\s*(?:;|$)", re.IGNORECASE)
PIXEL_SIZE_SUFFIX = "px"
SIZE_ATTRIBUTES = ("width", "height")
DESCRIPTION = "alt"
LARGEST_COUNTER_SIZE = 1
REPLACED_BY_CONTENT = frozenset({"object", "applet", "param", "embed"})
FRAMES = ("iframe", "frame", "fencedframe", "portal")
FRAMED_PAGE = ("src", "srcdoc")
FORM = "form"
# A form whose method is dialog sends nothing anywhere: it closes the dialog it stands in, if
# any. Its buttons may not name another method or address of their own.
FORM_METHOD = "method"
SENT_NOWHERE = "dialog"
DROPPED_ATTRIBUTES = frozenset(
    {
        "action",
        "formaction",
        "formmethod",
        "formenctype",
        "formtarget",
        "ping",
        "referrerpolicy",
        "autofocus",
        "autoplay",
    }
)
EVENT_HANDLER_PREFIX = "on"
# A browser drops tabs and line breaks anywhere in an address, and spaces and control
# characters before it, so these do not hide a script's address from it.
ADDRESS_NOISE = re.compile(r"[\t\n\r]")
SCRIPT_ADDRESS = re.compile(r"[\x00-\x20]*javascript:", re.IGNORECASE)
META = "meta"
META_INSTRUCTION = "http-equiv"
META_NAME = "name"
META_CONTENT = "content"
REFERRER = "referrer"
NO_REFERRER = "no-referrer"
LINKED_RESOURCE = "link"
RELATION = "rel"
STYLESHEET = "stylesheet"
PRELOAD = "preload"
PRELOADED_KIND = "as"
STYLE_KIND = "style"
MEDIA = "media"
PRINT_MEDIA = "print"
ON_LOAD = "onload"

# Where the page's relative addresses lead. A base that leads nowhere makes the browser fail
# to resolve them, so a page with no address of its own loads nothing from infonium's origin.
BASE = "base"
HEAD = "head"
BODY = "body"
REFERENCE_ADDRESS = "href"
NOWHERE = "about:blank"
WORLD_WIDE_WEB_PREFIX = "www."
NAMED_ADDRESSES = etree.XPath(
    "//link[@rel = 'canonical']/@href | //meta[@property = 'og:url']/@content"
)
OWN_BASE = etree.XPath("//base[@href]/@href")

# Chrome sends the origin of the page as the referrer of what the CSS held in the page loads,
# in a style element or a style attribute, whatever referrer policy the page names in its
# headers or in a meta element; what a stylesheet loaded from an address loads names that
# address, and a stylesheet loaded from a data: address names nothing.
# So CSS that loads anything is moved into stylesheets with data: addresses, which are no
# referrer, and its addresses are made absolute, since a data: address is no base for them.
# A declaration taken out of a style attribute is applied to its element by a rule in such a
# stylesheet, marked important as a style attribute outweighs other rules.
CSS_ADDRESS = re.compile(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)"'\s]*))\s*\)""", re.IGNORECASE)
CSS_IMPORT = re.compile(r"""@import\s+(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
# A fragment names an element of the page itself, wherever the CSS stands.
UNFETCHED_ADDRESS_PREFIXES = ("#", "data:")
CSS_DECLARATION_END = re.compile(r";(?![^(]*\))")
IMPORTANT = re.compile(r"!\s*important\s*$", re.IGNORECASE)
IMPORTANT_MARK = " !important"
STYLESHEET_ADDRESS = "data:text/css;charset=utf-8;base64,{content}"
IMPORTED_STYLESHEET = '@import url("{address}");'
CSS_URL = 'url("{address}")'
QUOTE_ESCAPE = str.maketrans({'"': "%22"})
MOVED_STYLE_ATTRIBUTE = "data-infonium-style"
MOVED_STYLE_RULE = '[{attribute}="{number}"]{{{declarations}}}'


class FramedPage(BaseModel):
    html: str
    placed: list[BlockNumber]


def page_tree(content: bytes, address: str) -> html.HtmlElement:
    # The page is decoded and parsed as the extractor does it.
    page = trafilatura.load_html(content)
    if page is None:
        raise UnreadableSource(NO_TEXT.format(source=address))
    return page


def is_script_address(value: str) -> bool:
    return SCRIPT_ADDRESS.match(ADDRESS_NOISE.sub("", value)) is not None


def is_visit_counter(picture: html.HtmlElement) -> bool:
    sizes = [
        picture.get(name, "").strip().removesuffix(PIXEL_SIZE_SUFFIX) for name in SIZE_ATTRIBUTES
    ]
    tiny = any(size.isdigit() and int(size) <= LARGEST_COUNTER_SIZE for size in sizes)
    styled_tiny = PICTURE_SIZE.search(picture.get(STYLE_ATTRIBUTE, "")) is not None
    unsized = not any(sizes) and not picture.get(DESCRIPTION, "").strip()
    return tiny or styled_tiny or unsized or bool(IS_HIDDEN(picture))


def drop_visit_counters(page: html.HtmlElement) -> None:
    for picture in SCRIPTLESS_PICTURES(page):
        if is_visit_counter(picture):
            picture.drop_tree()


def made_inert(page: html.HtmlElement) -> None:
    for element in list(page.iter(etree.Element)):
        tag = element.tag
        if tag == LINKED_RESOURCE:
            kept_resource(element)
        for name, value in list(element.attrib.items()):
            dropped = name in DROPPED_ATTRIBUTES or name.startswith(EVENT_HANDLER_PREFIX)
            if dropped or is_script_address(value):
                del element.attrib[name]
        if tag in FRAMES:
            for name in FRAMED_PAGE:
                element.attrib.pop(name, None)
        elif tag == FORM:
            element.set(FORM_METHOD, SENT_NOWHERE)
        elif tag in REPLACED_BY_CONTENT:
            element.drop_tag()
        elif tag in (SCRIPT, BASE) or is_page_instruction(element) or is_markup_in_style(element):
            element.drop_tree()


def is_markup_in_style(element: html.HtmlElement) -> bool:
    # The parser reads a style element as text wherever it stands, and it is written out as it
    # was read; within a picture or a formula a browser reads its content as elements, so
    # there what looks like an element in it could be one.
    in_foreign_content = next(element.iterancestors(*FOREIGN_CONTENT), None) is not None
    return element.tag == STYLE_ELEMENT and in_foreign_content and "<" in (element.text or "")


def is_page_instruction(element: html.HtmlElement) -> bool:
    # A meta element that tells the browser what to do with the page, such as load another in
    # its place, or which referrer to send.
    return element.tag == META and (
        element.get(META_INSTRUCTION) is not None or element.get(META_NAME, "").lower() == REFERRER
    )


def kept_resource(link: html.HtmlElement) -> None:
    # Only stylesheets are kept of what a page links to. Two ways in which a page's script
    # turns on a stylesheet once it has loaded are done here in its place: a stylesheet
    # preloaded becomes one, and one loaded for print is used for the screen.
    relations = link.get(RELATION, "").lower().split()
    preloaded_style = PRELOAD in relations and link.get(PRELOADED_KIND, "").lower() == STYLE_KIND
    if preloaded_style:
        link.set(RELATION, STYLESHEET)
    elif STYLESHEET not in relations:
        link.drop_tree()
    elif link.get(MEDIA, "").lower() == PRINT_MEDIA and link.get(ON_LOAD) is not None:
        del link.attrib[MEDIA]


def css_address(match: re.Match[str]) -> str:
    return next(group for group in match.groups() if group is not None).strip()


def loads_from_css(css: str) -> bool:
    return any(
        not css_address(match).lower().startswith(UNFETCHED_ADDRESS_PREFIXES)
        for match in CSS_ADDRESS.finditer(css)
    )


def absolute_css(css: str, base: str) -> str:
    def absolute(match: re.Match[str]) -> str:
        address = css_address(match)
        if address.lower().startswith(UNFETCHED_ADDRESS_PREFIXES):
            return match.group(0)
        joined = urljoin(base, address).translate(QUOTE_ESCAPE)
        return CSS_URL.format(address=joined)

    imports_as_addresses = CSS_IMPORT.sub(
        lambda match: f"@import {CSS_URL.format(address=css_address(match))}", css
    )
    return CSS_ADDRESS.sub(absolute, imports_as_addresses)


def stylesheet_address(css: str) -> str:
    return STYLESHEET_ADDRESS.format(content=b64encode(css.encode()).decode())


def important(declaration: str) -> str:
    return declaration if IMPORTANT.search(declaration) else declaration + IMPORTANT_MARK


def with_css_moved(page: html.HtmlElement, base: str) -> None:
    for style in page.iter(STYLE_ELEMENT):
        if loads_from_css(style.text or ""):
            imported = stylesheet_address(absolute_css(style.text or "", base))
            style.text = IMPORTED_STYLESHEET.format(address=imported)
    rules: list[str] = []
    for element in page.iter(etree.Element):
        declarations = CSS_DECLARATION_END.split(element.get(STYLE_ATTRIBUTE, ""))
        moved = [declaration for declaration in declarations if loads_from_css(declaration)]
        if moved:
            kept = [declaration for declaration in declarations if declaration not in moved]
            element.set(STYLE_ATTRIBUTE, ";".join(kept))
            element.set(MOVED_STYLE_ATTRIBUTE, str(len(rules)))
            applied = ";".join(important(absolute_css(declaration, base)) for declaration in moved)
            rules.append(
                MOVED_STYLE_RULE.format(
                    attribute=MOVED_STYLE_ATTRIBUTE, number=len(rules), declarations=applied
                )
            )
    body = page.find(BODY)
    if rules and body is not None:
        stylesheet = html.Element(LINKED_RESOURCE)
        stylesheet.set(RELATION, STYLESHEET)
        stylesheet.set(REFERENCE_ADDRESS, stylesheet_address("\n".join(rules)))
        body.append(stylesheet)


def site_of(address: str) -> str:
    return (urlsplit(address).hostname or "").removeprefix(WORLD_WIDE_WEB_PREFIX)


def base_address(page: html.HtmlElement, origin: Origin) -> str:
    # A page is laid out at the address it names as its own, where that is on the site a
    # person asked for: a site that redirects names there the address it served the page
    # from. Otherwise it is laid out at the address asked for. An uploaded page has only an
    # address it names, and a base of the page's own is taken relative to the page's address.
    named = [str(address).strip() for address in NAMED_ADDRESSES(page)]
    own_base = [str(address).strip() for address in OWN_BASE(page)][:1]
    match origin:
        case Link(url=asked):
            own = [
                urljoin(asked, address)
                for address in named
                if site_of(urljoin(asked, address)) == site_of(asked)
            ]
            address = own[0] if own else asked
        case Upload():
            absolute = [address for address in named if address.startswith(WEB_SCHEMES)]
            address = absolute[0] if absolute else NOWHERE
    if own_base and address != NOWHERE:
        return urljoin(address, own_base[0])
    return address


def heading_address(origin: Origin) -> str:
    # The address the extractor was given, as far as it is kept: it tells marks that lead
    # to a heading's own place from links to elsewhere.
    match origin:
        case Link(url=asked):
            return asked
        case Upload(name=name):
            return name


def with_head(page: html.HtmlElement, base: str) -> None:
    # The page sends no referrer, as its headers say too, and its addresses lead to its site.
    # Both are set before anything in the page loads.
    head = page.find(HEAD)
    if head is None:
        head = html.Element(HEAD)
        page.insert(0, head)
    base_element = html.Element(BASE)
    base_element.set(REFERENCE_ADDRESS, base)
    head.insert(0, base_element)
    referrer = html.Element(META)
    referrer.set(META_NAME, REFERRER)
    referrer.set(META_CONTENT, NO_REFERRER)
    head.insert(0, referrer)


def framed_page(content: bytes, origin: Origin, blocks: list[Block]) -> FramedPage:
    # The page an item was read from, made ready to be shown with none of its scripts run,
    # with the elements that hold the blocks' text marked. Its dialogs are left out, as the
    # extractor leaves them out: shown without the script that closes them, they would cover
    # the page.
    page = page_tree(content, heading_address(origin))
    doctype = page.getroottree().docinfo.doctype
    base = base_address(page, origin)
    made_inert(page)
    drop_visit_counters(page)
    with_css_moved(page, base)
    drop_dialogs(page)
    placed = placed_blocks(page, unread_parts(page, heading_address(origin)), base, blocks)
    with_head(page, base)
    serialized = html.tostring(page, encoding="unicode")
    return FramedPage(html=f"{doctype}\n{serialized}" if doctype else serialized, placed=placed)
