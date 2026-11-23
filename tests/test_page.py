import re
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from html import escape, unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
from threading import Thread
from typing import ClassVar
from urllib.parse import urljoin, urlsplit

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from lxml import html
from playwright.sync_api import Browser, sync_playwright
from playwright.sync_api import Error as PlaywrightError
from pydantic import BaseModel

from infonium.anchor import comparable
from infonium.document import BLOCKS, Block, Figure, Heading, Table
from infonium.htmlpage import IS_HIDDEN_BY_STYLE
from infonium.library import LOCAL_OWNER
from tests import service
from tests.pdf_engine_standin import PDF_DATA
from tests.service import FILLER, LEDGER, REVISED, SITE, App, stand

# The page of a web article as infonium serves it beside its document: the page made inert,
# in its own layout, with the elements that hold each block's text marked. The pages are made
# up here, read through the service, and the served page is read with a parser.
SERVED_HOST = "testserver"
PAGE_PATH = "/api/items/{item_id}/page"
BLOCKS_PATH = "/api/items/{item_id}/page/blocks"
QUICK_POLL_SECONDS = 0.005
# The examples a property runs: each is a page read through the service, about a tenth of a
# second.
EXAMPLES = settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
VOCABULARY = (
    "river", "sleeper", "ledger", "harbour", "window", "morning", "letter", "valley", "station",
    "carriage", "timetable", "platform", "border", "evening", "journey", "summer", "café",
    "Ångström", "naïve", "R&D", "a<b", "日本語の", "árbol", "“quoted”", "well—known",
)  # fmt: skip
# Words that no block of a made-up page holds, for the parts a test looks for by their words.
FORGED_WORDS = ("quorvex", "zindlap", "mertusk", "plovane", "brastic", "olquin")
# What holds code for the browser and what the page shows struck out: neither is a block's
# text, as the extractor reads it.
UNREAD = frozenset({"script", "style", "noscript", "template", "del", "s", "strike"})
LINE_BREAK_TAG = "br"
LINE_BREAK = "\n"
INLINE_TAGS = frozenset(
    {
        "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "dfn", "em", "font", "i", "kbd",
        "mark", "q", "s", "samp", "small", "span", "strong", "sub", "sup", "time", "u", "var",
        "infonium-run",
    }
)  # fmt: skip
HEADING_TAGS = frozenset({f"h{level}" for level in range(1, 7)})
# What a reader would lose if the content of a placed element were replaced: pictures,
# controls, formulas, image maps and stylesheets.
KEPT_APART = frozenset(
    {
        "img", "picture", "svg", "video", "audio", "canvas", "iframe", "object", "embed",
        "input", "button", "select", "textarea", "math", "map", "area", "style", "link",
    }
)  # fmt: skip
SCRIPTING_TAGS = frozenset({"script"})
FRAME_TAGS = frozenset({"iframe", "frame", "frameset", "object", "embed", "applet", "portal"})
FRAME_ADDRESSES = ("src", "srcdoc", "data", "code", "archive")
SVG_ANIMATIONS = frozenset(
    {"animate", "set", "animatemotion", "animatetransform", "animatecolor", "discard"}
)
CANDIDATE_LISTS = frozenset({"srcset", "imagesrcset"})
SCRIPT_SCHEMES = "javascript:"
# What a browser drops from an address before it reads its scheme.
ADDRESS_NOISE = re.compile(r"[\t\n\r]")
ADDRESS_EDGE = "".join(map(chr, range(0x21)))
INFONIUM_PREFIX = "infonium-"
INFONIUM_DATA_PREFIX = "data-infonium-"
DIALOG = "dialog"
# What navigates when a reader activates it, by the attributes that give its address, and
# the one target that opens a browsing context of its own.
NAVIGATING_TAGS = ("a", "area")
LINK_ADDRESSES = ("href", "xlink:href")
NEW_CONTEXT = "_blank"
DOCTYPE = re.compile(r"^\s*(<!doctype[^>]*>)", re.IGNORECASE)
# A style or a stylesheet link of a made-up page is labelled with where the page has it: in
# its head, at the end of its body, or among the words of a block.
STYLE_PLACE = re.compile(r"\bstyle[ -](head|body|inline)\b")
STYLE_LABEL = re.compile(r"style-\d+-(?:head|body|inline)")


class ServedPage(BaseModel):
    # What the service answers for an item's page: the page itself, and where its blocks
    # stand in it.
    item_id: str
    markup: str
    page_attribute: str
    block_attribute: str
    cell_attribute: str
    placed: list[int]
    blocks: list[Block]

    def root(self) -> html.HtmlElement:
        return html.document_fromstring(self.markup)

    @property
    def address(self) -> str:
        return f"http://{SERVED_HOST}{PAGE_PATH.format(item_id=self.item_id)}"


def served(app: App, item_id: str) -> ServedPage:
    page = app.client.get(PAGE_PATH.format(item_id=item_id))
    assert page.status_code == 200, f"the page of a web page item was not served: {page.text}"
    where = app.client.get(BLOCKS_PATH.format(item_id=item_id))
    assert where.status_code == 200, f"the blocks of a served page were not given: {where.text}"
    document = app.client.get(f"/api/items/{item_id}/document").json()
    return ServedPage(
        item_id=item_id,
        markup=page.text,
        blocks=BLOCKS.validate_python(document["blocks"]),
        **where.json(),
    )


def same_text(text: str) -> str:
    # Text as citations are matched to blocks: by what it says, not by how it is typeset.
    return comparable(text, set()).text.strip()


def shown_text(element: html.HtmlElement, parted_at: tuple[str, ...] | None = None) -> str:
    # The words of an element as the extractor reads them: no code for the browser, nothing
    # struck out or hidden by a style, and a line break for a br element. Parted at marks, the
    # words of elements laid out as blocks, and of elements that carry one of those marks, are
    # kept apart, as the words of two blocks are: a marked element's content goes as a whole.
    pieces: list[str] = []

    def walk(node: html.HtmlElement) -> None:
        if node.tag == LINE_BREAK_TAG:
            pieces.append(LINE_BREAK)
        elif isinstance(node.tag, str) and node.tag not in UNREAD and not IS_HIDDEN_BY_STYLE(node):
            parted = parted_at is not None and (
                node.tag not in INLINE_TAGS or any(node.get(mark) is not None for mark in parted_at)
            )
            gap = LINE_BREAK if parted else ""
            pieces.append(gap + (node.text or ""))
            for child in node:
                walk(child)
                pieces.append(child.tail or "")
            pieces.append(gap)

    walk(element)
    return "".join(pieces)


def block_marks(page: ServedPage, root: html.HtmlElement) -> list[html.HtmlElement]:
    return [element for element in root.iter() if element.get(page.block_attribute) is not None]


def cell_marks(page: ServedPage, root: html.HtmlElement) -> list[html.HtmlElement]:
    return [element for element in root.iter() if element.get(page.cell_attribute) is not None]


def cell_place(element: html.HtmlElement, page: ServedPage) -> tuple[int, int, int]:
    block, row, cell = (int(number) for number in element.get(page.cell_attribute, "").split(","))
    return block, row, cell


def table_cells(table: Table) -> list[list[str]]:
    return [row.split(table.cell_separator) for row in table.text.split(table.row_separator)]


def table_has_all_cells(page: ServedPage, number: int, cells: set[tuple[int, int, int]]) -> bool:
    table = page.blocks[number]
    assert isinstance(table, Table)
    return all(
        (number, row, cell) in cells
        for row, texts in enumerate(table_cells(table))
        for cell, text in enumerate(texts)
        if same_text(text)
    )


