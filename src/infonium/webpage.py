import re
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
    Quote,
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
SETTING_TAGS = frozenset({Tag.HEAD, Tag.ITEM, Tag.QUOTE})


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
        self.marks: list[Mark] = []

    def write(self, piece: str | None) -> None:
        collapsed = WHITESPACE.sub(" ", piece or "")
        at_line_start = self.text[-1:] in ("", " ", "\n")
        self.text += collapsed.lstrip(" ") if at_line_start else collapsed

    def break_line(self) -> None:
        if self.text:
            self.text = self.text.rstrip(" ") + "\n"

    def write_inline(self, element: etree._Element) -> None:
        if element.tag == Tag.BREAK:
            self.break_line()
        else:
            start = len(self.text)
            self.write(element.text)
            for child in element:
                self.write_inline(child)
                self.write(child.tail)
            self.marks.extend(marks_of(element, start=start, end=len(self.text)))

    def trimmed_text(self) -> str:
        return self.text.rstrip()

    def trimmed_marks(self) -> list[Mark]:
        length = len(self.trimmed_text())
        return [
            mark.model_copy(update={"end": min(mark.end, length)})
            for mark in self.marks
            if mark.start < min(mark.end, length)
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


def is_code_block(code: etree._Element) -> bool:
    parent = code.getparent()
    stands_alone = parent is not None and parent.tag == Tag.BODY
    return stands_alone or "\n" in "".join(code.itertext()).strip()


def is_inline(element: etree._Element) -> bool:
    match element.tag:
        case Tag.CODE:
            return not is_code_block(element)
        case Tag.GRAPHIC:
            return False
        case tag:
            return tag not in FLOW_TAGS


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


def run_block(container: etree._Element, text: str, marks: list[Mark]) -> Block:
    setting = next(
        (
            ancestor
            for ancestor in (container, *container.iterancestors())
            if ancestor.tag in SETTING_TAGS
        ),
        container,
    )
    match setting.tag:
        case Tag.HEAD:
            level = HEADING_LEVELS.get(setting.get(RENDITION, ""), DEFAULT_HEADING_LEVEL)
            return Heading(level=level, text=text, marks=marks)
        case Tag.ITEM:
            lists = list(setting.iterancestors(Tag.LIST))
            ordered = lists[0].get(RENDITION) == ORDERED_LIST
            return ListItem(ordered=ordered, depth=len(lists) - 1, text=text, marks=marks)
        case Tag.QUOTE:
            return Quote(text=text, marks=marks)
        case _:
            return Paragraph(text=text, marks=marks)


def run_blocks(container: etree._Element, run: Run) -> list[Block]:
    text = run.trimmed_text()
    # A run without a letter or digit is leftover punctuation, such as a stripped link's brackets.
    has_content = any(character.isalnum() for character in text)
    return [run_block(container, text=text, marks=run.trimmed_marks())] if has_content else []


def flowed_blocks(container: etree._Element) -> list[Block]:
    blocks: list[Block] = []
    run = Run()
    run.write(container.text)
    for child in container:
        if is_inline(child):
            run.write_inline(child)
        else:
            blocks.extend(run_blocks(container, run))
            blocks.extend(blocks_of(child))
            run = Run()
        run.write(child.tail)
    blocks.extend(run_blocks(container, run))
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
            return [Code(text="".join(element.itertext()).strip("\n"))]
        case _:
            return flowed_blocks(element)


def fetch_page(url: str) -> FetchedPage:
    response = httpx.get(
        url,
        follow_redirects=True,
        timeout=FETCH_TIMEOUT_SECONDS,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    return FetchedPage(url=str(response.url.copy_with(fragment=None)), content=response.content)


def read_page(content: bytes, url: str) -> WebPage:
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
        raise UnreadablePage(url)
    blocks = flowed_blocks(extraction.body)
    if not blocks:
        raise UnreadablePage(url)
    return WebPage(title=extraction.title or url, blocks=blocks)
