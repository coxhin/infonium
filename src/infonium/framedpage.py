import re
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin, urlsplit

import trafilatura
from lxml import etree, html
from pydantic import BaseModel
from trafilatura.utils import decode_file

from infonium.blockholders import PAGE_ATTRIBUTE, drop_own_names, placed_blocks, unread_parts
from infonium.document import (
    NO_TEXT,
    WEB_SCHEMES,
    Block,
    BlockNumber,
    Document,
    UnreadableSource,
)
from infonium.htmlpage import (
    HEAD,
    IS_HIDDEN,
    PICTURE,
    REFERENCE_ADDRESS,
    STYLE_ATTRIBUTE,
    drop_dialogs,
)
from infonium.library import Link, Origin, Upload

# What a page holds that would run, send or load another page where it is shown. The headers
# the page is served with forbid all of this to the browser; taking it out of the page itself
# is the second line of defence, which holds wherever the page is shown without those
# headers. A script is removed with what it holds. An object or an applet is replaced by its
# fallback content, which a browser shows where it cannot show the object; so is an embed,
# which holds nothing in a browser, but which the parser takes for an element that holds
# what follows it. A frame keeps its box but not its page. An animation of a picture is
# removed: it can set an attribute to a value that the page does not hold, such as a link's
# address to a script's.
SCRIPT = "script"
STYLE_ELEMENT = "style"
FOREIGN_CONTENT = ("svg", "math")
ANIMATIONS = frozenset({"animate", "animatecolor", "animatemotion", "animatetransform", "set"})
# What a page holds for a browser without scripts is shown, as such a browser shows it: its
# stylesheets and the pictures of its article. A picture in it that says of itself that it is
# not to be seen is left out, as one that is only there to count the visit: one whose sizes,
# by its attributes or its style, are a pixel or less, and one that is hidden.
SCRIPTLESS_PICTURES = etree.XPath(f"//noscript//{PICTURE}")
SIZE_ATTRIBUTES = ("width", "height")
STYLED_SIZE = re.compile(
    r"(?:^|[;\s])(?:width|height)\s*:\s*([0-9.]+)(?:px)?\s*(?:;|$)", re.IGNORECASE
)
PIXEL_SIZE = re.compile(r"\s*([0-9.]+)(?:px)?\s*", re.IGNORECASE)
LARGEST_COUNTER_SIZE = 1.0
REPLACED_BY_CONTENT = frozenset({"object", "applet", "param", "embed"})
FRAMES = ("iframe", "frame", "fencedframe", "portal")
FRAMED_PAGE = ("src", "srcdoc")
FORM = "form"
# What navigates when it is activated: a link of HTML or SVG, an area of an image map, a form,
# wherever it stands, in a template too, and the base that sets the target of them all. Each
# opens a new browsing context, which the frame's sandbox, allowing no popups, refuses: a click,
# Enter or a sent form loads nothing in the frame, whatever target the page gave it. The
# addresses stay, for the reader to open a link elsewhere itself.
NAVIGATING = frozenset({"a", "area", FORM})
TARGET = "target"
NEW_CONTEXT = "_blank"
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
        "attributionsrc",
    }
)
EVENT_HANDLER_PREFIX = "on"
# A browser drops tabs and line breaks anywhere in an address, and spaces and control
# characters before it, so these do not hide a script's address from it.
ADDRESS_NOISE = re.compile(r"[\t\n\r]")
SCRIPT_ADDRESS = re.compile(r"[\x00-\x20]*javascript:", re.IGNORECASE)
# Attributes that hold several addresses, or an address beside other words, are looked at part
# by part, and dropped where any part is a script's address: the candidates of a srcset, the
# addresses of a ping or an archive, the content of a meta element (as a refresh's "0;
# url=…"), and the url() of a style.
ADDRESS_LISTS = frozenset({"srcset", "imagesrcset", "ping", "archive"})
ADDRESS_LIST_PARTS = re.compile(r"[\s,;=]+")
SCRIPT_IN_CSS = re.compile(r"url\(\s*['\"]?[\x00-\x20]*javascript:", re.IGNORECASE)
META = "meta"
META_INSTRUCTION = "http-equiv"
META_NAME = "name"
META_CONTENT = "content"
# The page's own declaration of its document type, which sets the mode a browser lays it out
# in. The extractor's parser is not told it: trafilatura removes a declaration that names a
# document type definition, a "/" in its first line, before it parses. It is taken from the
# decoded text as a browser's tokenizer reads it, after any spaces and comments, up to the
# first ">".
DOCUMENT_TYPE = re.compile(
    r"\A\ufeff?(?:\s|<!--.*?-->)*(<!doctype[^>]*>)", re.IGNORECASE | re.DOTALL
)
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
# to resolve them, so a page with no address of its own loads nothing from infonium's origin;
# a base of the page's own is kept only where it is a web address, since any other, such as a
# data: address, makes the browser resolve the page's addresses against infonium's.
BASE = "base"
NOWHERE = "about:blank"
WORLD_WIDE_WEB_PREFIX = "www."
NAMED_ADDRESSES = etree.XPath(
    "//link[@rel = 'canonical']/@href | //meta[@property = 'og:url']/@content"
)
OWN_BASE = etree.XPath("//base[@href]/@href")


