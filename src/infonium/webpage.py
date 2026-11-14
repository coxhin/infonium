import re
import string
from enum import StrEnum
from itertools import takewhile

import httpx
import trafilatura
from lxml import etree, html
from pydantic import BaseModel
from trafilatura.settings import Document as Extraction

from infonium.document import (
    CELL_SEPARATOR,
    ROW_SEPARATOR,
    Block,
    Code,
    Figure,
    Heading,
    LinkMark,
    ListItem,
    Mark,
    Paragraph,
    Quotation,
    Style,
    StyleMark,
    Table,
)

EXTRACTOR = "webpage/1"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36 infonium/0.1"
)
FETCH_TIMEOUT_SECONDS = 20.0
WEB_SCHEMES = ("http://", "https://")
WHITESPACE = re.compile(r"\s+")
LINE_BREAK = "\n"
OPENING_QUOTATION_MARK = "\N{LEFT DOUBLE QUOTATION MARK}"
CLOSING_QUOTATION_MARK = "\N{RIGHT DOUBLE QUOTATION MARK}"
LEFTOVER_CHARACTERS = frozenset(string.punctuation + string.whitespace)

INLINE_QUOTATION = "q"
CODE_ELEMENT = "code"
BARE_PREFORMATTED = "//pre[not(.//code)]"
CODE_IN_TEXT = "//code[not(ancestor::pre)]"


class Tag(StrEnum):
    BODY = "body"
    DIVISION = "div"
    ANONYMOUS_BLOCK = "ab"
    PARAGRAPH = "p"
    HEAD = "head"
    QUOTE = "quote"
    LIST = "list"
    ITEM = "item"
    TABLE = "table"
    ROW = "row"
    CELL = "cell"
    GRAPHIC = "graphic"
    CODE = "code"
    BREAK = "lb"
    HIGHLIGHT = "hi"
    REFERENCE = "ref"
    DELETION = "del"


RENDITION = "rend"
TARGET = "target"
SOURCE = "src"
DESCRIPTION = "alt"
ORDERED_LIST = "ol"
DEFINITION_LIST = "dl"
HEADING_LEVELS = {f"h{level}": level for level in range(1, 7)}
DEFAULT_HEADING_LEVEL = 2
CROWDED_CELL_BLOCKS = 5
CELL_PART_SEPARATOR = "; "
STYLES = {
    "#b": Style.STRONG,
    "#i": Style.EMPHASIS,
    "#u": Style.UNDERLINE,
    "#t": Style.CODE,
    "#sub": Style.SUBSCRIPT,
    "#sup": Style.SUPERSCRIPT,
}
FLOW_TAGS = frozenset(
    {
        Tag.DIVISION,
        Tag.ANONYMOUS_BLOCK,
        Tag.PARAGRAPH,
        Tag.HEAD,
        Tag.QUOTE,
        Tag.LIST,
        Tag.ITEM,
        Tag.TABLE,
        Tag.ROW,
        Tag.CELL,
    }
)
WRAPPER_TAGS = frozenset({Tag.HEAD, Tag.QUOTE})


class UnreadablePage(Exception):
    pass


class FetchedPage(BaseModel):
    url: str
    content: bytes


class WebPage(BaseModel):
    title: str
    blocks: list[Block]


# The extractor turns a short inline quotation and a block quotation into the same element,
# and so with preformatted text and code, and with code in a sentence and code on lines of
# its own. The page is rewritten first so that each pair stays apart: an inline quotation
# becomes its words inside quotation marks, preformatted text is wrapped as code, and code
# in a sentence loses its line breaks.
def prepared_page(content: bytes, url: str) -> html.HtmlElement:
    page = trafilatura.load_html(content)
    if page is None:
        raise UnreadablePage(f"no readable text at {url}")
    for quotation in list(page.iter(INLINE_QUOTATION)):
        quotation.text = OPENING_QUOTATION_MARK + (quotation.text or "")
        if len(quotation):
            quotation[-1].tail = (quotation[-1].tail or "") + CLOSING_QUOTATION_MARK
        else:
            quotation.text += CLOSING_QUOTATION_MARK
        quotation.drop_tag()
    for preformatted in page.xpath(BARE_PREFORMATTED):
        code = html.Element(CODE_ELEMENT)
        code.text, preformatted.text = preformatted.text, None
        code.extend(list(preformatted))
        preformatted.append(code)
    for code in page.xpath(CODE_IN_TEXT):
        for piece in code.iter():
            piece.text = WHITESPACE.sub(" ", piece.text or "")
            piece.tail = piece.tail if piece is code else WHITESPACE.sub(" ", piece.tail or "")
    return page


