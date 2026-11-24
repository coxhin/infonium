import pytest
from hypothesis import given
from hypothesis import strategies as st

from infonium import webpage
from infonium.document import Block, Heading, LinkMark, Paragraph, StyleMark, Table

URL = "https://example.test/posts/night-trains"
VOCABULARY = (
    "river", "sleeper", "ledger", "harbour", "window", "morning", "letter", "valley", "station",
    "carriage", "timetable", "platform", "border", "evening", "journey", "summer",
)  # fmt: skip
# Words that only a part the page does not show holds, so that finding one in what was read
# tells that the part was read.
UNSHOWN = ("quorvex", "zindlap", "mertusk", "plovane", "brastic", "olquin", "vespard", "tundrol")
DIALOGS = (
    "<dialog open>{}</dialog>",
    "<dialog>{}</dialog>",
    "<div role='dialog'>{}</div>",
    "<div role='alertdialog' aria-modal='true'>{}</div>",
    "<div role='dialog' hidden>{}</div>",
    "<section role='dialog' style='display: none'>{}</section>",
)
SHOWN_DIALOGS = DIALOGS[0], DIALOGS[2], DIALOGS[3]
HIDDEN_DIALOGS = (
    "<div role='dialog' hidden>{}</div>",
    "<div role='dialog' style='display:none'>{}</div>",
    "<dialog style='visibility: hidden'>{}</dialog>",
)
SCREEN_READER_CLASSES = (
    "sr-only",
    "visually-hidden",
    "visuallyhidden",
    "screen-reader-text",
    "govuk-visually-hidden",
)
SHOWN_CLASSES = ("md:sr-only", "u-visually-hidden@mobile", "sr-only not-sr-only")
HEADING_MARKS = ("#", "¶", "§", "🔗", "")
STYLE_TAGS = {
    "b": "strong",
    "strong": "strong",
    "i": "emphasis",
    "em": "emphasis",
    "u": "underline",
}

phrases = st.lists(st.sampled_from(VOCABULARY), min_size=12, max_size=30).map(" ".join)
articles = st.lists(phrases, min_size=4, max_size=7)
unshown = st.lists(st.sampled_from(UNSHOWN), min_size=8, max_size=30).map(" ".join)
names = st.lists(st.sampled_from(VOCABULARY), min_size=1, max_size=3).map(
    lambda words: " ".join(word.capitalize() for word in words)
)


def paragraphs(article: list[str]) -> str:
    return "".join(f"<p>{text}.</p>" for text in article)


def read(body: str) -> list[Block]:
    page = f"<!doctype html><html><head><title>Trains</title></head><body>{body}</body></html>"
    return webpage.page_text(page.encode(), URL).blocks


def text_of(blocks: list[Block]) -> str:
    return "\n".join(block.text for block in blocks)


def holds_unshown(blocks: list[Block]) -> bool:
    return any(word in text_of(blocks) for word in UNSHOWN)


@given(
    article=articles,
    banner=unshown,
    dialog=st.sampled_from(DIALOGS),
    at=st.sampled_from(["before", "inside", "after"]),
)
def test_a_dialog_beside_the_pages_text_is_not_read(
    article: list[str], banner: str, dialog: str, at: str
) -> None:
    shown = dialog.format(f"<p>{banner}.</p>")
    before, inside, after = (
        shown if at == place else "" for place in ("before", "inside", "after")
    )
    page = f"<article>{paragraphs(article[:2])}{inside}{paragraphs(article[2:])}</article>"
    blocks = read(f"{before}{page}{after}")
    assert not holds_unshown(blocks), f"the text of the dialog {dialog!r} was read with the page"
    assert article[0] in text_of(blocks), "the page beside a dialog was not read"


@given(article=articles, dialog=st.sampled_from(DIALOGS), part=st.sampled_from(["article", "main"]))
def test_a_dialog_that_holds_the_pages_main_part_is_read(
    article: list[str], dialog: str, part: str
) -> None:
    blocks = read(dialog.format(f"<{part}>{paragraphs(article)}</{part}>"))
    assert article[0] in text_of(blocks), f"the page's {part} inside {dialog!r} was not read"


@given(article=articles, dialog=st.sampled_from(SHOWN_DIALOGS))
def test_a_shown_dialog_that_holds_the_pages_text_is_read(article: list[str], dialog: str) -> None:
    blocks = read(dialog.format(f"<div>{paragraphs(article)}</div>") + "<p>A short note.</p>")
    assert article[0] in text_of(blocks), f"the page's text in the shown {dialog!r} was not read"