class FramedPage(BaseModel):
    html: str
    placed: list[BlockNumber]


def page_tree(text: str, address: str) -> html.HtmlElement:
    # The page's decoded text is parsed as the extractor parses it.
    page = trafilatura.load_html(text)
    if page is None:
        raise UnreadableSource(NO_TEXT.format(source=address))
    return page


def is_script_address(value: str) -> bool:
    return SCRIPT_ADDRESS.match(ADDRESS_NOISE.sub("", value)) is not None


def names_script(element: html.HtmlElement, name: str, value: str) -> bool:
    listed = name in ADDRESS_LISTS or (element.tag == META and name == META_CONTENT)
    parts = ADDRESS_LIST_PARTS.split(ADDRESS_NOISE.sub("", value)) if listed else []
    in_css = name == STYLE_ATTRIBUTE and SCRIPT_IN_CSS.search(ADDRESS_NOISE.sub("", value))
    return is_script_address(value) or any(map(is_script_address, parts)) or bool(in_css)


def picture_sizes(picture: html.HtmlElement) -> list[float]:
    given = [PIXEL_SIZE.fullmatch(picture.get(name, "")) for name in SIZE_ATTRIBUTES]
    styled = STYLED_SIZE.findall(picture.get(STYLE_ATTRIBUTE, ""))
    numbers = [size.group(1) for size in given if size is not None] + styled
    return [float(number) for number in numbers if number.count(".") <= 1 and number != "."]


def is_visit_counter(picture: html.HtmlElement) -> bool:
    sizes = picture_sizes(picture)
    no_larger_than_a_pixel = bool(sizes) and all(size <= LARGEST_COUNTER_SIZE for size in sizes)
    return no_larger_than_a_pixel or bool(IS_HIDDEN(picture))


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
            if dropped or names_script(element, name, value):
                del element.attrib[name]
        if tag in NAVIGATING:
            element.set(TARGET, NEW_CONTEXT)
        if tag in FRAMES:
            for name in FRAMED_PAGE:
                element.attrib.pop(name, None)
        elif tag == FORM:
            element.set(FORM_METHOD, SENT_NOWHERE)
        elif tag in REPLACED_BY_CONTENT:
            element.drop_tag()
        elif (
            tag in (SCRIPT, BASE)
            or str(tag).lower() in ANIMATIONS
            or is_page_instruction(element)
            or is_markup_in_style(element)
        ):
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
    joined = [urljoin(address, base) if address != NOWHERE else base for base in own_base]
    own_web_base = [base for base in joined if base.lower().startswith(WEB_SCHEMES)]
    return own_web_base[0] if own_web_base else address


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
    base_element.set(TARGET, NEW_CONTEXT)
    head.insert(0, base_element)
    referrer = html.Element(META)
    referrer.set(META_NAME, REFERRER)
    referrer.set(META_CONTENT, NO_REFERRER)
    head.insert(0, referrer)


def framed_page(content: bytes, item_id: str, origin: Origin, blocks: list[Block]) -> FramedPage:
    # The page an item was read from, made ready to be shown with none of its scripts run,
    # with the elements that hold the blocks' text marked. Its dialogs are left out, as the
    # extractor leaves them out: shown without the script that closes them, they would cover
    # the page.
    text = decode_file(content)
    page = page_tree(text, heading_address(origin))
    declared = DOCUMENT_TYPE.match(text)
    doctype = declared.group(1) if declared else ""
    base = base_address(page, origin)
    made_inert(page)
    drop_own_names(page)
    drop_visit_counters(page)
    drop_dialogs(page)
    placed = placed_blocks(page, unread_parts(page, heading_address(origin)), blocks)
    with_head(page, base)
    page.set(PAGE_ATTRIBUTE, item_id)
    serialized = html.tostring(page, encoding="unicode")
    return FramedPage(html=f"{doctype}\n{serialized}" if doctype else serialized, placed=placed)


class KeptPages:
    # The pages built last, kept by owner and document. A document never changes, so a page is
    # dropped only for room. functools.lru_cache keys a result on every argument it is built
    # from, where a page must be built from the document already read and kept by its name.
    def __init__(self, room: int) -> None:
        self.room = room
        self.pages: OrderedDict[tuple[str, str], FramedPage] = OrderedDict()
        self.lock = Lock()

    def page(self, owner: str, document: Document, origin: Origin, snapshot: Path) -> FramedPage:
        key = (owner, document.id)
        with self.lock:
            kept = self.pages.get(key)
            if kept is not None:
                self.pages.move_to_end(key)
                return kept
        built = framed_page(snapshot.read_bytes(), document.item_id, origin, document.blocks)
        with self.lock:
            self.pages[key] = built
            self.pages.move_to_end(key)
            while len(self.pages) > self.room:
                self.pages.popitem(last=False)
        return built