# Made-up pages, with what real pages hold: inline elements nested in each other, line
# breaks, lists in lists, tables with spans, texts that stand more than once, hidden and
# struck-out words, and scripts, handlers and ways to load other pages in every place.
INLINE_KINDS = (
    "style", "link", "break", "struck", "hidden", "script", "noscript", "entity", "sheet",
    "danger", "forged", "aimed",
)  # fmt: skip
PART_KINDS = (
    "heading", "paragraph", "paragraph", "list", "table", "figure", "loose", "parted", "quote",
    "code", "noscript", "danger", "again", "forged", "map", "drawing", "template", "sent",
    "named frame",
)  # fmt: skip
STYLE_TAGS = ("b", "i", "em", "strong", "u", "span", "small")
STRUCK_TAGS = ("del", "s", "strike")
ENTITIES = ("&nbsp;", "&shy;", " &amp; ", "&#8203;")
INLINE_SHEETS = (
    "<style>/* style inline */</style>",
    "<link rel='stylesheet' href='style-inline.css'>",
)
CELL_CONTENTS = ("{words}", "<b>{words}</b>", "<a href='/cell'>{words}</a>", "")
CELL_SPANS = ("", "", " colspan='2'", " rowspan='2'")
NOSCRIPT_PARTS = (
    "<noscript><img src='https://sink.test/pixel' width='1' height='1'></noscript>",
    "<noscript><p>Turn on scripts to see the map.</p></noscript>",
)
# What a page writes in infonium's own names: marks on elements that hold no block's text
# alone, an element named as infonium names the runs it marks, and attributes it never sets.
FORGED_PARTS = (
    f"<div class='forged' data-infonium-block='1'><p>{FORGED_WORDS[1]}</p>"
    f"<p>{FORGED_WORDS[2]}</p></div>",
    f"<p>{FORGED_WORDS[3]} <infonium-run class='forged' data-infonium-block='2'>"
    f"{FORGED_WORDS[4]}</infonium-run> {FORGED_WORDS[5]}</p>",
    "<img class='forged' src='https://pictures.test/f.jpg' data-infonium-block='3'"
    " data-infonium-cell='0,0,0'>",
    f"<p data-infonium-elsewhere='x' data-INFONIUM-cell='9,9,9'>{FORGED_WORDS[0]} words</p>",
    f"<p data-infonium-page='{FORGED_WORDS[1]}'>{FORGED_WORDS[1]} words</p>",
    "<span data-INFONIUM-PAGE=''></span>",
)
# Where a page names the item it would be the page of, on its root and its body.
FORGED_ROOTS = ("", " data-infonium-page='forged'", " DATA-INFONIUM-PAGE=''", " data-infonium-page")
# The frame a page names for its links, which it also holds.
FRAME_NAME = "beside"
FORGED_RUN = f" <span class='forged' data-infonium-block='0'>{FORGED_WORDS[0]}</span> "

words = st.lists(st.sampled_from(VOCABULARY), min_size=1, max_size=7).map(
    lambda chosen: escape(" ".join(chosen), quote=False)
)
handlers = st.sampled_from(
    [
        "",
        " onclick=\"fetch('https://sink.test/click')\"",
        " ONMOUSEOVER='x()'",
        " onfocus=\"location='https://sink.test/focus'\" autofocus",
        " onerror=x() ",
        " onanimationstart='x()'",
    ]
)
script_addresses = st.sampled_from(
    [
        "javascript:x()",
        "JavaScript:x()",
        " javascript:x()",
        "java&#x09;script:x()",
        "&#106;avascript:x()",
        "java\nscript:x()",
    ]
)
# The targets a page gives what navigates, every one of them the frame itself or one beside
# it but the last; and the bases it gives, some naming a target.
targets = st.sampled_from(
    [
        "",
        " target='_self'",
        " TARGET='_TOP'",
        " target='_parent'",
        f" target='{FRAME_NAME}'",
        " target=' _self '",
        " target=''",
        " target='_blank'",
    ]
)
own_bases = st.sampled_from(
    [
        "",
        "<base target='_self'>",
        "<base href='https://pages.test/' target='_top'>",
        f"<base target='{FRAME_NAME}'><base target='_blank'>",
        "<base href='https://pages.test/'>",
    ]
)
link_addresses = st.one_of(
    st.sampled_from(["https://line.test/one", "timetable/two", "/three", "../four", "?five"]),
    script_addresses,
)
dangers = st.one_of(
    st.sampled_from(
        [
            "<script>fetch('https://sink.test/script')</script>",
            "<script src='https://sink.test/script.js'></script>",
            "<SCRIPT type='module'>x()</SCRIPT>",
            "<meta http-equiv='refresh' content='0; url=https://sink.test/refresh'>",
            "<meta http-equiv=' Refresh ' content='5'>",
            "<iframe src='https://sink.test/frame'></iframe>",
            "<iframe srcdoc='&lt;script&gt;x()&lt;/script&gt;'></iframe>",
            "<object data='https://sink.test/object'></object>",
            "<embed src='https://sink.test/embed'>",
            "<form action='https://sink.test/form'><input name='q'><button>Go</button></form>",
            "<form><button formaction='https://sink.test/b' formmethod='post'>Go</button></form>",
            "<form method='post'><input type='submit'></form>",
            "<svg><a href='javascript:x()'><text>t</text></a></svg>",
            "<svg><animate attributeName='href' to='javascript:x()'/></svg>",
            "<svg><set attributeName='onclick' to='x()'/></svg>",
            "<svg><animateMotion dur='1s'/><animateTransform attributeName='x'/></svg>",
            "<img src='x.png' onerror=\"fetch('https://sink.test/img')\">",
            "<a href='https://line.test/' ping='https://sink.test/ping'>ping</a>",
            "<link rel='stylesheet' href='javascript:x()'>",
            "<base href='javascript:x()'>",
            "<template><script>x()</script></template>",
        ]
    ),
    script_addresses.map(lambda address: f"<a href='{address}'>go</a>"),
    script_addresses.map(lambda address: f"<iframe src='{address}'></iframe>"),
)
# A form inside a sentence would close the paragraph around it.
inline_dangers = dangers.filter(lambda danger: "<form" not in danger)


@st.composite
def inline(draw: st.DrawFn, depth: int = 0) -> str:
    # A run of words with inline elements in it, nested to two levels.
    pieces = [draw(words)]
    for _ in range(draw(st.integers(0, 4))):
        inner = draw(inline(depth + 1)) if depth < 2 else draw(words)
        handler = draw(handlers)
        match draw(st.sampled_from(INLINE_KINDS)):
            case "style":
                tag = draw(st.sampled_from(STYLE_TAGS))
                pieces.append(f" <{tag}{handler}>{inner}</{tag}> ")
            case "link":
                pieces.append(f" <a href='{draw(link_addresses)}'{handler}>{inner}</a> ")
            case "break":
                pieces.append("<br>")
            case "struck":
                tag = draw(st.sampled_from(STRUCK_TAGS))
                pieces.append(f" <{tag}>{draw(words)}</{tag}> ")
            case "hidden":
                pieces.append(f"<span style='display:none'>{draw(words)}</span>")
            case "script":
                pieces.append("<script>document.write('<p>written</p>')</script>")
            case "noscript":
                pieces.append("<noscript>Turn on scripts to see the map.</noscript>")
            case "entity":
                pieces.append(draw(st.sampled_from(ENTITIES)))
            case "sheet":
                pieces.append(draw(st.sampled_from(INLINE_SHEETS)))
            case "danger":
                pieces.append(draw(inline_dangers))
            case "forged":
                pieces.append(FORGED_RUN)
            case "aimed":
                address = draw(link_addresses)
                pieces.append(f" <a href='{address}'{draw(targets)}>{inner}</a> ")
        pieces.append(draw(words))
    return "".join(pieces)


