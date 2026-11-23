import re
from bisect import bisect_left
from copy import deepcopy
from difflib import SequenceMatcher
from itertools import count
from urllib.parse import urljoin, urlsplit

import trafilatura
from lxml import etree, html
from pydantic import BaseModel, ConfigDict

from infonium.anchor import comparable
from infonium.document import (
    CELL_SEPARATOR,
    NO_TEXT,
    ROW_SEPARATOR,
    WEB_SCHEMES,
    Block,
    BlockNumber,
    Figure,
    Table,
    UnreadableSource,
)
from infonium.htmlpage import INLINE_QUOTATION, PICTURE, drop_dialogs, drop_unread_parts
from infonium.library import Link, Origin, Upload

# The attribute that carries a block's number on the element that holds the block's text,
# and the element that holds a block's text where the text is only part of an element. The
# element's name is one that no page's stylesheet names, and like any unknown element it is
# laid out inline, so it changes no layout around the inline content it holds.
BLOCK_ATTRIBUTE = "data-infonium-block"
RUN_ELEMENT = "infonium-run"

# What a page holds that would run, send or load another page in the frame. A script is
# removed with what it holds. An object or an applet is replaced by its fallback content,
# which a browser shows where it cannot show the object; so is an embed, which holds nothing
# in a browser, but which the parser takes for an element that holds what follows it. A
# frame keeps its box but not its page.
SCRIPT = "script"
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

# Elements laid out as blocks, which part the runs of inline content around them, as the
# extractor parts its blocks at them.
BLOCK_LEVEL_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "caption",
        "center",
        "dd",
        "details",
        "dialog",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "hr",
        "legend",
        "li",
        "main",
        "menu",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