@pytest.mark.parametrize("dialog", HIDDEN_DIALOGS)
@given(text=st.lists(unshown, min_size=4, max_size=6), note=phrases)
def test_a_hidden_dialog_is_not_read_however_much_it_holds(
    dialog: str, text: list[str], note: str
) -> None:
    blocks = read(dialog.format(f"<div>{paragraphs(text)}</div>") + f"<p>{note}.</p>")
    assert not holds_unshown(blocks), f"the text of the hidden {dialog!r} was read"


@given(text=st.lists(unshown, min_size=4, max_size=6), note=phrases)
def test_a_closed_dialog_is_not_read_however_much_it_holds(text: list[str], note: str) -> None:
    blocks = read(f"<dialog><div>{paragraphs(text)}</div></dialog><p>{note}.</p>")
    assert not holds_unshown(blocks), "the text of a closed <dialog> was read"


@given(article=articles, said=unshown, kind=st.sampled_from(SCREEN_READER_CLASSES))
def test_text_for_screen_readers_beside_words_is_not_read(
    article: list[str], said: str, kind: str
) -> None:
    rated = f"<p>Rated <span class='{kind}'>{said}</span> by riders this summer.</p>"
    blocks = read(f"<article>{paragraphs(article)}{rated}</article>")
    assert not holds_unshown(blocks), f"text of class {kind!r} beside words was read"
    assert "Rated by riders" in text_of(blocks), "the words beside a screen reader's text are lost"


@given(article=articles, said=phrases, kind=st.sampled_from(SHOWN_CLASSES))
def test_text_shown_again_or_hidden_at_some_widths_only_is_read(
    article: list[str], said: str, kind: str
) -> None:
    rated = f"<p>Rated <span class='{kind}'>{said}</span> by riders this summer.</p>"
    blocks = read(f"<article>{paragraphs(article)}{rated}</article>")
    assert f"Rated {said} by riders" in text_of(blocks), f"text of class {kind!r} was not read"


@given(article=articles, kind=st.sampled_from(SCREEN_READER_CLASSES + SHOWN_CLASSES))
def test_text_for_screen_readers_that_stands_for_an_icon_is_read(
    article: list[str], kind: str
) -> None:
    table = (
        f"<table><tr><th>Wifi</th><td><span class='{kind}'>Not available</span></td></tr>"
        "<tr><th>Showers</th><td>Yes</td></tr></table>"
    )
    blocks = read(f"<article>{paragraphs(article)}{table}</article>")
    tables = [block.text for block in blocks if isinstance(block, Table)]
    assert tables == ["Wifi\tNot available\nShowers\tYes"], (
        f"text of class {kind!r}, the only words of its cell, was not read"
    )


@st.composite
def marked_headings(draw: st.DrawFn) -> tuple[str, str]:
    # A heading and the mark that links to its own place, as pages set it: inside the heading
    # or right beside it, leading to the heading or to a place named nowhere.
    words = draw(names)
    mark = draw(st.sampled_from(HEADING_MARKS))
    target = draw(st.sampled_from(["#sleepers", f"{URL}#sleepers", "#nowhere"]))
    link = f"<a href='{target}' class='anchor'>{mark}</a>"
    heading = draw(
        st.sampled_from(
            [
                "<h2 id='sleepers'>{words} {link}</h2>",
                "<h2 id='sleepers'>{link}{words}</h2>",
                "<h2 id='sleepers'>{words}</h2>{link}",
                "{link}<h2 id='sleepers'>{words}</h2>",
                "<div id='sleepers'><h2>{words}</h2>{link}</div>",
            ]
        )
    )
    return words, heading.format(words=words, link=link)


@given(article=articles, heading=marked_headings())
def test_a_mark_that_links_to_its_heading_is_not_read(
    article: list[str], heading: tuple[str, str]
) -> None:
    words, markup = heading
    blocks = read(f"<article>{markup}{paragraphs(article)}</article>")
    headings = [block.text for block in blocks if isinstance(block, Heading)]
    assert headings == [words], f"{markup!r} was read as the headings {headings!r}"


@given(article=articles, words=names, mark=st.sampled_from(["*", "1", "†"]))
def test_a_mark_that_leads_elsewhere_in_the_page_is_read(
    article: list[str], words: str, mark: str
) -> None:
    note = "<p id='note'>The note on sleepers.</p>"
    markup = f"<h2 id='sleepers'>{words}<a href='#note'>{mark}</a></h2>"
    blocks = read(f"<article>{markup}{paragraphs(article)}{note}</article>")
    headings = [block.text for block in blocks if isinstance(block, Heading)]
    assert headings == [f"{words}{mark}"], f"the note mark of {markup!r} was not read"