def list_markup(draw: st.DrawFn, depth: int) -> str:
    tag = draw(st.sampled_from(["ul", "ol"]))
    items = []
    for _ in range(draw(st.integers(1, 4))):
        nested = list_markup(draw, depth + 1) if depth < 1 and draw(st.booleans()) else ""
        items.append(f"<li{draw(handlers)}>{draw(inline())}{nested}</li>")
    return f"<{tag}>{''.join(items)}</{tag}>"


def table_markup(draw: st.DrawFn) -> str:
    rows = []
    for _ in range(draw(st.integers(1, 4))):
        cells = []
        for _ in range(draw(st.integers(1, 4))):
            tag = draw(st.sampled_from(["td", "td", "th"]))
            span = draw(st.sampled_from(CELL_SPANS))
            content = draw(st.sampled_from(CELL_CONTENTS)).format(words=draw(words))
            cells.append(f"<{tag}{span}{draw(handlers)}>{content}</{tag}>")
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table>{''.join(rows)}</table>"


@st.composite
def page_parts(draw: st.DrawFn) -> list[str]:
    # The parts of an article, some of them standing twice.
    parts: list[str] = []
    for _ in range(draw(st.integers(2, 8))):
        handler = draw(handlers)
        match draw(st.sampled_from(PART_KINDS)):
            case "heading":
                level = draw(st.integers(1, 6))
                parts.append(f"<h{level}{handler}>{draw(inline())}</h{level}>")
            case "list":
                parts.append(list_markup(draw, 0))
            case "table":
                parts.append(table_markup(draw))
            case "figure":
                number = draw(st.integers(0, 99))
                parts.append(
                    f"<figure><img src='https://pictures.test/{number}.jpg' alt='{draw(words)}'>"
                    f"<figcaption>{draw(inline())}</figcaption></figure>"
                )
            case "loose":
                parts.append(
                    f"<div{handler}>{draw(inline())}<p>{draw(inline())}</p>{draw(words)}</div>"
                )
            case "parted":
                parts.append(f"<div>{draw(inline())}<br><br>{draw(inline())}</div>")
            case "quote":
                parts.append(f"<blockquote><p>{draw(inline())}</p></blockquote>")
            case "code":
                parts.append(f"<pre><code>{draw(words)}\n  {draw(words)}</code></pre>")
            case "noscript":
                parts.append(draw(st.sampled_from(NOSCRIPT_PARTS)))
            case "danger":
                parts.append(draw(dangers))
            case "again" if parts:
                parts.append(draw(st.sampled_from(parts)))
            case "forged":
                parts.append(draw(st.sampled_from(FORGED_PARTS)))
            case "map":
                parts.append(
                    "<img src='https://pictures.test/map.png' usemap='#stops' alt=''>"
                    f"<map name='stops'><area href='{draw(link_addresses)}'{draw(targets)}"
                    " shape='rect' coords='0,0,9,9' alt='stop'><area shape='default'></map>"
                )
            case "drawing":
                parts.append(
                    f"<svg><a xlink:href='{draw(link_addresses)}'{draw(targets)}><text>"
                    f"{draw(words)}</text></a><a href='{draw(link_addresses)}'{draw(targets)}>"
                    "<rect width='9' height='9'/></a></svg>"
                )
            case "template":
                parts.append(
                    f"<template><p><a href='{draw(link_addresses)}'{draw(targets)}>"
                    f"{draw(words)}</a></p><form{draw(targets)}></form></template>"
                )
            case "sent":
                parts.append(
                    f"<form action='/send'{draw(targets)}><input name='q'>"
                    f"<button formtarget='_self'>Go</button>"
                    f"<input type='submit' formtarget='{FRAME_NAME}'></form>"
                )
            case "named frame":
                parts.append(f"<iframe name='{FRAME_NAME}'></iframe>")
            case _:
                parts.append(f"<p{handler}>{draw(inline())}</p>")
    return parts


@st.composite
def made_up_pages(draw: st.DrawFn) -> str:
    head_extras = "".join(draw(st.lists(dangers, min_size=1, max_size=3)))
    nav = draw(st.sampled_from(["", f"<nav><p>{draw(words)}</p></nav>"]))
    parts = "".join(draw(page_parts()))
    root, body = draw(st.sampled_from(FORGED_ROOTS)), draw(st.sampled_from(FORGED_ROOTS))
    page = (
        f"<!DOCTYPE html><html{root}><head>{draw(own_bases)}<meta charset='utf-8'>"
        "<title>Trains</title>"
        "<style>/* style head */ body { margin: 0 }</style>"
        "<link rel='stylesheet' href='style-head.css'>"
        f"<script>fetch('https://sink.test/head')</script>{head_extras}</head>"
        f"<body{body}{draw(handlers)}>{nav}<article>{parts}{FILLER}</article>"
        "<style>/* style body */</style></body></html>"
    )
    numbers = count()
    return STYLE_PLACE.sub(lambda place: f"style-{next(numbers)}-{place.group(1)}", page)


names = (f"page{number}" for number in count())


def article_page(body: str, head: str = "", doctype: str = "<!DOCTYPE html>") -> str:
    # A page whose article holds these parts, with words enough to be read as an article.
    return (
        f"{doctype}<html><head>{head}<title>Made up</title></head>"
        f"<body><article>{body}{FILLER}</article></body></html>"
    )


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    # A made-up page is read in a few hundredths of a second; the reading is looked at as
    # often, so that a property's examples do not wait on the polls.
    monkeypatch.setattr(service, "POLL_SECONDS", QUICK_POLL_SECONDS)


def read_made_up(app: App, page: str) -> ServedPage:
    return served(app, app.read_page(next(names), page.encode()))


def page_marks(page: ServedPage, root: html.HtmlElement) -> list[html.HtmlElement]:
    # The marked elements and cells of a page. A page that the extractor reads as one run
    # across its parts has no element to mark; were no page marked, Hypothesis would find the
    # property it is asked for unsatisfiable.
    marks = block_marks(page, root) + cell_marks(page, root)
    assume(marks)
    return marks


def holder_of(element: html.HtmlElement, page: ServedPage) -> html.HtmlElement | None:
    # The marked element that holds this one, or is it.
    for candidate in (element, *element.iterancestors()):
        if (
            candidate.get(page.block_attribute) is not None
            or candidate.get(page.cell_attribute) is not None
        ):
            return candidate
    return None


@EXAMPLES
@given(page=made_up_pages())
def test_every_marked_element_holds_exactly_its_blocks_text(app: App, page: str) -> None:
    served_page = read_made_up(app, page)
    page_marks(served_page, served_page.root())
    for element in block_marks(served_page, served_page.root()):
        number = int(element.get(served_page.block_attribute, ""))
        assert 0 <= number < len(served_page.blocks), f"an element is marked {number}, no block"
        block = served_page.blocks[number]
        assert not isinstance(block, Table | Figure), (
            f"a {block.kind} block {number} is marked as a whole on a {element.tag!r} element"
        )
        assert same_text(shown_text(element)) == same_text(block.text), (
            f"the element marked with block {number} holds other text than the block: "
            f"{shown_text(element)!r} in place of {block.text!r}"
        )


@EXAMPLES
@given(page=made_up_pages())
def test_a_block_is_on_one_element_at_most_and_no_mark_is_inside_another(
    app: App, page: str
) -> None:
    served_page = read_made_up(app, page)
    root = served_page.root()
    marks = page_marks(served_page, root)
    numbers = Counter(element.get(served_page.block_attribute) for element in marks)
    places = Counter(element.get(served_page.cell_attribute) for element in marks)
    twice = [number for number, times in numbers.items() if number is not None and times > 1]
    assert not twice, f"the blocks {twice} are each marked on more than one element"
    twice = [place for place, times in places.items() if place is not None and times > 1]
    assert not twice, f"the table cells {twice} are each marked on more than one element"
    for element in marks:
        outer = element.getparent()
        holder = None if outer is None else holder_of(outer, served_page)
        assert holder is None, (
            f"a marked {element.tag!r} element stands inside the marked {holder.tag!r} element"
        )


