import re
from bisect import bisect_left
from collections import Counter, defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
from itertools import count, pairwise

from lxml import etree, html
from pydantic import BaseModel, ConfigDict
from trafilatura.htmlprocessing import CONVERSIONS, convert_deletions, convert_headings

from infonium.anchor import comparable
from infonium.document import (
    CELL_SEPARATOR,
    ROW_SEPARATOR,
    Block,
    BlockNumber,
    Figure,
    Heading,
    Table,
)
from infonium.htmlpage import (
    BODY,
    CODE_FOR_THE_BROWSER,
    HEAD,
    INLINE_QUOTATION,
    IS_HIDDEN_BY_STYLE,
    PICTURE,
    drop_unread_parts,
    holds_words,
)

# The names infonium gives what it adds to a page. What a page itself wrote in these names is
# taken out before anything is marked, so that every mark in the page is infonium's.
OWN_ATTRIBUTE_PREFIX = "data-infonium-"
OWN_ELEMENT_PREFIX = "infonium-"
# The attribute that carries a block's number on the element that holds the block's text,
# and the element that holds a block's text where the text is only part of an element. The
# element's name is one that no page's stylesheet names, and like any unknown element it is
# laid out inline, so it changes no layout around the inline content it holds.
BLOCK_ATTRIBUTE = f"{OWN_ATTRIBUTE_PREFIX}block"
RUN_ELEMENT = f"{OWN_ELEMENT_PREFIX}run"
# The attribute that a cell of a table carries when it holds a cell of a table block's text:
# the block's number, the row and the cell within the row, counted from 0 as the block's
# text gives them, parted by the separator.
CELL_ATTRIBUTE = f"{OWN_ATTRIBUTE_PREFIX}cell"
CELL_PLACE_SEPARATOR = ","
# The attribute by which elements of the page are found again in a copy of it.
COPY_MARK = f"{OWN_ATTRIBUTE_PREFIX}copy"