class Run:
    def __init__(self) -> None:
        self.text = ""
        self.spans: list[tuple[etree._Element, int, int]] = []

    def write(self, piece: str | None) -> None:
        collapsed = WHITESPACE.sub(" ", piece or "")
        at_line_start = self.text[-1:] in ("", " ", LINE_BREAK)
        self.text += collapsed.lstrip(" ") if at_line_start else collapsed

    def break_line(self) -> None:
        if self.text:
            self.text = self.text.rstrip(" ") + LINE_BREAK

    def write_inline(self, element: etree._Element) -> None:
        if element.tag == Tag.BREAK:
            self.break_line()
        elif element.tag != Tag.DELETION:
            # Text the author struck out is left out: it no longer says what the page says.
            start = len(self.text)
            self.write(element.text)
            for child in element:
                self.write_inline(child)
                self.write(child.tail)
            self.spans.append((element, start, len(self.text)))

    def trimmed_text(self) -> str:
        return self.text.rstrip()

    def trimmed_marks(self) -> list[Mark]:
        length = len(self.trimmed_text())
        return [
            mark
            for element, start, end in self.spans
            if start < min(end, length)
            for mark in marks_of(element, start=start, end=min(end, length))
        ]


def marks_of(element: etree._Element, start: int, end: int) -> list[Mark]:
    rendition = element.get(RENDITION, "")
    target = element.get(TARGET, "")
    match element.tag:
        case Tag.HIGHLIGHT if rendition in STYLES:
            return [StyleMark(style=STYLES[rendition], start=start, end=end)]
        case Tag.CODE:
            return [StyleMark(style=Style.CODE, start=start, end=end)]
        case Tag.REFERENCE if target.startswith(WEB_SCHEMES):
            return [LinkMark(target=target, start=start, end=end)]
        case _:
            return []


def verbatim_text(element: etree._Element) -> str:
    pieces = [element.text or ""]
    for child in element:
        pieces.append(LINE_BREAK if child.tag == Tag.BREAK else verbatim_text(child))
        pieces.append(child.tail or "")
    return "".join(pieces)


def is_inline(element: etree._Element) -> bool:
    match element.tag:
        case Tag.CODE:
            parent = element.getparent()
            stands_alone = parent is not None and parent.tag == Tag.BODY
            several_lines = LINE_BREAK in verbatim_text(element).strip()
            return not (stands_alone or several_lines)
        case Tag.GRAPHIC:
            return False
        case tag:
            return tag not in FLOW_TAGS


# A quotation that holds nothing but code is a listing with one piece of code per line.
def is_code_listing(quote: etree._Element) -> bool:
    lines = list(quote)
    loose_text = (quote.text or "") + "".join(line.tail or "" for line in lines)
    return bool(lines) and all(line.tag == Tag.CODE for line in lines) and not loose_text.strip()


def is_data_table(table: etree._Element) -> bool:
    # A page laid out with tables keeps its whole text in one cell; a data cell holds a few blocks.
    return not any(
        len([child for child in cell if child.tag in FLOW_TAGS]) > CROWDED_CELL_BLOCKS
        for row in table.findall(Tag.ROW)
        for cell in row.findall(Tag.CELL)
    )


def cell_text(cell: etree._Element) -> str:
    lines = [line for block in flowed_blocks(cell) for line in block.text.splitlines() if line]
    return WHITESPACE.sub(" ", CELL_PART_SEPARATOR.join(lines))


def table_text(table: etree._Element) -> str:
    rows = [[cell_text(cell) for cell in row.findall(Tag.CELL)] for row in table.findall(Tag.ROW)]
    return ROW_SEPARATOR.join(CELL_SEPARATOR.join(cells) for cells in rows if any(cells))


# A run takes its kind from the nearest heading or quotation around it. The search stops at
# a list item, whose runs are told apart from the rest of its content by item_blocks.
def block_of_run(container: etree._Element, text: str, marks: list[Mark]) -> Block:
    within_item = takewhile(
        lambda element: element.tag != Tag.ITEM, (container, *container.iterancestors())
    )
    wrapper = next((element for element in within_item if element.tag in WRAPPER_TAGS), container)
    match wrapper.tag:
        case Tag.HEAD:
            level = HEADING_LEVELS.get(wrapper.get(RENDITION, ""), DEFAULT_HEADING_LEVEL)
            return Heading(level=level, text=text, marks=marks)
        case Tag.QUOTE:
            return Quotation(text=text, marks=marks)
        case _:
            return Paragraph(text=text, marks=marks)