@EXAMPLES
@given(page=made_up_pages())
def test_every_marked_cell_holds_its_cell_of_the_tables_text(app: App, page: str) -> None:
    served_page = read_made_up(app, page)
    for element in cell_marks(served_page, served_page.root()):
        number, row, cell = cell_place(element, served_page)
        table = served_page.blocks[number]
        assert isinstance(table, Table), f"a cell is marked with block {number}, a {table.kind}"
        cells = table_cells(table)
        assert row < len(cells) and cell < len(cells[row]), (
            f"a cell is marked {number},{row},{cell}, which the table's text does not have"
        )
        assert element.tag in ("td", "th", "caption"), f"a {element.tag!r} is marked as a cell"
        assert same_text(shown_text(element)) == same_text(cells[row][cell]), (
            f"the cell marked {number},{row},{cell} holds other text than that cell of the "
            f"table: {shown_text(element)!r} in place of {cells[row][cell]!r}"
        )


@EXAMPLES
@given(page=made_up_pages())
def test_placed_lists_exactly_the_blocks_that_have_their_elements(app: App, page: str) -> None:
    served_page = read_made_up(app, page)
    root = served_page.root()
    whole = {
        int(element.get(served_page.block_attribute, ""))
        for element in block_marks(served_page, root)
    }
    cells = {cell_place(element, served_page) for element in cell_marks(served_page, root)}
    tables = {
        number
        for number, block in enumerate(served_page.blocks)
        if isinstance(block, Table) and table_has_all_cells(served_page, number, cells)
    }
    placed = set(served_page.placed)
    assert len(served_page.placed) == len(placed), "placed names a block twice"
    assert placed == whole | tables, (
        f"placed lists {sorted(placed - whole - tables)} without their elements"
        f" and leaves out {sorted((whole | tables) - placed)} that have them"
    )
    figures = [n for n in placed if isinstance(served_page.blocks[n], Figure)]
    assert not figures, f"the figures {figures} are placed"


def body_of(root: html.HtmlElement) -> html.HtmlElement:
    body = root.body
    assert body is not None, "the served page has no body"
    return body


def body_text(page: ServedPage, root: html.HtmlElement) -> str:
    marks = (page.block_attribute, page.cell_attribute)
    return same_text(shown_text(body_of(root), parted_at=marks))


def pictures(root: html.HtmlElement) -> set[str]:
    return {picture.get("src", "") for picture in body_of(root).iter("img")}


@EXAMPLES
@given(page=made_up_pages())
def test_replacing_every_marked_element_loses_nothing_of_another_block(app: App, page: str) -> None:
    served_page = read_made_up(app, page)
    root = served_page.root()
    before, pictures_before = body_text(served_page, root), pictures(root)
    marks = page_marks(served_page, root)
    for element in marks:
        for child in list(element):
            element.remove(child)
        element.text = "#"
    after, pictures_after = body_text(served_page, root), pictures(root)
    others = [
        (number, block)
        for number, block in enumerate(served_page.blocks)
        if number not in served_page.placed
    ]
    for number, block in others:
        if isinstance(block, Figure):
            assert block.source not in pictures_before or block.source in pictures_after, (
                f"the picture of figure {number} went with the content of a marked element"
            )
            continue
        for part in re.split(r"[\t\n]", block.text):
            words = same_text(part)
            assert words not in before or words in after, (
                f"the words {part!r} of block {number}, which has no element of its own, went "
                "with the content of a marked element"
            )


@EXAMPLES
@given(page=made_up_pages())
def test_a_marked_element_holds_nothing_a_reader_would_lose(app: App, page: str) -> None:
    served_page = read_made_up(app, page)
    root = served_page.root()
    marks = page_marks(served_page, root)
    for element in marks:
        kept_apart = [inner.tag for inner in element.iter() if inner.tag in KEPT_APART]
        assert not kept_apart, (
            f"the marked {element.tag!r} element holds {kept_apart}, which a translation put in "
            "its place would take away"
        )
        noscripts = [
            same_text(inner.text_content())
            for inner in element.iter("noscript")
            if same_text(inner.text_content())
        ]
        assert not noscripts, f"the marked {element.tag!r} element holds words for no script"


def address_of(value: str) -> str:
    # An address as a browser reads its scheme.
    return ADDRESS_NOISE.sub("", value).strip(ADDRESS_EDGE).lower()


class Tokens(HTMLParser):
    # The start tags of a page as a tokenizer reads them, before a tree is built: a second
    # view of the page, in which an element that a tree builder would move or drop still
    # shows.
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, attrs))


def tag_views(page_html: str) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    # Every element of the page with its attributes, as the tree and as the tokens have it.
    for element in html.document_fromstring(page_html).iter():
        if isinstance(element.tag, str):
            yield element.tag.lower(), [(name.lower(), value) for name, value in element.items()]
    tokens = Tokens()
    tokens.feed(page_html)
    tokens.close()
    for tag, attributes in tokens.tags:
        yield tag, [(name, value or "") for name, value in attributes]


def what_could_run(page_html: str) -> list[str]:
    # The parts of a page that run a script, load another page in its place or in a frame,
    # send a form, or animate an address or a handler.
    found = []
    for tag, attributes in tag_views(page_html):
        named = dict(attributes)
        if tag in SCRIPTING_TAGS:
            found.append(f"a {tag} element")
        if tag in SVG_ANIMATIONS:
            found.append(f"an SVG {tag} element")
        if tag in FRAME_TAGS and any(named.get(name, "").strip() for name in FRAME_ADDRESSES):
            found.append(f"a {tag} element with a page in it: {named}")
        if tag == "meta" and named.get("http-equiv", "").strip().lower() == "refresh":
            found.append("an instruction to load another page (meta refresh)")
        if tag == "form" and named.get("method", "").strip().lower() != DIALOG:
            found.append(f"a form that can be sent: {named}")
        if named.get("formmethod", DIALOG).strip().lower() != DIALOG:
            found.append(f"a {tag} that sends its form: {named}")
        for name, value in attributes:
            if name.startswith("on"):
                found.append(f"the handler {name} on a {tag} element")
            candidates = value.split(",") if name in CANDIDATE_LISTS else [value]
            if any(address_of(candidate).startswith(SCRIPT_SCHEMES) for candidate in candidates):
                found.append(f"a script address in {name} of a {tag} element: {value!r}")
    return found


@EXAMPLES
@given(page=made_up_pages())
def test_nothing_in_the_served_page_can_run_or_load_another_page(app: App, page: str) -> None:
    assert what_could_run(page), "a made-up page holds nothing that could run"
    served_page = read_made_up(app, page)
    found = what_could_run(served_page.markup)
    assert not found, f"the served page still holds what could run: {found}"


@pytest.mark.parametrize(
    "picture",
    [
        "<img src='a.png' srcset='a.png 1x, javascript:x() 2x'>",
        "<picture><source srcset='javascript:x()'><img src='a.png'></picture>",
    ],
)
def test_no_script_address_is_left_among_the_addresses_of_a_picture(app: App, picture: str) -> None:
    page = article_page(f"<p>A picture.</p>{picture}")
    served_page = served(app, app.read_page(next(names), page.encode()))
    found = what_could_run(served_page.markup)
    assert not found, f"the served page still holds what could run: {found}"