# Elements laid out as blocks, which part the runs of inline content around them. The
# extractor parts its blocks at the elements it converts into its own block elements, inside
# trafilatura, where no list of them is offered; this list is HTML's own block elements, which
# those are among.
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
# Words the author struck out, which the extractor leaves out, as trafilatura converts them.
STRUCK_OUT_TAGS = frozenset(
    tag for tag, conversion in CONVERSIONS.items() if conversion is convert_deletions
)
UNREAD_TAGS = frozenset(CODE_FOR_THE_BROWSER) | STRUCK_OUT_TAGS
# The elements whose text the extractor reads as a heading: those trafilatura converts into
# headings, and the summary of a details element, which it turns into one inside its
# conversion of details.
SUMMARY = "summary"
HEADING_TAGS = frozenset(
    tag for tag, conversion in CONVERSIONS.items() if conversion is convert_headings
) | {SUMMARY}
# What an element may hold besides its text that a reader or the rest of the page would lose
# if the element's content were replaced by a translation: an image map is named by a
# picture elsewhere. An element that holds one of these holds no block, nor does one that
# holds words for a browser without scripts, which a page shown without its scripts shows.
SCRIPTLESS = "noscript"
EMBEDDED_TAGS = (
    "map",
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
# Stylesheets that a placed element holds apply to the whole page, and are moved to the end
# of the page's head before the element is marked.
STYLESHEET_TAGS = ("style", "link")
TABLE = "table"
# A table's rows as the extractor reads them: its caption first, as a row of one cell.
OWN_ROWS = etree.XPath("./caption | ./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
CELLS = etree.XPath("self::caption | ./td | ./th")
QUOTATION_MARK = '"'
WORD_GAP = " "
CELL_FILLER = re.compile(r"[ ;]")
# Kinds of key, so that a table's cells never equal a text, and a heading's text is never
# taken for the same text standing elsewhere than in a heading.
TABLE_KEY = "\x01"
HEADING_KEY = "\x03"


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


class TableHolder(BaseModel):
    # A table whose cells that say something, row by row, hold the cells of a table block's
    # text that say something.
    model_config = ConfigDict(arbitrary_types_allowed=True)
    order: int
    begin: int
    end: int
    element: html.HtmlElement
    cells: list[html.HtmlElement]


Holder = ElementHolder | RunHolder | TableHolder


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
        # The extractor leaves out what a style hides, unless that would leave a seventh or
        # less of the text of the part it takes for the page's own; that exception is
        # trafilatura's, made inside it, and is not followed here.
        return (
            isinstance(node.tag, str)
            and node.tag not in UNREAD_TAGS
            and node not in self.unread
            and not IS_HIDDEN_BY_STYLE(node)
        )

    def parts_runs(self, node: etree._Element) -> bool:
        # The second of two line breaks in a row parts paragraphs, as webpage.flowed_blocks
        # parts them in the extractor's tree, where this rule cannot be shared.
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


def drop_own_names(page: html.HtmlElement) -> None:
    for element in list(page.iter(etree.Element)):
        for name in [name for name in element.attrib if name.startswith(OWN_ATTRIBUTE_PREFIX)]:
            del element.attrib[name]
        if str(element.tag).startswith(OWN_ELEMENT_PREFIX):
            element.drop_tag()


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


def table_cells(text: str) -> list[list[str]]:
    return [
        [cell_key(comparable_key(cell)) for cell in row.split(CELL_SEPARATOR)]
        for row in text.split(ROW_SEPARATOR)
    ]


def block_key(block: Block) -> str:
    # A figure has none: the words of a picture's description are not shown in a page.
    match block:
        case Table(text=text):
            return table_key(table_cells(text))
        case Heading(text=text):
            return HEADING_KEY + comparable_key(text)
        case Figure():
            return ""
        case _:
            return comparable_key(block.text)


def is_in_heading(element: html.HtmlElement) -> bool:
    return any(
        tag in HEADING_TAGS for tag in (element.tag, *(a.tag for a in element.iterancestors()))
    )


def held_roots(holder: Holder) -> list[html.HtmlElement]:
    match holder:
        case ElementHolder(element=element):
            return [element]
        case RunHolder(children=children):
            return children
        case TableHolder(cells=cells):
            return cells


def holds_embedded(holder: Holder) -> bool:
    return any(
        next(root.iter(EMBEDDED_TAGS), None) is not None
        or any(holds_words("".join(words.itertext())) for words in root.iter(SCRIPTLESS))
        for root in held_roots(holder)
    )


def page_candidates(
    page: html.HtmlElement, unread: set[html.HtmlElement], wanted: set[str]
) -> list[Candidate]:
    # The elements and runs of the page's body whose key is wanted, in document order, keyed
    # by their text as citations compare it, a heading's marked as such, and a table by its
    # cells, as the extractor gives them.
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
                    [
                        (cell, cell_key(key_of(elements[cell])))
                        for cell in CELLS(row)
                        if cell in elements
                    ]
                    for row in OWN_ROWS(element)
                ]
                key = table_key([[cell for _, cell in row] for row in rows])
                holder = TableHolder(
                    order=holder.order,
                    begin=holder.begin,
                    end=holder.end,
                    element=element,
                    cells=[cell for row in rows for cell, said in row if said],
                )
            case ElementHolder(element=element):
                kind = HEADING_KEY if is_in_heading(element) else ""
                key = kind + key_of(holder)
            case RunHolder(parent=parent):
                kind = HEADING_KEY if is_in_heading(parent) else ""
                key = kind + key_of(holder)
        if key in wanted:
            candidates.append(Candidate(key=key, holder=holder))
    return candidates


def is_nested(inner: Holder, outer: Holder) -> bool:
    return outer.begin <= inner.begin and inner.end <= outer.end


def placeable(candidates: list[Candidate]) -> list[Candidate]:
    # An element or run that holds a picture or a control besides its text holds no block, nor
    # does a table with such a cell. Of those that hold the same text one inside the other, the
    # innermost is taken, which keeps the most of the page around the text.
    kept: list[Candidate] = []
    for candidate in candidates:
        if holds_embedded(candidate.holder):
            continue
        if kept and kept[-1].key == candidate.key and is_nested(candidate.holder, kept[-1].holder):
            kept[-1] = candidate
        else:
            kept.append(candidate)
    return kept


def aligned(keys: list[str], candidates: list[Candidate]) -> list[tuple[int, int]]:
    # Blocks whose text stands once among the blocks and once in the page anchor the
    # alignment, where they stand in the same order in both. Between two anchors, the blocks of
    # a text are placed on the holders of that text there, in order, when there are as many of
    # each; otherwise neither the order nor the kind decides, and they are left unplaced.
    candidate_keys = [candidate.key for candidate in candidates]
    block_counts = Counter(keys)
    holder_counts = Counter(candidate_keys)
    matcher = SequenceMatcher(None, keys, candidate_keys, False)
    anchors = [
        (match.a + step, match.b + step)
        for match in matcher.get_matching_blocks()
        for step in range(match.size)
        if block_counts[keys[match.a + step]] == 1 and holder_counts[keys[match.a + step]] == 1
    ]
    bounds = [(-1, -1), *anchors, (len(keys), len(candidates))]
    pairs = list(anchors)
    for (first_block, first_holder), (next_block, next_holder) in pairwise(bounds):
        blocks_between: defaultdict[str, list[int]] = defaultdict(list)
        holders_between: defaultdict[str, list[int]] = defaultdict(list)
        for number in range(first_block + 1, next_block):
            if keys[number]:
                blocks_between[keys[number]].append(number)
        for position in range(first_holder + 1, next_holder):
            holders_between[candidate_keys[position]].append(position)
        for key, numbers in blocks_between.items():
            positions = holders_between[key]
            if len(positions) == len(numbers):
                pairs.extend(zip(numbers, positions, strict=True))
    return sorted(pairs)


def stylesheets_moved(roots: list[html.HtmlElement], head: html.HtmlElement) -> None:
    # A stylesheet goes to the end of the head, and the text after it stays where it was.
    for root in roots:
        for node in [node for node in root.iter(STYLESHEET_TAGS) if node is not root]:
            tail, node.tail = node.tail, None
            previous = node.getprevious()
            parent = node.getparent()
            if previous is not None:
                previous.tail = (previous.tail or "") + (tail or "")
            elif parent is not None:
                parent.text = (parent.text or "") + (tail or "")
            head.append(node)


def mark(holder: Holder, number: BlockNumber, block: Block, head: html.HtmlElement) -> None:
    match holder:
        case ElementHolder(element=element):
            stylesheets_moved([element], head)
            element.set(BLOCK_ATTRIBUTE, str(number))
        case RunHolder(parent=parent, after=after, children=children):
            wrapper = html.Element(RUN_ELEMENT)
            if after is None:
                wrapper.text, parent.text = parent.text, None
                parent.insert(0, wrapper)
            else:
                wrapper.text, after.tail = after.tail, None
                after.addnext(wrapper)
            wrapper.extend(children)
            stylesheets_moved([wrapper], head)
            wrapper.set(BLOCK_ATTRIBUTE, str(number))
        case TableHolder(cells=cells):
            stylesheets_moved(cells, head)
            said = [
                (row, cell)
                for row, texts in enumerate(table_cells(block.text))
                for cell, text in enumerate(texts)
                if text
            ]
            for element, (row, cell) in zip(cells, said, strict=True):
                place = CELL_PLACE_SEPARATOR.join(str(part) for part in (number, row, cell))
                element.set(CELL_ATTRIBUTE, place)


def placed_blocks(
    page: html.HtmlElement, unread: set[html.HtmlElement], blocks: list[Block]
) -> list[BlockNumber]:
    # The blocks are matched to holders in the order both have, so that of the holders that
    # hold the same text, the one among the blocks around it is taken.
    keys = [block_key(block) for block in blocks]
    candidates = placeable(page_candidates(page, unread, {key for key in keys if key}))
    pairs = aligned(keys, candidates)
    head = page.find(HEAD)
    if head is None:
        head = html.Element(HEAD)
        page.insert(0, head)
    for number, position in pairs:
        mark(candidates[position].holder, number, blocks[number], head)
    return [number for number, _ in pairs]