LINE_BREAK_TAG = "br"
# Elements whose text the extractor does not read: code for the browser, and words the author
# struck out.
HIDDEN_BY_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
STYLE_ATTRIBUTE = "style"
UNREAD_TAGS = frozenset({"script", "style", "template", "noscript", "del", "s", "strike"})
# What an element may hold besides its text that a reader would lose if the element's content
# were replaced by a translation.
EMBEDDED_TAGS = (
    PICTURE,
    "picture",
    "svg",
    "math",
    "video",
    "audio",
    "canvas",
    "iframe",
    "frame",
    "object",
    "input",
    "select",
    "textarea",
)
TABLE = "table"
# A table's rows as the extractor reads them: its caption first, as a row of one cell.
OWN_ROWS = etree.XPath("./caption | ./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
CELLS = etree.XPath("self::caption | ./td | ./th")
# Picture addresses the extractor reads, in the order it tries them.
PICTURE_ADDRESS_PREFIX = "data-src"
PICTURE_ADDRESS = "src"
QUOTATION_MARK = '"'
WORD_GAP = " "
CELL_FILLER = re.compile(r"[ ;]")
# Kinds of key, so that a figure's address or a table's cells never equal a text.
TABLE_KEY = "\x01"
FIGURE_KEY = "\x02"
# The attribute by which elements of the page are found again in a copy of it.
COPY_MARK = "data-infonium-copy"


class FramedPage(BaseModel):
    html: str
    placed: list[BlockNumber]


class ElementPlace(BaseModel):
    # A whole element, at its place in the walk of the page; begin and end bound its text in
    # the page's shown text.
    model_config = ConfigDict(arbitrary_types_allowed=True)
    order: int
    begin: int
    end: int
    element: html.HtmlElement


class RunPlace(BaseModel):
    # Inline content of an element that block-level children part from the rest: the text
    # after a child, or at the element's start where there is no child, and the children
    # that follow it, each with its tail.
    model_config = ConfigDict(arbitrary_types_allowed=True)
    order: int
    begin: int
    end: int
    parent: html.HtmlElement
    after: html.HtmlElement | None
    children: list[html.HtmlElement]


Place = ElementPlace | RunPlace


class Candidate(BaseModel):
    key: str
    place: Place


class ShownText:
    # The text a reader sees in the page's body, walked in document order, with where each
    # element and each run of inline content stands in it.
    def __init__(self, unread: set[html.HtmlElement]) -> None:
        self.unread = unread
        self.pieces: list[str] = []
        self.length = 0
        self.orders = count()
        self.elements: list[ElementPlace] = []
        self.runs: list[RunPlace] = []

    def write(self, piece: str | None) -> None:
        if piece:
            self.pieces.append(piece)
            self.length += len(piece)

    def is_read(self, node: etree._Element) -> bool:
        # The extractor does not read what a style hides.
        return (
            isinstance(node.tag, str)
            and node.tag not in UNREAD_TAGS
            and node not in self.unread
            and HIDDEN_BY_STYLE.search(node.get(STYLE_ATTRIBUTE, "")) is None
        )

    def parts_runs(self, node: etree._Element) -> bool:
        # The second of two line breaks in a row parts paragraphs, as it does for the extractor.
        earlier = node.getprevious()
        second_break = (
            node.tag == LINE_BREAK_TAG
            and earlier is not None
            and earlier.tag == LINE_BREAK_TAG
            and not (earlier.tail or "").strip()
        )
        return node in self.unread or node.tag in BLOCK_LEVEL_TAGS or second_break

    def walk(self, element: html.HtmlElement) -> None:
        order = next(self.orders)
        begin = self.length
        if element.tag in BLOCK_LEVEL_TAGS or element.tag == LINE_BREAK_TAG:
            self.write(WORD_GAP)
        if element.tag == INLINE_QUOTATION:
            self.write(QUOTATION_MARK)
        runs: list[RunPlace] = []
        after: html.HtmlElement | None = None
        run_children: list[html.HtmlElement] = []
        run_order, run_begin = next(self.orders), self.length
        self.write(element.text)
        for child in element:
            parts = self.parts_runs(child)
            if parts:
                runs.append(self.run(run_order, run_begin, element, after, run_children))
            if self.is_read(child):
                self.walk(child)
            if parts:
                after = child
                run_order, run_begin, run_children = next(self.orders), self.length, []
            else:
                run_children.append(child)
            self.write(child.tail)
        if element.tag == INLINE_QUOTATION:
            self.write(QUOTATION_MARK)
        if runs:
            runs.append(self.run(run_order, run_begin, element, after, run_children))
            self.runs.extend(runs)
        self.elements.append(
            ElementPlace(order=order, begin=begin, end=self.length, element=element)
        )
        if element.tag in BLOCK_LEVEL_TAGS:
            self.write(WORD_GAP)

    def run(
        self,
        order: int,
        begin: int,
        parent: html.HtmlElement,
        after: html.HtmlElement | None,
        children: list[html.HtmlElement],
    ) -> RunPlace:
        return RunPlace(
            order=order,
            begin=begin,
            end=self.length,
            parent=parent,
            after=after,
            children=children,
        )

    def text(self) -> str:
        return "".join(self.pieces)


def page_tree(content: bytes, address: str) -> html.HtmlElement:
    # The page is decoded and parsed as the extractor does it.
    page = trafilatura.load_html(content)
    if page is None:
        raise UnreadableSource(NO_TEXT.format(source=address))
    return page


def is_script_address(value: str) -> bool:
    return SCRIPT_ADDRESS.match(ADDRESS_NOISE.sub("", value)) is not None


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
        elif tag in (SCRIPT, BASE) or is_page_instruction(element):
            element.drop_tree()


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


def unread_parts(page: html.HtmlElement, address: str) -> set[html.HtmlElement]:
    # The parts that the extractor drops before it reads are found by dropping them from a
    # copy of the page, and they stay shown.
    elements = list(page.iter(etree.Element))
    for number, element in enumerate(elements):
        element.set(COPY_MARK, str(number))
    copy = deepcopy(page)
    drop_unread_parts(copy, address)
    kept = {element.get(COPY_MARK) for element in copy.iter(etree.Element)}
    for element in elements:
        del element.attrib[COPY_MARK]
    return {element for number, element in enumerate(elements) if str(number) not in kept}


def comparable_key(text: str) -> str:
    return comparable(text, ignored=set()).text.strip()


def cell_key(text: str) -> str:
    return CELL_FILLER.sub("", text)


def table_key(rows: list[list[str]]) -> str:
    # Cells that say nothing are left out: the extractor adds some where a cell above spans
    # rows, and leaves out rows that say nothing.
    kept = [[cell for cell in cells if cell] for cells in rows]
    return TABLE_KEY + ROW_SEPARATOR.join(CELL_SEPARATOR.join(cells) for cells in kept if cells)


def picture_key(address: str) -> str:
    parts = urlsplit(address)
    return f"{FIGURE_KEY}{parts.path}?{parts.query}"


def block_key(block: Block) -> str:
    match block:
        case Table(text=text):
            rows = [
                [cell_key(comparable_key(cell)) for cell in row.split(CELL_SEPARATOR)]
                for row in text.split(ROW_SEPARATOR)
            ]
            return table_key(rows)
        case Figure(source=source):
            return picture_key(source)
        case _:
            return comparable_key(block.text)


def picture_keys(picture: html.HtmlElement, base: str) -> set[str]:
    return {
        picture_key(urljoin(base, value.strip()))
        for name, value in picture.attrib.items()
        if name == PICTURE_ADDRESS or name.startswith(PICTURE_ADDRESS_PREFIX)
    }


def holds_embedded(place: Place) -> bool:
    match place:
        case ElementPlace(element=element):
            roots = [element]
        case RunPlace(children=children):
            roots = children
    return any(next(root.iter(EMBEDDED_TAGS), None) is not None for root in roots)


def page_candidates(
    page: html.HtmlElement, unread: set[html.HtmlElement], base: str, wanted: set[str]
) -> list[Candidate]:
    # The elements and runs of the page's body whose key is wanted, in document order, keyed
    # by their text as citations compare it, a table by its cells and a picture by its
    # address, as the extractor gives them.
    body = page.find(BODY)
    if body is None:
        return []
    shown = ShownText(unread)
    shown.walk(body)
    text = comparable(shown.text(), ignored=set())

    def key_of(place: Place) -> str:
        found = text.text[
            bisect_left(text.origins, place.begin) : bisect_left(text.origins, place.end)
        ]
        return found.strip()

    elements = {place.element: place for place in shown.elements}
    candidates: list[Candidate] = []
    places: list[Place] = [*shown.elements, *shown.runs]
    for place in sorted(places, key=lambda place: place.order):
        match place:
            case ElementPlace(element=element) if element.tag == TABLE:
                rows = [
                    [cell_key(key_of(elements[cell])) for cell in CELLS(row) if cell in elements]
                    for row in OWN_ROWS(element)
                ]
                keys = [table_key(rows)]
            case ElementPlace(element=element) if element.tag == PICTURE:
                keys = sorted(picture_keys(element, base) & wanted)[:1]
            case _:
                keys = [key_of(place)]
        candidates.extend(Candidate(key=key, place=place) for key in keys if key in wanted)
    return candidates


def is_nested(inner: Place, outer: Place) -> bool:
    return outer.begin <= inner.begin and inner.end <= outer.end


def placeable(candidates: list[Candidate]) -> list[Candidate]:
    # A place that holds a picture or a control besides its text is not one; of places that
    # hold the same text one inside the other, the innermost is taken, which keeps the most of
    # the page around the text.
    kept: list[Candidate] = []
    for candidate in candidates:
        is_picture = candidate.key.startswith(FIGURE_KEY)
        if not is_picture and holds_embedded(candidate.place):
            continue
        if kept and kept[-1].key == candidate.key and is_nested(candidate.place, kept[-1].place):
            kept[-1] = candidate
        else:
            kept.append(candidate)
    return kept


def mark(place: Place, number: BlockNumber) -> None:
    match place:
        case ElementPlace(element=element):
            element.set(BLOCK_ATTRIBUTE, str(number))
        case RunPlace(parent=parent, after=after, children=children):
            wrapper = html.Element(RUN_ELEMENT)
            wrapper.set(BLOCK_ATTRIBUTE, str(number))
            if after is None:
                wrapper.text, parent.text = parent.text, None
                parent.insert(0, wrapper)
            else:
                wrapper.text, after.tail = after.tail, None
                after.addnext(wrapper)
            wrapper.extend(children)


def placed_blocks(
    page: html.HtmlElement, unread: set[html.HtmlElement], base: str, blocks: list[Block]
) -> list[BlockNumber]:
    # The blocks are matched to places in the order both have, so that of the places that
    # hold the same text, the one among the blocks around it is taken.
    keys = [block_key(block) for block in blocks]
    wanted = {key for key in keys if key}
    candidates = placeable(page_candidates(page, unread, base, wanted))
    matcher = SequenceMatcher(None, keys, [candidate.key for candidate in candidates], False)
    pairs = [
        (match.a + step, candidates[match.b + step].place)
        for match in matcher.get_matching_blocks()
        for step in range(match.size)
    ]
    for number, place in pairs:
        mark(place, number)
    return [number for number, _ in pairs]


def with_head(page: html.HtmlElement, base: str) -> None:
    # The page sends no referrer, and its addresses lead to its site. Both are set before
    # anything in the page loads.
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
    # The page an item was read from, made ready to be shown in a frame that runs none of
    # its scripts, with the elements that hold the blocks' text marked. Its dialogs are left
    # out, as the extractor leaves them out: shown without the script that closes them, they
    # would cover the page.
    page = page_tree(content, heading_address(origin))
    doctype = page.getroottree().docinfo.doctype
    base = base_address(page, origin)
    made_inert(page)
    drop_dialogs(page)
    placed = placed_blocks(page, unread_parts(page, heading_address(origin)), base, blocks)
    with_head(page, base)
    serialized = html.tostring(page, encoding="unicode")
    return FramedPage(html=f"{doctype}\n{serialized}" if doctype else serialized, placed=placed)