@EXAMPLES
@given(page=made_up_pages())
def test_nothing_the_page_wrote_in_infoniums_names_is_left(app: App, page: str) -> None:
    served_page = read_made_up(app, page)
    own = (served_page.block_attribute, served_page.cell_attribute)
    for tag, attributes in tag_views(served_page.markup):
        named = dict(attributes)
        # The root element names the item the page is of, in infonium's own words.
        names_item = tag == "html" and named.get(served_page.page_attribute) == served_page.item_id
        foreign = [
            name
            for name in named
            if name.startswith(INFONIUM_DATA_PREFIX)
            and name not in own
            and not (names_item and name == served_page.page_attribute)
        ]
        assert not foreign, f"the page's own attributes {foreign} in infonium's names are left"
        assert "forged" not in named.get("class", "").split() or not set(own) & set(named), (
            f"a mark the page wrote itself on a {tag} element is left: {named}"
        )
        assert not tag.startswith(INFONIUM_PREFIX) or "forged" not in named.get("class", ""), (
            f"an element the page wrote in infonium's names is left: {tag} {named}"
        )
        assert not tag.startswith(INFONIUM_PREFIX) or served_page.block_attribute in named, (
            f"an {tag} element is not a block's mark: {named}"
        )


@EXAMPLES
@given(page=made_up_pages())
def test_the_served_page_names_its_item_on_its_root_and_nowhere_else(app: App, page: str) -> None:
    served_page = read_made_up(app, page)
    root = served_page.root()
    assert root.get(served_page.page_attribute) == served_page.item_id, (
        f"the served page's root names {root.get(served_page.page_attribute)!r}, not its item"
    )
    namers = [
        (tag, dict(attributes).get(served_page.page_attribute))
        for tag, attributes in tag_views(served_page.markup)
        if served_page.page_attribute in dict(attributes)
    ]
    # The page as the tree and as the tokens have it: the root once in each.
    assert namers == [("html", served_page.item_id)] * 2, (
        f"elements other than the root, or with another value, carry {served_page.page_attribute}:"
        f" {namers}"
    )


def opens_a_new_context(target: str | None) -> bool:
    # Only this target opens a browsing context of its own whatever else the reader's window
    # holds; a name may be that of a frame beside the page.
    return target is not None and target.lower() == NEW_CONTEXT


def what_navigates_the_frame(page_html: str) -> list[str]:
    # What, activated, would load a page in the frame itself or in one beside it: a link of
    # HTML or SVG, an area of an image map or a form that names no new browsing context, a
    # control that names another target for its form, and a base that names none for the
    # rest. Template contents are elements of the page here, as the tokens have them.
    found = []
    for tag, attributes in tag_views(page_html):
        named = dict(attributes)
        navigates = tag == "form" or (
            tag in NAVIGATING_TAGS and any(name in named for name in LINK_ADDRESSES)
        )
        if navigates and not opens_a_new_context(named.get("target")):
            found.append(f"a {tag} that opens in the frame: {named}")
        if "formtarget" in named and not opens_a_new_context(named["formtarget"]):
            found.append(f"a {tag} that sends its form into the frame: {named}")
    aimed = [
        base.get("target")
        for base in html.document_fromstring(page_html).iter("base")
        if base.get("target") is not None
    ]
    if not aimed or not opens_a_new_context(aimed[0]):
        found.append(f"the page's base names the target {aimed[0] if aimed else None!r}")
    return found


@EXAMPLES
@given(page=made_up_pages())
def test_nothing_in_the_served_page_loads_a_page_in_the_frame_when_activated(
    app: App, page: str
) -> None:
    assert what_navigates_the_frame(page), "a made-up page holds nothing that opens in its frame"
    served_page = read_made_up(app, page)
    found = what_navigates_the_frame(served_page.markup)
    assert not found, f"the served page still loads a page in the frame: {found}"


def every_navigating_kind(target: str) -> str:
    # One of each thing that navigates when activated, all given the same target, under a
    # base of the page's own that names it too.
    return article_page(
        f"<p>A <a href='/link'{target}>link</a> and <a href='#place'{target}>a place</a>.</p>"
        "<img src='https://pictures.test/map.png' usemap='#stops' alt=''>"
        f"<map name='stops'><area href='/area'{target} shape='rect' coords='0,0,9,9'></map>"
        f"<svg><a xlink:href='/drawn'{target}><text>drawn</text></a>"
        f"<a href='/drawn-too'{target}><rect width='9' height='9'/></a></svg>"
        f"<template><p><a href='/kept'{target}>kept</a></p><form{target}></form></template>"
        f"<form action='/send'{target}><input name='q'><button formtarget='_self'>Go</button>"
        f"</form><iframe name='{FRAME_NAME}'></iframe>",
        head=f"<base{target}>",
    )


def test_every_kind_of_thing_that_navigates_opens_a_new_context_whatever_its_target(
    app: App,
) -> None:
    for target in ("", " target='_self'", " target='_top'", f" target='{FRAME_NAME}'"):
        page = every_navigating_kind(target)
        served_page = served(app, app.read_page(next(names), page.encode()))
        found = what_navigates_the_frame(served_page.markup)
        assert not found, f"with the target {target!r}, the served page loads in the frame: {found}"


def link_address(element: html.HtmlElement) -> str | None:
    return next((element.get(name) for name in LINK_ADDRESSES if element.get(name)), None)


def link_addresses_of(root: html.HtmlElement) -> Counter[tuple[str, str]]:
    # The addresses the page's links and areas give, as written, but for those to scripts,
    # which R2 takes away. Where an address leads under the page's base is judged with R3.
    return Counter(
        (str(element.tag), address.strip())
        for element in root.iter(*NAVIGATING_TAGS)
        for address in [link_address(element)]
        if address is not None and not address_of(address).startswith(SCRIPT_SCHEMES)
    )


@EXAMPLES
@given(page=made_up_pages())
def test_what_navigates_keeps_its_address(app: App, page: str) -> None:
    written = link_addresses_of(html.document_fromstring(page))
    assume(written)
    kept = link_addresses_of(read_made_up(app, page).root())
    assert kept == written, (
        f"links give other addresses than the page's own did: lost "
        f"{sorted((written - kept).elements())}, gained {sorted((kept - written).elements())}"
    )


def style_labels(element: html.HtmlElement) -> list[str]:
    return STYLE_LABEL.findall((element.text or "") + element.get("href", ""))


@EXAMPLES
@given(page=made_up_pages())
def test_the_pages_styles_stay_where_it_has_them(app: App, page: str) -> None:
    served_page = read_made_up(app, page)
    labels = STYLE_LABEL.findall(page)
    assert len(labels) >= 3, "a made-up page has no styles in its head and body"
    served_styles = [
        (label, element)
        for element in served_page.root().iter("style", "link")
        for label in style_labels(element)
    ]
    for label in labels:
        kept = [element for served_label, element in served_styles if served_label == label]
        assert len(kept) == 1, f"the page's style {label} stands {len(kept)} times in the page"
        parent = kept[0].getparent()
        assert parent is not None and holder_of(parent, served_page) is None, (
            f"the page's style {label} stands inside a marked element"
        )
        place = label.rsplit("-", 1)[1]
        assert place == "inline" or parent.tag == place, (
            f"the page's style {label} moved from the page's {place} to a {parent.tag!r}"
        )


def effective_address(page: ServedPage, address: str) -> str:
    # Where an address of the served page leads, opened at the page's own address: against
    # the first base element that has an address, as a browser resolves it.
    bases = [base.get("href", "") for base in page.root().iter("base") if base.get("href")]
    base = urljoin(page.address, bases[0].strip()) if bases else page.address
    return urljoin(base, address.strip())


