import re
import string
from enum import StrEnum

import httpx
import trafilatura
from lxml import etree
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


class Tag(StrEnum):
    BODY = "body"
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
    {Tag.PARAGRAPH, Tag.HEAD, Tag.QUOTE, Tag.LIST, Tag.ITEM, Tag.TABLE, Tag.ROW, Tag.CELL}
)
WRAPPER_TAGS = frozenset({Tag.HEAD, Tag.ITEM, Tag.QUOTE})
INLINE_HOST_TAGS = frozenset({Tag.PARAGRAPH, Tag.HEAD, Tag.HIGHLIGHT, Tag.REFERENCE})


class UnreadablePage(Exception):
    pass


class FetchedPage(BaseModel):
    url: str
    content: bytes


class WebPage(BaseModel):
    title: str
    blocks: list[Block]


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

    def write_content(self, element: etree._Element) -> None:
        start = len(self.text)
        self.write(element.text)
        for child in element:
            self.write_inline(child)
            self.write(child.tail)
        self.spans.append((element, start, len(self.text)))

    def write_inline(self, element: etree._Element) -> None:
        if element.tag == Tag.BREAK:
            self.break_line()
        elif element.tag == Tag.QUOTE:
            # The page's quotation marks around a short inline quotation come from its style
            # sheet, so the text does not hold them.
            self.write(OPENING_QUOTATION_MARK)
            self.write_content(element)
            self.write(CLOSING_QUOTATION_MARK)
        elif element.tag != Tag.DELETION:
            # Text the author struck out is left out: it no longer says what the page says.
            self.write_content(element)

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
    parent = element.getparent()
    in_text = parent is not None and parent.tag in INLINE_HOST_TAGS
    stands_alone = parent is not None and parent.tag == Tag.BODY
    match element.tag:
        case Tag.CODE:
            several_lines = LINE_BREAK in verbatim_text(element).strip()
            return in_text or not (stands_alone or several_lines)
        case Tag.QUOTE:
            return in_text
        case Tag.GRAPHIC:
            return False
        case tag:
            return tag not in FLOW_TAGS


# Preformatted text reaches us as a quotation; one that holds nothing but code is a listing
# with one piece of code per line.
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


def item_depth(item: etree._Element) -> int:
    return len(list(item.iterancestors(Tag.LIST))) - 1


def block_of_run(container: etree._Element, text: str, marks: list[Mark]) -> Block:
    wrapper = next(
        (
            ancestor
            for ancestor in (container, *container.iterancestors())
            if ancestor.tag in WRAPPER_TAGS
        ),
        container,
    )
    match wrapper.tag:
        case Tag.HEAD:
            level = HEADING_LEVELS.get(wrapper.get(RENDITION, ""), DEFAULT_HEADING_LEVEL)
            return Heading(level=level, text=text, marks=marks)
        case Tag.ITEM:
            return ListItem(
                ordered=next(wrapper.iterancestors(Tag.LIST)).get(RENDITION) == ORDERED_LIST,
                depth=item_depth(wrapper),
                position=len(list(wrapper.itersiblings(Tag.ITEM, preceding=True))) + 1,
                continues=False,
                text=text,
                marks=marks,
            )
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
        if is_inline(child):
            run.write_inline(child)
        else:
            blocks.extend(blocks_of_run(container, run))
            blocks.extend(blocks_of(child))
            run = Run()
        run.write(child.tail)
    blocks.extend(blocks_of_run(container, run))
    return blocks


# Every block of an item's own text after the first continues that item.
def item_blocks(item: etree._Element) -> list[Block]:
    depth = item_depth(item)
    blocks: list[Block] = []
    started = False
    for block in flowed_blocks(item):
        if isinstance(block, ListItem) and block.depth == depth:
            blocks.append(
                ListItem(
                    ordered=block.ordered,
                    depth=block.depth,
                    position=block.position,
                    continues=started,
                    text=block.text,
                    marks=block.marks,
                )
            )
            started = True
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
            return [Code(text=LINE_BREAK.join(verbatim_text(line) for line in element))]
        case Tag.ITEM:
            return item_blocks(element)
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
        content,
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