@given(article=articles, name=names, logo=unshown)
def test_a_picture_in_a_cell_beside_the_words_of_its_link_is_not_read(
    article: list[str], name: str, logo: str
) -> None:
    picture = f"<img src='https://acme.test/logo.png' alt='{logo}'>"
    cell = f"<td><a href='https://acme.test/'>{picture} {name}</a></td><td>Night</td>"
    table = f"<table><tr><th>Operator</th><th>Line</th></tr><tr>{cell}</tr></table>"
    blocks = read(f"<article>{paragraphs(article)}{table}</article>")
    tables = [block.text for block in blocks if isinstance(block, Table)]
    assert tables == [f"Operator\tLine\n{name}\tNight"], f"the cell's logo was read: {tables!r}"


@st.composite
def links_side_by_side(draw: st.DrawFn) -> tuple[str, str, str]:
    # Two links with nothing but space between them, whose words may sit in a style.
    first, second = draw(names), draw(names)
    style = draw(st.sampled_from(["", *STYLE_TAGS]))
    gap = draw(st.sampled_from([" ", "&nbsp;", "\n", " &nbsp; ", "\N{NO-BREAK SPACE}"]))
    wrapped = [f"<{style}>{words}</{style}>" if style else words for words in (first, second)]
    links = (
        f"<a href='https://line.test/1'>{wrapped[0]}</a>{gap}"
        f"<a href='https://line.test/2'>{wrapped[1]}</a>"
    )
    return first, second, links


def sentence_with(links: str) -> str:
    return (
        f"<p>The summer timetable lists {links} and many other stops along the line in the "
        "long months of the year.</p>"
    )


@given(article=articles, linked=links_side_by_side())
def test_links_side_by_side_keep_a_space_between_their_words(
    article: list[str], linked: tuple[str, str, str]
) -> None:
    first, second, links = linked
    blocks = read(f"<article>{paragraphs(article)}{sentence_with(links)}</article>")
    assert f"lists {first} {second} and" in text_of(blocks), (
        f"the words of the links {links!r} were not kept apart by one space"
    )


@pytest.mark.xfail(
    strict=True,
    reason="trafilatura drops the first of two links side by side whose words sit in a style "
    "element, such as <a><u>…</u></a> <a><u>…</u></a>: its words lose their link",
)
@given(article=articles, linked=links_side_by_side())
def test_links_side_by_side_each_keep_their_target(
    article: list[str], linked: tuple[str, str, str]
) -> None:
    first, _, links = linked
    blocks = read(f"<article>{paragraphs(article)}{sentence_with(links)}</article>")
    [sentence] = [
        block for block in blocks if isinstance(block, Paragraph) and "lists" in block.text
    ]
    targets = {
        mark.target: sentence.text[mark.start : mark.end]
        for mark in sentence.marks
        if isinstance(mark, LinkMark)
    }
    assert targets.get("https://line.test/1") == first, f"the first link of {links!r} was lost"


@st.composite
def marked_sentences(draw: st.DrawFn) -> tuple[str, list[tuple[str, str]]]:
    # A sentence of plain words with some runs styled or linked, and what each run is: its
    # words and the style or the target that marks them. A space that parts a run from the
    # words around it may stand inside the element that marks it, or outside.
    markup, runs = draw(phrases), []
    for number in range(draw(st.integers(1, 5))):
        words = draw(names)
        inside = [draw(st.sampled_from(["", " "])) for _ in range(2)]
        outside = [" " if not gap else draw(st.sampled_from(["", " "])) for gap in inside]
        tag = draw(st.sampled_from([*STYLE_TAGS, "code", "a"]))
        target = f"https://line.test/{number}"
        opening = f"<a href='{target}'>" if tag == "a" else f"<{tag}>"
        marked = f"{outside[0]}{opening}{inside[0]}{words}{inside[1]}</{tag}>{outside[1]}"
        markup += marked + draw(phrases)
        runs.append((words, target if tag == "a" else "code" if tag == "code" else STYLE_TAGS[tag]))
    return markup, runs


@given(article=articles, sentence=marked_sentences())
def test_marks_cover_the_words_they_mark(
    article: list[str], sentence: tuple[str, list[tuple[str, str]]]
) -> None:
    markup, runs = sentence
    blocks = read(f"<article>{paragraphs(article)}<p>{markup}.</p></article>")
    [read_sentence] = [block for block in blocks if isinstance(block, Paragraph) and block.marks]
    marked = [
        (
            read_sentence.text[mark.start : mark.end],
            mark.style if isinstance(mark, StyleMark) else mark.target,
        )
        for mark in read_sentence.marks
        if isinstance(mark, StyleMark | LinkMark)
    ]
    assert marked == runs, f"the marks of {markup!r} cover other words than they mark"