UPLOADED_BASES = ("", "/", "sub/", "?q")
RELATIVE_ADDRESSES = ("pictures/a.png", "/root.css", "../up.html", "?q=1", "//cdn.test/x.png")
PAGE_BASES = (
    "",
    "javascript:x()",
    "data:text/html,x",
    "https://elsewhere.test/dir/",
    "http://elsewhere.test/dir/",
    "  https://spaced.test/a/  ",
    "/x/",
    "sub/",
    "//other.test/y/",
)


def addressed_page(base: str) -> str:
    base_element = f"<base href='{base}'>" if base else ""
    uses = "".join(
        f"<p><a href='{address}'>Link {number}</a> <img src='{address}' alt=''></p>"
        f"<link rel='stylesheet' href='{address}'>"
        for number, address in enumerate(RELATIVE_ADDRESSES)
    )
    return (
        f"<!DOCTYPE html><html><head>{base_element}<title>Addresses</title></head>"
        f"<body><article><h1>Addresses</h1>{uses}{FILLER}</article></body></html>"
    )


def page_addresses(page: ServedPage) -> list[tuple[str, str]]:
    # The relative addresses of the served page, each with where it leads.
    root = page.root()
    found = [
        (element.get(name, ""), effective_address(page, element.get(name, "")))
        for element in root.iter("a", "img", "link")
        for name in ("href", "src")
        if element.get(name) is not None
    ]
    return [(written, resolved) for written, resolved in found if written.strip()]


# The cases of one requirement are judged on one service each: starting one takes longer
# than reading a page.
def test_the_pages_relative_addresses_lead_to_the_site_it_came_from(app: App) -> None:
    for base in PAGE_BASES:
        relative_addresses_lead_to_the_site(app, base)


def relative_addresses_lead_to_the_site(app: App, base: str) -> None:
    name = next(names)
    page_url = f"{SITE}/page/{name}"
    served_page = served(app, app.read_page(name, addressed_page(base).encode()))
    own_base = urljoin(page_url, base.strip())
    leads = page_addresses(served_page)
    assert len(leads) >= 3 * len(RELATIVE_ADDRESSES), (
        f"with the page's base {base!r}, the served page lost the page's addresses: {leads}"
    )
    for written, resolved in leads:
        relative = next(
            (address for address in RELATIVE_ADDRESSES if written.endswith(address.lstrip("./"))),
            None,
        )
        assert relative is not None, f"an address {written!r} that the page did not have"
        if urlsplit(base.strip()).scheme in ("http", "https"):
            expected = {urljoin(own_base, relative)}
        elif base and not urlsplit(base.strip()).scheme:
            expected = {urljoin(own_base, relative), urljoin(page_url, relative)}
        else:
            expected = {urljoin(page_url, relative)}
        assert resolved in expected, (
            f"with the page's base {base!r}, its address {relative!r} leads to {resolved}, "
            f"not to {' or '.join(sorted(expected))}"
        )


def test_an_uploaded_pages_relative_addresses_do_not_lead_to_infonium(app: App) -> None:
    for number, base in enumerate(UPLOADED_BASES):
        upload = app.upload(f"saved page {number}.html", addressed_page(base).encode())
        entry = app.settled(upload.json()["id"])
        assert stand(entry) == "read/settled", f"an uploaded page was not read: {entry['reading']}"
        leads = page_addresses(served(app, entry["id"]))
        assert leads, f"with the page's base {base!r}, the served page lost its addresses"
        to_infonium = [
            (written, resolved)
            for written, resolved in leads
            if urlsplit(resolved).netloc == SERVED_HOST
        ]
        assert not to_infonium, (
            f"with the page's base {base!r}, addresses of an uploaded page lead to infonium: "
            f"{to_infonium}"
        )


def doctype_of(page_html: str) -> str | None:
    declared = DOCTYPE.match(page_html)
    return None if declared is None else " ".join(declared.group(1).split()).lower()


KEPT_DOCTYPES = (
    "<!DOCTYPE html>",
    "<!doctype html>",
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN"\n'
    '  "http://www.w3.org/TR/html4/loose.dtd">',
    '<!DOCTYPE html SYSTEM "about:legacy-compat">',
    "",
)


def keeps_its_document_type(app: App, doctype: str) -> None:
    page = article_page("", doctype=doctype)
    served_page = served(app, app.read_page(next(names), page.encode()))
    assert doctype_of(served_page.markup) == doctype_of(page), (
        f"the page declared {doctype!r} and is served with {doctype_of(served_page.markup)!r}"
    )


def test_the_page_keeps_its_document_type(app: App) -> None:
    for doctype in KEPT_DOCTYPES:
        keeps_its_document_type(app, doctype)


@pytest.mark.parametrize(
    "doctype",
    [
        '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN" "http://www.w3.org/TR/html4/strict.dtd">',
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">',
        '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">',
        '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">',
        '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">',
    ],
)
def test_the_page_keeps_a_document_type_of_an_older_kind(app: App, doctype: str) -> None:
    keeps_its_document_type(app, doctype)


# Elements that give a page its layout, each told apart by its attributes.
LAYOUT = (
    ("div", {"class": "layout wide", "style": "display: flex; gap: 2em"}),
    ("section", {"id": "main", "class": "column", "data-width": "640"}),
    ("aside", {"class": "column narrow", "style": "float: right", "lang": "en"}),
    ("p", {"class": "lede", "style": "font-size: 1.4em", "dir": "ltr"}),
    ("h2", {"class": "title", "style": "color: #333"}),
    ("li", {"class": "entry", "value": "3"}),
    ("td", {"class": "number", "colspan": "2", "style": "text-align: right"}),
    ("img", {"class": "hero", "src": "https://pictures.test/hero.jpg", "width": "640"}),
)


def test_the_pages_structure_classes_and_styles_are_kept(app: App) -> None:
    opened = {
        tag: "<{tag} {attributes}>".format(
            tag=tag, attributes=" ".join(f"{name}='{value}'" for name, value in attributes.items())
        )
        for tag, attributes in LAYOUT
    }
    body = (
        f"{opened['div']}{opened['section']}{opened['h2']}The night ferry</h2>"
        f"{opened['p']}It left at a quarter past nine on every weekday.</p>"
        f"<ol>{opened['li']}Wednesday, in fog</li><li>Thursday</li></ol>"
        f"<table><tr>{opened['td']}Forty-one late nights</td></tr><tr><td>a</td><td>b</td></tr>"
        f"</table>{opened['img']}</section>"
        f"{opened['aside']}<p>A note beside the article, about the harbour.</p></aside></div>"
    )
    root = served(app, app.read_page(next(names), article_page(body).encode())).root()
    for tag, attributes in LAYOUT:
        kept = [
            element
            for element in root.iter(tag)
            if all(element.get(name) == value for name, value in attributes.items())
        ]
        assert len(kept) == 1, f"the page's {opened[tag]} is not in the served page as it was"


ENCODED_WORDS = (
    (
        "windows-1252",
        "Le café naïve à côté de l\N{RIGHT SINGLE QUOTATION MARK}Œuvre — ça coûte 5 €",
    ),
    ("iso-8859-1", "El árbol de la mañana, señor, está aquí"),
    ("shift_jis", "日本語の段落もある。港の帳簿は町の図書館にある。"),
    ("gb2312", "港口的账簿至今仍保存在镇图书馆里。"),
    ("koi8-r", "Ночной паром уходил в четверть десятого."),
)


def test_the_pages_text_is_decoded_as_the_extractor_decodes_it(app: App) -> None:
    for charset, words in ENCODED_WORDS:
        decoded_as_the_extractor_decodes_it(app, charset, words)


