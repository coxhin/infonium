import re
from bisect import bisect_left
from copy import deepcopy
from difflib import SequenceMatcher
from itertools import count
from urllib.parse import urljoin, urlsplit

from lxml import etree, html
from pydantic import BaseModel, ConfigDict

from infonium.anchor import comparable
from infonium.document import CELL_SEPARATOR, ROW_SEPARATOR, Block, BlockNumber, Figure, Table
from infonium.htmlpage import INLINE_QUOTATION, PICTURE, drop_unread_parts

# The attribute that carries a block's number on the element that holds the block's text,
# and the element that holds a block's text where the text is only part of an element. The
# element's name is one that no page's stylesheet names, and like any unknown element it is
# laid out inline, so it changes no layout around the inline content it holds.
BLOCK_ATTRIBUTE = "data-infonium-block"
RUN_ELEMENT = "infonium-run"

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
BODY = "body"
# The extractor does not read what a style hides.
HIDDEN_BY_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
STYLE_ATTRIBUTE = "style"
# Elements whose text the extractor does not read: code for the browser, and words the author
# struck out.
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


class ElementHolder(BaseModel):
    # A whole element that may hold a block's text; order is its place in the walk of the
    # page, and begin and end bound its text in the page's shown text.
    model_config = ConfigDict(arbitrary_types_allowed=True)
    order: int
    begin: int
    end: int
    element: html.HtmlElement


class RunHolder(BaseModel):
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


Holder = ElementHolder | RunHolder


class Candidate(BaseModel):
    key: str
    holder: Holder


class ShownText:
    # The text a reader sees in the page's body, walked in document order, with where each
    # element and each run of inline content stands in it.
    def __init__(self, unread: set[html.HtmlElement]) -> None:
        self.unread = unread
        self.pieces: list[str] = []
        self.length = 0
        self.orders = count()
        self.elements: list[ElementHolder] = []
        self.runs: list[RunHolder] = []

    def write(self, piece: str | None) -> None:
        if piece:
            self.pieces.append(piece)
            self.length += len(piece)

    def is_read(self, node: etree._Element) -> bool:
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
        runs: list[RunHolder] = []
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
            ElementHolder(order=order, begin=begin, end=self.length, element=element)
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
    ) -> RunHolder:
        # Line breaks at the edges of a run part it from what is around it, and stay outside
        # it, so that replacing the run's content keeps them.
        leading_text = parent.text if after is None else after.tail
        while children and is_bare_break(children[0]) and not (leading_text or "").strip():
            after, children = children[0], children[1:]
            leading_text = after.tail
        while children and is_bare_break(children[-1]):
            children = children[:-1]
        return RunHolder(
            order=order,
            begin=begin,
            end=self.length,
            parent=parent,
            after=after,
            children=children,
        )

    def text(self) -> str:
        return "".join(self.pieces)


def is_bare_break(node: html.HtmlElement) -> bool:
    # A line break with nothing but spaces after it.
    return node.tag == LINE_BREAK_TAG and not (node.tail or "").strip()


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


def holds_embedded(holder: Holder) -> bool:
    match holder:
        case ElementHolder(element=element):
            roots = [element]
        case RunHolder(children=children):
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

    def key_of(holder: Holder) -> str:
        found = text.text[
            bisect_left(text.origins, holder.begin) : bisect_left(text.origins, holder.end)
        ]
        return found.strip()

    elements = {holder.element: holder for holder in shown.elements}
    candidates: list[Candidate] = []
    holders: list[Holder] = [*shown.elements, *shown.runs]
    for holder in sorted(holders, key=lambda holder: holder.order):
        match holder:
            case ElementHolder(element=element) if element.tag == TABLE:
                rows = [
                    [cell_key(key_of(elements[cell])) for cell in CELLS(row) if cell in elements]
                    for row in OWN_ROWS(element)
                ]
                keys = [table_key(rows)]
            case ElementHolder(element=element) if element.tag == PICTURE:
                keys = sorted(picture_keys(element, base) & wanted)[:1]
            case _:
                keys = [key_of(holder)]
        candidates.extend(Candidate(key=key, holder=holder) for key in keys if key in wanted)
    return candidates


def is_nested(inner: Holder, outer: Holder) -> bool:
    return outer.begin <= inner.begin and inner.end <= outer.end


def placeable(candidates: list[Candidate]) -> list[Candidate]:
    # An element or run that holds a picture or a control besides its text holds no block. Of
    # those that hold the same text one inside the other, the innermost is taken, which keeps
    # the most of the page around the text.
    kept: list[Candidate] = []
    for candidate in candidates:
        is_picture = candidate.key.startswith(FIGURE_KEY)
        if not is_picture and holds_embedded(candidate.holder):
            continue
        if kept and kept[-1].key == candidate.key and is_nested(candidate.holder, kept[-1].holder):
            kept[-1] = candidate
        else:
            kept.append(candidate)
    return kept


def mark(holder: Holder, number: BlockNumber) -> None:
    match holder:
        case ElementHolder(element=element):
            element.set(BLOCK_ATTRIBUTE, str(number))
        case RunHolder(parent=parent, after=after, children=children):
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
    # The blocks are matched to holders in the order both have, so that of the holders that
    # hold the same text, the one among the blocks around it is taken.
    keys = [block_key(block) for block in blocks]
    wanted = {key for key in keys if key}
    candidates = placeable(page_candidates(page, unread, base, wanted))
    matcher = SequenceMatcher(None, keys, [candidate.key for candidate in candidates], False)
    pairs = [
        (match.a + step, candidates[match.b + step].holder)
        for match in matcher.get_matching_blocks()
        for step in range(match.size)
    ]
    for number, holder in pairs:
        mark(holder, number)
    return [number for number, _ in pairs]
