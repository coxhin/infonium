import re
from itertools import chain
from urllib.parse import urldefrag, urljoin

import trafilatura
from lxml import etree, html
from trafilatura.htmlprocessing import REND_TAG_MAPPING

from infonium.document import NO_TEXT, UnreadableSource

WHITESPACE = re.compile(r"\s+")
OPENING_QUOTATION_MARK = "\N{LEFT DOUBLE QUOTATION MARK}"
CLOSING_QUOTATION_MARK = "\N{RIGHT DOUBLE QUOTATION MARK}"

INLINE_QUOTATION = "q"
CODE_ELEMENT = "code"
# Preformatted text that holds an image is left alone, so that the image stays a figure.
BARE_PREFORMATTED = "//pre[not(.//code) and not(.//img)]"
CODE_IN_TEXT = "//code[not(ancestor::pre) and not(.//pre)]"
# The characters HTML collapses; a no-break space is not one of them.
HTML_SPACE = re.compile(r"[ \t\n\f\r]+")
HEADING_LEVELS = {f"h{level}": level for level in range(1, 7)}

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
        raise UnreadableSource(NO_TEXT.format(source=url))
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
