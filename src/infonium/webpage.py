import string
from enum import StrEnum
from itertools import takewhile

import trafilatura
from lxml import etree
from pydantic import BaseModel
from trafilatura.settings import Document as Extraction

from infonium.document import (
    LINE_BREAK,
    NO_TEXT,
    WEB_SCHEMES,
    Block,
    Code,
    Extracted,
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
    UnreadableSource,
    tabled,
)
from infonium.htmlpage import HEADING_LEVELS, WHITESPACE, prepared_page

EXTRACTOR = "webpage/1"
LEFTOVER_CHARACTERS = frozenset(string.punctuation + string.whitespace)


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
            end = len(self.text)
            # A mark covers words; a space before or after them belongs to the sentence.
            if self.text[start:end].startswith(" "):
                start += 1
            if self.text[start:end].endswith(" "):
                end -= 1
            self.spans.append((element, start, end))

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


def is_in_sentence(element: etree._Element) -> bool:
    # Words directly before or after an element show that it sits in a sentence.
    parent = element.getparent()
    earlier = element.getprevious()
    parent_text = parent.text if parent is not None else None
    before = earlier.tail if earlier is not None else parent_text
    return bool((before or "").strip() or (element.tail or "").strip())


def is_inline(element: etree._Element) -> bool:
    match element.tag:
        case Tag.CODE:
            parent = element.getparent()
            # Preformatted text arrives as code that wraps code, whatever its length.
            preformatted = element.find(Tag.CODE) is not None
            several_lines = LINE_BREAK in verbatim_text(element).strip()
            under_body = parent is not None and parent.tag == Tag.BODY
            stands_alone = under_body and not is_in_sentence(element)
            return not (preformatted or several_lines or stands_alone)
        case Tag.GRAPHIC:
            return False
        case tag:
            return tag not in FLOW_TAGS


def is_code_listing(quote: etree._Element) -> bool:
    # A quotation that holds nothing but code is a listing with one piece of code per line.
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
    return tabled(
        [[cell_text(cell) for cell in row.findall(Tag.CELL)] for row in table.findall(Tag.ROW)]
    )


def block_of_run(container: etree._Element, text: str, marks: list[Mark]) -> Block:
    # A run takes its kind from the nearest heading or quotation around it. The search stops
    # at a list item, whose runs are told apart from the rest of its content by item_blocks.
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


def is_definition(item: etree._Element) -> bool:
    # The terms and descriptions of a definition list read as paragraphs, not as list items.
    return next(item.iterancestors(Tag.LIST)).get(RENDITION) == DEFINITION_LIST


def item_blocks(item: etree._Element) -> list[Block]:
    # The paragraphs of an item's own text become its list items: the first carries the item's
    # place in the list, and each later one continues it.
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


def is_leftover(block: Block) -> bool:
    # A block of nothing but punctuation is what is left of something stripped from the page,
    # such as the brackets around a removed link.
    return isinstance(block, Heading | Paragraph | ListItem | Quotation) and all(
        character in LEFTOVER_CHARACTERS for character in block.text
    )


class PageText(BaseModel):
    # What the extractor finds in a page, which may be nothing.
    title: str
    blocks: list[Block]


def page_text(content: bytes, url: str) -> PageText:
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
        return PageText(title=url, blocks=[])
    blocks = [block for block in flowed_blocks(extraction.body) if not is_leftover(block)]
    return PageText(title=extraction.title or url, blocks=blocks)


def extracted_text(text: PageText, url: str) -> Extracted:
    if not text.blocks:
        raise UnreadableSource(NO_TEXT.format(source=url))
    return Extracted(title=text.title, blocks=text.blocks)


def extract_page(content: bytes, url: str) -> Extracted:
    return extracted_text(page_text(content, url), url)