def decoded_as_the_extractor_decodes_it(app: App, charset: str, words: str) -> None:
    page = (
        f"<!DOCTYPE html><html><head><meta charset='{charset}'><title>Kinds</title></head>"
        f"<body><article><p>{words}</p>{FILLER}</article></body></html>"
    )
    served_page = served(app, app.read_page(next(names), page.encode(charset)))
    [number] = [n for n, block in enumerate(served_page.blocks) if block.text == words]
    holders = [
        element
        for element in block_marks(served_page, served_page.root())
        if element.get(served_page.block_attribute) == str(number)
    ]
    assert [shown_text(element) for element in holders] == [words], (
        f"the words of a page in {charset} are served as {[shown_text(e) for e in holders]}"
    )


NOSCRIPT_PICTURE = "https://pictures.test/noscript.jpg"


NOSCRIPT_PICTURES = (
    ("width='1' height='1'", False),
    ("width='0' height='0'", False),
    ("style='display:none'", False),
    ("style='visibility: hidden'", False),
    ("hidden", False),
    ("", True),
    ("width='640' height='480'", True),
    ("alt='The night ferry at the pier' class='lazy'", True),
)


def test_a_picture_for_no_script_is_left_out_only_when_it_is_not_to_be_seen(app: App) -> None:
    for attributes, kept in NOSCRIPT_PICTURES:
        picture_for_no_script_is_left_out(app, attributes, kept)


def picture_for_no_script_is_left_out(app: App, attributes: str, kept: bool) -> None:
    picture = f"<noscript><img src='{NOSCRIPT_PICTURE}' {attributes}></noscript>"
    page = (
        "<!DOCTYPE html><html><head><title>Pictures</title></head><body><article>"
        f"<p>The night ferry.</p>{picture}{FILLER}</article></body></html>"
    )
    served_page = served(app, app.read_page(next(names), page.encode()))
    shown = [img for img in served_page.root().iter("img") if img.get("src") == NOSCRIPT_PICTURE]
    assert bool(shown) == kept, (
        f"a picture for no script with {attributes or 'no size'!r} was "
        f"{'left out' if kept else 'kept'}"
    )


TWICE = (
    ("<h2>{words}</h2>", "<p>{words}</p>"),
    ("<p>{words}</p>", "<h2>{words}</h2>"),
    ("<nav><p>{words}</p></nav><h2>{words}</h2>", "<p>{words}</p>"),
    ("<p>{words}</p>", "<h3><span>{words}</span></h3>"),
)


@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(said=words)
def test_a_text_that_stands_twice_is_marked_on_the_element_of_its_kind(app: App, said: str) -> None:
    for first, second in TWICE:
        marked_on_the_element_of_its_kind(app, first, second, said)


def marked_on_the_element_of_its_kind(app: App, first: str, second: str, said: str) -> None:
    body = f"{first.format(words=said)}{FILLER}{second.format(words=said)}"
    page = article_page(body)
    served_page = served(app, app.read_page(next(names), page.encode()))
    root = served_page.root()
    repeated = [
        (number, block)
        for number, block in enumerate(served_page.blocks)
        if same_text(block.text) == same_text(unescape(said))
    ]
    assert repeated, f"the made-up page's repeated words {said!r} were not read"
    for number, block in repeated:
        for element in block_marks(served_page, root):
            if element.get(served_page.block_attribute) != str(number):
                continue
            on_heading = element.tag in HEADING_TAGS or any(
                ancestor.tag in HEADING_TAGS for ancestor in element.iterancestors()
            )
            assert on_heading == isinstance(block, Heading), (
                f"the {block.kind} {said!r} is marked on a {element.tag!r} element "
                f"{'in' if on_heading else 'outside'} a heading"
            )


def failure_detail(app: App, path: str) -> tuple[int, str]:
    answer = app.client.get(path)
    detail = answer.json().get("detail") if answer.status_code == 404 else None
    return answer.status_code, detail if isinstance(detail, str) else ""


def test_only_an_item_read_from_a_web_page_has_a_page(app: App) -> None:
    pdf = (PDF_DATA / "samples" / "trap.pdf").read_bytes()
    read_pdf = app.settled(app.upload("trap.pdf", pdf).json()["id"])
    unread = app.settled(app.link(f"{SITE}/no-such-page").json()["id"])
    assert (stand(read_pdf), stand(unread)) == ("read/settled", "unread/failed"), (
        "the items a test needs were not read as it needs them"
    )
    uploaded = app.settled(app.upload("saved.html", addressed_page("").encode()).json()["id"])
    served(app, uploaded["id"])
    for what, item_id in (
        ("a PDF", read_pdf["id"]),
        ("an item without a document", unread["id"]),
        ("an item not in the library", "0" * 64),
    ):
        for path in (PAGE_PATH, BLOCKS_PATH):
            status, detail = failure_detail(app, path.format(item_id=item_id))
            assert status == 404 and detail.strip(), (
                f"{path} of {what} answered {status} without a failure that says why"
            )


def csp_directives(policy: str) -> dict[str, list[str]]:
    directives = [part.split() for part in policy.split(";") if part.strip()]
    return {directive[0].lower(): directive[1:] for directive in directives}


def test_the_page_is_served_inert_and_quiet_whoever_opens_its_address(app: App) -> None:
    item_id = app.read_page(next(names), addressed_page("").encode())
    answer = app.client.get(PAGE_PATH.format(item_id=item_id))
    assert answer.headers["content-type"].startswith("text/html"), "the page is not served as HTML"
    policy = csp_directives(answer.headers.get("content-security-policy", ""))
    fallback = policy.get("default-src")

    def forbidden(directive: str, *fallbacks: str) -> bool:
        chain = [policy.get(name) for name in (directive, *fallbacks)] + [fallback]
        first = next((sources for sources in chain if sources is not None), None)
        return first == ["'none'"]

    assert "sandbox" in policy and set(policy["sandbox"]) <= {"allow-same-origin"}, (
        f"the page is not sandboxed with its own origin alone: {policy.get('sandbox')}"
    )
    assert forbidden("script-src"), "the page's policy lets scripts run"
    assert forbidden("object-src"), "the page's policy lets plugins load"
    assert forbidden("frame-src", "child-src"), "the page's policy lets frames load"
    assert policy.get("form-action") == ["'none'"], "the page's policy lets forms be sent"
    assert forbidden("connect-src"), "the page's policy lets a link's ping be sent"
    assert answer.headers.get("referrer-policy") == "no-referrer", "the page sends a referrer"
    assert answer.headers.get("x-content-type-options") == "nosniff", "the page may be sniffed"


def snapshot_of(app: App, item_id: str) -> str:
    with app.store.library(LOCAL_OWNER) as library:
        return library.holding(item_id).document.snapshot


@pytest.mark.parametrize("first", [PAGE_PATH, BLOCKS_PATH])
def test_both_endpoints_answer_from_the_page_built_once_for_a_document(
    app: App, first: str
) -> None:
    item_id = app.read_page(next(names), addressed_page("").encode())
    app.client.get(first.format(item_id=item_id))
    page, where = (
        app.client.get(path.format(item_id=item_id)) for path in (PAGE_PATH, BLOCKS_PATH)
    )
    # The kept source changed under the service: a page built anew would be another.
    app.store.snapshot(snapshot_of(app, item_id)).write_bytes(
        f"<html><body><article><h1>Other</h1><p>{FORGED_WORDS[0]}</p></article></body></html>".encode()
    )
    again, where_again = (
        app.client.get(path.format(item_id=item_id)) for path in (PAGE_PATH, BLOCKS_PATH)
    )
    assert (again.text, where_again.json()) == (page.text, where.json()), (
        "the page of a document was built again for a later request"
    )


