import re
import string
from enum import StrEnum
from itertools import chain, takewhile
from urllib.parse import urldefrag, urljoin

import trafilatura
from lxml import etree, html
from pydantic import BaseModel
from trafilatura.htmlprocessing import REND_TAG_MAPPING
from trafilatura.settings import Document as Extraction

from infonium.document import (
    LINE_BREAK,
    NO_TEXT,
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

EXTRACTOR = "webpage/1"
WEB_SCHEMES = ("http://", "https://")
WHITESPACE = re.compile(r"\s+")
OPENING_QUOTATION_MARK = "\N{LEFT DOUBLE QUOTATION MARK}"
CLOSING_QUOTATION_MARK = "\N{RIGHT DOUBLE QUOTATION MARK}"
LEFTOVER_CHARACTERS = frozenset(string.punctuation + string.whitespace)

INLINE_QUOTATION = "q"
CODE_ELEMENT = "code"
# Preformatted text that holds an image is left alone, so that the image stays a figure.
BARE_PREFORMATTED = "//pre[not(.//code) and not(.//img)]"
CODE_IN_TEXT = "//code[not(ancestor::pre) and not(.//pre)]"
# The characters HTML collapses; a no-break space is not one of them.
HTML_SPACE = re.compile(r"[ \t\n\f\r]+")


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

EXSLT_NAMESPACES = {"re": "http://exslt.org/regular-expressions"}
LINK = "a"
PICTURE = "img"
# The ways the HTML itself hides an element from the person reading it.
IS_HIDDEN = etree.XPath(
    "boolean(self::*[@hidden"
    " or re:test(@style, 'display\\s*:\\s*none|visibility\\s*:\\s*hidden', 'i')])",
    namespaces=EXSLT_NAMESPACES,
)
DIALOGS = etree.XPath("//dialog | //*[@role = 'dialog' or @role = 'alertdialog']")
MAIN_PARTS = etree.XPath("descendant-or-self::*[self::article or self::main or @role = 'main']")
IN_CODE_FOR_THE_BROWSER = (
    "ancestor::script or ancestor::style or ancestor::noscript or ancestor::template"
)
SHOWN_TEXT = etree.XPath(f".//text()[not({IN_CODE_FOR_THE_BROWSER})]")
TEXT_OUTSIDE_LINKS = etree.XPath(f".//text()[not(ancestor::{LINK} or {IN_CODE_FOR_THE_BROWSER})]")
# A shown dialog that holds this share of the page's text outside links or more, counted in
# characters, is taken for the page's text.
PAGE_TEXT_SHARE = 0.5
# Class names that frameworks give text meant for screen readers only, as whole names with an
# optional prefix (govuk-visually-hidden). A name for some screen widths only (md:sr-only,
# u-visually-hidden@mobile) does not match, and not-sr-only shows the text again.
SCREEN_READER_CLASS = re.compile(
    r"(?:[a-z0-9]+-)*(?:sr-only|visually-?hidden|screen-reader-(?:text|only))", re.IGNORECASE
)
SHOWN_AGAIN = "not-sr-only"
CLASSED_ELEMENTS = etree.XPath("//*[@class]")
IS_HEADING = " or ".join(f"self::{tag}" for tag in HEADING_LEVELS)
# The links a page sets in a heading or right beside it, among them the mark that links to
# the heading's own place.
HEADING_LINKS = etree.XPath(
    f"//{LINK}[ancestor::*[{IS_HEADING}] or preceding-sibling::*[1][{IS_HEADING}]"
    f" or following-sibling::*[1][{IS_HEADING}]]"
)
REFERENCE_ADDRESS = "href"
# The tags trafilatura turns into styles; its public interface does not name them, and the
# rule below must cover the same tags as the fault it works around.
LINK_AND_STYLE_ELEMENTS = (LINK, CODE_ELEMENT, *REND_TAG_MAPPING)
CELL_LINKS_WITH_PICTURES = etree.XPath(f"//*[self::td or self::th]//{LINK}[.//{PICTURE}]")


def holds_words(text: str) -> bool:
    return any(character.isalnum() for character in text)


def has_words(element: html.HtmlElement) -> bool:
    return holds_words("".join(SHOWN_TEXT(element)))


def text_size(element: html.HtmlElement) -> int:
    return sum(len(WHITESPACE.sub("", text)) for text in TEXT_OUTSIDE_LINKS(element))


def drop_dialogs(page: html.HtmlElement) -> None:
    # A dialog is not the page's text, open or closed, unless it holds the page's main part,
    # or is shown and holds the page's text. Other hidden parts are left to the extractor,
    # which reads what carries the hidden attribute, and drops what a style hides unless that
    # would leave a seventh or less of the text of the part it takes for the page's own: a
    # page may hide its text until a script shows it.
    page_size = text_size(page)
    for dialog in DIALOGS(page):
        holds_text = text_size(dialog) >= PAGE_TEXT_SHARE * page_size
        kept = MAIN_PARTS(dialog) or (holds_text and not IS_HIDDEN(dialog))
        # A dialog inside one already dropped is out of the page, and the root stays.
        in_page = dialog.getparent() is not None and dialog.getroottree().getroot() is page
        if in_page and not kept:
            page_size -= text_size(dialog)
            dialog.drop_tree()


def drop_screen_reader_text(page: html.HtmlElement) -> None:
    # Text meant for screen readers only is not what the page shows, unless it is the only
    # words of its parent: then it names what the page shows as an icon or a sign, such as a
    # tick or a dash for "not available", and infonium, like a screen reader, sees no icon.
    for element in CLASSED_ELEMENTS(page):
        names = element.get("class", "").split()
        for_screen_readers = any(SCREEN_READER_CLASS.fullmatch(name) for name in names)
        shown_again = any(name.endswith(SHOWN_AGAIN) for name in names)
        parent = element.getparent()
        if for_screen_readers and not shown_again and parent is not None:
            texts_beside = chain((parent.text or "",), (child.tail or "" for child in parent))
            words_beside = any(holds_words(text) for text in texts_beside) or any(
                has_words(child) for child in parent if child is not element
            )
            if words_beside:
                element.drop_tree()


def drop_heading_marks(page: html.HtmlElement, url: str) -> None:
    # The mark that links to a heading's own place, such as # or ¶, is not text; an empty one
    # beside a heading also makes the extractor take the heading for a block of links and drop
    # it. A mark leads to a place in this page that is the heading, holds it, or is named
    # nowhere in the page, and holds no letter, digit or picture; a footnote mark leads
    # elsewhere and is kept.
    for link in HEADING_LINKS(page):
        address, place_name = urldefrag(urljoin(url, link.get(REFERENCE_ADDRESS, "")))
        within_page = bool(place_name) and address == urldefrag(url).url
        place = page.get_element_by_id(place_name, None) if within_page else None
        around = {link, *link.iterancestors(), link.getprevious(), link.getnext()}
        own_place = within_page and (place is None or place in around)
        bare = not has_words(link) and link.find(f".//{PICTURE}") is None
        if own_place and bare:
            link.drop_tree()


def drop_unread_parts(page: html.HtmlElement, url: str) -> None:
    drop_dialogs(page)
    drop_screen_reader_text(page)
    drop_heading_marks(page, url)
    for link in CELL_LINKS_WITH_PICTURES(page):
        # A picture in a link that has words of its own repeats them, as a logo beside a name
        # does, and its description would be read into the cell beside the words.
        if has_words(link):
            for picture in list(link.iter(PICTURE)):
                picture.drop_tree()


def keep_spaces_after_bare_elements(page: html.HtmlElement) -> None:
    # The extractor drops the spaces after a link or a style whose words all sit in an element
    # inside it, such as a link whose words are underlined, so they are moved to the start of
    # the next one.
    for element in page.iter(LINK_AND_STYLE_ELEMENTS):
        following = element.getnext()
        if (
            not element.text
            and element.tail is not None
            and element.tail.isspace()
            and following is not None
            and following.tag in LINK_AND_STYLE_ELEMENTS
        ):
            following.text = element.tail + (following.text or "")
            element.tail = None


def prepared_page(content: bytes, url: str) -> html.HtmlElement:
    # Parts that are not the page's text are dropped, and the page is rewritten where the
    # extractor would otherwise lose or mix up text. It turns a short inline quotation and a
    # block quotation into the same element, and so with preformatted text and code, and with
    # code in a sentence and code on lines of its own, so that each pair is kept apart: an
    # inline quotation becomes its words inside quotation marks, preformatted text is wrapped
    # as code, and code in a sentence loses its line breaks.
    page = trafilatura.load_html(content)
    if page is None:
        raise UnreadableSource(f"no readable text at {url}")
    drop_unread_parts(page, url)
    keep_spaces_after_bare_elements(page)
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
            # The extractor reads an element with empty text differently from one with none,
            # and drops the first together with the words after it, so text that is absent
            # is left absent.
            if piece.text is not None:
                piece.text = HTML_SPACE.sub(" ", piece.text)
            if piece is not code and piece.tail is not None:
                piece.tail = HTML_SPACE.sub(" ", piece.tail)
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