def blocks_of_run(container: etree._Element, run: Run) -> list[Block]:
    text = run.trimmed_text()
    return [block_of_run(container, text=text, marks=run.trimmed_marks())] if text else []


def flowed_blocks(container: etree._Element) -> list[Block]:
    blocks: list[Block] = []
    run = Run()
    run.write(container.text)
    for child in container:
        # Two line breaks in a row are how pages without paragraph elements part paragraphs.
        parts_paragraphs = child.tag == Tag.BREAK and run.text.endswith(LINE_BREAK)
        if is_inline(child) and not parts_paragraphs:
            run.write_inline(child)
        else:
            blocks.extend(blocks_of_run(container, run))
            blocks.extend(blocks_of(child))
            run = Run()
        run.write(child.tail)
    blocks.extend(blocks_of_run(container, run))
    return blocks


# The terms and descriptions of a definition list read as paragraphs, not as list items.
def is_definition(item: etree._Element) -> bool:
    return next(item.iterancestors(Tag.LIST)).get(RENDITION) == DEFINITION_LIST


# The paragraphs of an item's own text become its list items: the first carries the item's
# place in the list, and each later one continues it.
def item_blocks(item: etree._Element) -> list[Block]:
    enclosing = list(item.iterancestors(Tag.LIST))
    ordered = enclosing[0].get(RENDITION) == ORDERED_LIST
    depth = len(enclosing) - 1
    position = len(list(item.itersiblings(Tag.ITEM, preceding=True))) + 1
    blocks: list[Block] = []
    for block in flowed_blocks(item):
        if isinstance(block, Paragraph):
            # Items of lists nested in this one are deeper, so only its own count here.
            continues = any(
                isinstance(earlier, ListItem) and earlier.depth == depth for earlier in blocks
            )
            blocks.append(
                ListItem(
                    ordered=ordered,
                    depth=depth,
                    position=position,
                    continues=continues,
                    text=block.text,
                    marks=block.marks,
                )
            )
        else:
            blocks.append(block)
    return blocks


def blocks_of(element: etree._Element) -> list[Block]:
    match element.tag:
        case Tag.TABLE if is_data_table(element):
            text = table_text(element)
            return [Table(text=text)] if text else []
        case Tag.GRAPHIC:
            source = element.get(SOURCE, "")
            description = WHITESPACE.sub(" ", element.get(DESCRIPTION, "")).strip()
            is_web_image = source.startswith(WEB_SCHEMES)
            return [Figure(source=source, text=description)] if is_web_image else []
        case Tag.CODE:
            code = verbatim_text(element).strip(LINE_BREAK)
            return [Code(text=code)] if code.strip() else []
        case Tag.QUOTE if is_code_listing(element):
            lines = [verbatim_text(line).strip(LINE_BREAK) for line in element]
            code = LINE_BREAK.join(lines)
            return [Code(text=code)] if code.strip() else []
        case Tag.ITEM if not is_definition(element):
            return item_blocks(element)
        case Tag.BREAK:
            return []
        case _:
            return flowed_blocks(element)


# A block of nothing but punctuation is what is left of something stripped from the page,
# such as the brackets around a removed link.
def is_leftover(block: Block) -> bool:
    return isinstance(block, Heading | Paragraph | ListItem | Quotation) and all(
        character in LEFTOVER_CHARACTERS for character in block.text
    )


def fetch_page(url: str) -> FetchedPage:
    response = httpx.get(
        url,
        follow_redirects=True,
        timeout=FETCH_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return FetchedPage(url=str(response.url.copy_with(fragment=None)), content=response.content)


def extract_page(content: bytes, url: str) -> WebPage:
    extraction = trafilatura.bare_extraction(
        prepared_page(content, url),
        url=url,
        include_comments=False,
        include_formatting=True,
        include_links=True,
        include_images=True,
        with_metadata=True,
    )
    if not isinstance(extraction, Extraction):
        raise UnreadablePage(f"no readable text at {url}")
    blocks = [block for block in flowed_blocks(extraction.body) if not is_leftover(block)]
    if not blocks:
        raise UnreadablePage(f"no readable text at {url}")
    return WebPage(title=extraction.title or url, blocks=blocks)