def test_a_page_read_again_into_another_document_is_served_as_the_new_one(app: App) -> None:
    item_id = app.read("/revised")
    before = served(app, item_id)
    app.link(f"{SITE}/revised")
    assert stand(app.settled(item_id)) == "read/settled", "the page was not read again"
    after = served(app, item_id)
    assert after.blocks != before.blocks, "reading the page again kept the document"
    texts = [same_text(shown_text(element)) for element in block_marks(after, after.root())]
    assert same_text(REVISED[0]) in texts, "the page served is not that of the new document"
    assert same_text(LEDGER[0]) not in texts, "the page served still shows the old document"


# In Chrome: a page that tries every way to run or send something, opened as a tab and in a
# frame, with each line of defence alone (the headers over the page as it was kept, the served
# page without the headers) and with both. A sink of this machine hears what the page sends.
LOOPBACK = "127.0.0.1"
SINK_PORT = 9052
PAGES_PORT = 9053
SINK = f"http://{LOOPBACK}:{SINK_PORT}"
# What a page may still cause a browser to fetch: its stylesheets and pictures, and where a
# link a reader follows leads, with the browser's own request for that site's icon.
FETCHED = frozenset({"/sheet.css", "/picture.png", "/missing-picture", "/followed", "/favicon.ico"})
REFRESH = "/refresh"
# What the page sends when nothing defends against it: each is one way to run or send.
TRIED = frozenset(
    {
        "/script", "/script.js", "/onload", "/onerror", "/onfocus", "/onclick", "/javascript",
        "/animated", "/frame", "/object", "/embed", "/form", "/ping", REFRESH,
    }
)  # fmt: skip
LOAD_SECONDS = 1.0
# A refresh comes later than every click, so that the clicks find the page.
REFRESH_SECONDS = 2
SETTLE_MILLISECONDS = 1500
CLICKS = ("#clicked", "#script-link", "#animated text")
NAVIGATING_CLICKS = ("#send", "#pinging")
EVERYTHING_AT_ONCE = f"""<!DOCTYPE html><html><head><title>Inert</title>
<meta http-equiv="refresh" content="{REFRESH_SECONDS}; url={SINK}{REFRESH}">
<link rel="stylesheet" href="{SINK}/sheet.css">
<script>fetch("{SINK}/script")</script>
<script src="{SINK}/script.js"></script>
</head><body onload="fetch('{SINK}/onload')"><article><h1>Inert page</h1>
<p>A picture: <img src="{SINK}/picture.png" alt=""></p>
<img src="{SINK}/missing-picture" onerror="fetch('{SINK}/onerror')">
<p id="clicked" onclick="fetch('{SINK}/onclick')">A paragraph that answers a click.</p>
<p><a id="script-link" href="javascript:fetch('{SINK}/javascript')">A script link</a></p>
<p><a id="pinging" href="{SINK}/followed" ping="{SINK}/ping">A link that pings</a></p>
<iframe src="{SINK}/frame"></iframe>
<object data="{SINK}/object" type="text/html"></object>
<embed src="{SINK}/embed" type="text/html">
<form action="{SINK}/form" method="post"><input name="q" value="x">
<button id="send">Send</button></form>
<svg width="300" height="40"><a id="animated"><text y="20">An animated link</text>
<set attributeName="href" to="javascript:fetch('{SINK}/animated')" begin="0s"/></a></svg>
<input autofocus onfocus="fetch('{SINK}/onfocus')">
{FILLER}</article></body></html>"""


class Sink(BaseHTTPRequestHandler):
    heard: ClassVar[list[str]] = []

    def answer(self) -> None:
        self.heard.append(urlsplit(self.path).path)
        status = 404 if self.path == "/missing-picture" else 200
        self.send_response(status)
        self.send_header("content-type", "text/html")
        self.send_header("content-length", "0")
        self.end_headers()

    do_GET = answer
    do_POST = answer

    def log_message(self, format: str, *arguments: object) -> None:
        pass


class Pages(BaseHTTPRequestHandler):
    # The page under each line of defence, by path, and a frame for each.
    served: ClassVar[dict[str, tuple[bytes, dict[str, str]]]] = {}

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/frame/"):
            content = (
                f"<!DOCTYPE html><iframe id='page' src='/{path.removeprefix('/frame/')}' "
                "width='900' height='700'></iframe>"
            ).encode()
            headers = {"content-type": "text/html"}
        elif path in self.served:
            content, headers = self.served[path]
        else:
            content, headers = b"", {}
        self.send_response(200 if content else 404)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("content-length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *arguments: object) -> None:
        pass


@contextmanager
def listening(handler: type[BaseHTTPRequestHandler], port: int) -> Iterator[None]:
    with ThreadingHTTPServer((LOOPBACK, port), handler) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            yield
        finally:
            server.shutdown()


def heard_after(browser: Browser, address: str, framed: bool, clicks: tuple[str, ...]) -> list[str]:
    # What the sink hears while the page is open at the address, alone or in a frame, and
    # these of its elements are clicked.
    Sink.heard.clear()
    tab = browser.new_page()
    try:
        tab.goto(address, wait_until="load")
        time.sleep(LOAD_SECONDS)
        frame = tab.frame_locator("#page") if framed else tab
        for selector in clicks:
            target = frame.locator(selector)
            if target.count():
                with suppress(PlaywrightError):
                    target.first.click(timeout=SETTLE_MILLISECONDS, no_wait_after=True)
        time.sleep(REFRESH_SECONDS)
    finally:
        tab.close()
    return list(Sink.heard)


def heard_with_every_try(browser: Browser, address: str, framed: bool) -> set[str]:
    # A click that follows a link or sends a form leaves the page, so each is made on a page
    # of its own.
    heard = heard_after(browser, address, framed, CLICKS)
    for click in NAVIGATING_CLICKS:
        heard += heard_after(browser, address, framed, (click,))
    return set(heard)


@pytest.mark.chrome
@pytest.mark.enable_socket
def test_nothing_of_the_page_runs_or_is_sent_in_chrome_under_each_line_of_defence(
    app: App, chrome: Path
) -> None:
    item_id = app.read_page(next(names), EVERYTHING_AT_ONCE.encode())
    answer = app.client.get(PAGE_PATH.format(item_id=item_id))
    assert answer.status_code == 200, "the page a test needs was not served"
    headers = {
        name: answer.headers[name]
        for name in (
            "content-type",
            "content-security-policy",
            "referrer-policy",
            "x-content-type-options",
        )
    }
    plain = {"content-type": answer.headers["content-type"]}
    Pages.served = {
        "/both": (answer.content, headers),
        "/headers": (EVERYTHING_AT_ONCE.encode(), headers),
        "/page": (answer.content, plain),
        "/neither": (EVERYTHING_AT_ONCE.encode(), plain),
    }
    with (
        listening(Sink, SINK_PORT),
        listening(Pages, PAGES_PORT),
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(executable_path=chrome, chromium_sandbox=True)
        pages = f"http://{LOOPBACK}:{PAGES_PORT}"
        try:
            unguarded = heard_with_every_try(browser, f"{pages}/neither", framed=False)
            assert unguarded >= TRIED, (
                f"without a defence the page did not send {sorted(TRIED - unguarded)}, so its "
                "test proves nothing of them"
            )
            for line in ("both", "headers", "page"):
                for framed in (False, True):
                    address = f"{pages}/{'frame/' if framed else ''}{line}"
                    heard = heard_with_every_try(browser, address, framed)
                    where = f"{'in a frame' if framed else 'as a tab'} with {line}"
                    assert "/picture.png" in heard, f"the page did not load {where}: {heard}"
                    sent = sorted(heard - FETCHED)
                    assert not sent, f"the page ran or sent {sent} {where}"
        finally:
            browser.close()
