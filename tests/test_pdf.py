import gzip
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, get_args

import pikepdf
import pypdfium2
import pytest
from hypothesis import given
from hypothesis import strategies as st
from pikepdf import Name
from PIL import Image, ImageChops, ImageStat

from infonium import pdf, pdfium
from infonium.document import (
    Code,
    Extracted,
    Figure,
    FormulaMark,
    ListItem,
    PagePlace,
    Paragraph,
    Table,
    put_together,
)
from infonium.mineru import (
    BACKEND_KEY,
    READ_AS_PICTURES_KEY,
    VERSION_KEY,
    Box,
    EngineVersion,
    HybridBackend,
    Line,
    ParsedPage,
    RegionType,
    SpanType,
    Text,
    TextSpan,
    UnparsedFile,
)
from tests.pdf_engine_standin import DRAWN, PDF_DATA

# The samples were drawn for infonium; the reference is what commit 6facef0 read from them,
# which a fresh run of the engine gave again on 2026-11-21, with a figure where each picture
# and chart stands, as infonium reads them since. It is what was read then, not what is right:
# in turned_digital a heading reads "accou nt", a known limit of turned pages.
SAMPLES = sorted(sample.stem for sample in (PDF_DATA / "samples").glob("*.pdf"))
REFERENCE = json.loads((PDF_DATA / "handed.json").read_text("utf-8"))["made_by"]
# The ligatures that begin with an f, by their letters, for the rules by which the reader puts
# a PDF's words together.
LIGATURE_OF = {
    "ff": "\N{LATIN SMALL LIGATURE FF}",
    "fi": "\N{LATIN SMALL LIGATURE FI}",
    "fl": "\N{LATIN SMALL LIGATURE FL}",
    "ffi": "\N{LATIN SMALL LIGATURE FFI}",
    "ffl": "\N{LATIN SMALL LIGATURE FFL}",
}
letters = st.from_regex(r"[A-Za-z]{1,8}", fullmatch=True)
known_words = st.frozensets(st.from_regex(r"[a-z]{1,8}(-[a-z]{1,8})?", fullmatch=True), max_size=6)
# Runs of letters without an f, so that a ligature stands only where one is put.
runs_without_f = st.from_regex(r"[a-eg-z]{1,5}", fullmatch=True)


def reading_of(sample: str, engine: Path, folder: Path) -> Extracted:
    try:
        return pdf.extract_pdf(PDF_DATA / "samples" / f"{sample}.pdf", sample, engine, folder)
    except UnparsedFile as refused:
        pytest.fail(
            f"the stored engine results of {sample} do not apply, so it cannot be judged "
            f"without the engine (run the tests marked mineru): {refused}"
        )


def page_count(sample: str) -> int:
    document = pypdfium2.PdfDocument(PDF_DATA / "samples" / f"{sample}.pdf")
    try:
        return len(document)
    finally:
        document.close()


def as_stored(extracted: Extracted) -> dict[str, Any]:
    # A reading as the reference keeps it: its title, its blocks, and of each picture the
    # figure it is drawn for and its size in pixels.
    return {
        "title": extracted.title,
        "blocks": [block.model_dump(mode="json") for block in extracted.blocks],
        "pictures": [
            {"block": picture.block, "width": picture.width, "height": picture.height}
            for picture in extracted.pictures
        ],
    }


def differences(expected: dict[str, Any], read: dict[str, Any]) -> str:
    if len(expected["blocks"]) != len(read["blocks"]):
        return f"{len(expected['blocks'])} blocks became {len(read['blocks'])}"
    changed = [
        number
        for number, (before, after) in enumerate(
            zip(expected["blocks"], read["blocks"], strict=True)
        )
        if before != after
    ]
    title = "" if expected["title"] == read["title"] else f"the title became {read['title']!r}; "
    pictures = (
        ""
        if expected["pictures"] == read["pictures"]
        else f"; the pictures {expected['pictures']} became {read['pictures']}"
    )
    return f"{title}blocks {changed} differ{pictures}"


@pytest.mark.parametrize("sample", SAMPLES)
def test_a_pdf_reads_as_the_reference_from_the_engines_stored_results(
    sample: str, standin_pdf_engine: Callable[[str], Path], tmp_path: Path
) -> None:
    extracted = reading_of(sample, standin_pdf_engine(sample), tmp_path)
    pages = page_count(sample)
    figures = {number for number, block in enumerate(extracted.blocks) if isinstance(block, Figure)}
    unplaced = [
        number
        for number, block in enumerate(extracted.blocks)
        if not block.places and number not in figures
    ]
    places = [place for block in extracted.blocks for place in block.places]
    on_pages = [place.page for place in places if isinstance(place, PagePlace)]
    off_pages = set(on_pages) - set(range(pages))
    pictured = [picture.block for picture in extracted.pictures]
    assert not unplaced, f"blocks {unplaced} of {sample} were read without a place in the file"
    assert len(on_pages) == len(places), f"blocks of {sample} were placed elsewhere than on pages"
    assert not off_pages, f"{sample} has {pages} pages, and places were put on pages {off_pages}"
    assert sorted(pictured) == sorted(figures), (
        f"the figures {sorted(figures)} of {sample} have the pictures {pictured}, not one each"
    )
    expected = json.loads((PDF_DATA / "readings" / f"{sample}.json").read_text("utf-8"))
    read = as_stored(extracted)
    assert read == expected, (
        f"{sample} is read otherwise than {REFERENCE} read it: {differences(expected, read)}"
    )


@pytest.mark.mineru
@pytest.mark.parametrize("sample", ["trap", "marks_digital", "turned_digital"])
def test_a_pdf_reads_as_the_reference_with_the_engine(
    sample: str, pdf_engine: Path, tmp_path: Path
) -> None:
    extracted = pdf.extract_pdf(
        PDF_DATA / "samples" / f"{sample}.pdf", sample, pdf_engine, tmp_path
    )
    expected = json.loads((PDF_DATA / "readings" / f"{sample}.json").read_text("utf-8"))
    read = as_stored(extracted)
    assert read == expected, (
        f"{sample} is read by the engine otherwise than {REFERENCE} read it: "
        f"{differences(expected, read)}"
    )


@given(head=letters, tail=letters, known=known_words)
def test_a_hyphen_at_a_line_end_breaks_a_word_the_document_shows_whole(
    head: str, tail: str, known: frozenset[str]
) -> None:
    whole = f"{head}{tail}".casefold()
    assert pdf.breaks_word(head, tail, known | {whole}), (
        f"{head}- {tail} was not taken for the broken {whole}, which the document shows whole"
    )


@given(head=letters, tail=letters, known=known_words, shown=st.sampled_from(["compound", "halves"]))
def test_a_hyphen_at_a_line_end_joins_a_compound_the_document_shows(
    head: str, tail: str, known: frozenset[str], shown: str
) -> None:
    compound = f"{head}{pdf.HYPHEN}{tail}".casefold()
    halves = {compound} if shown == "compound" else {head.casefold(), tail.casefold()}
    told = (known | halves) - {f"{head}{tail}".casefold()}
    assert not pdf.breaks_word(head, tail, told), (
        f"{head}- {tail} was taken for a broken word, where the document shows it as {shown}"
    )


@given(head=letters, tail=letters, known=known_words)
def test_a_hyphen_between_words_shown_nowhere_breaks_a_word_going_on_in_lower_case(
    head: str, tail: str, known: frozenset[str]
) -> None:
    forms = {f"{head}{tail}", f"{head}{pdf.HYPHEN}{tail}", head, tail}
    told = known - {form.casefold() for form in forms}
    assert pdf.breaks_word(head, tail, told) == tail[0].islower(), (
        f"{head}- {tail}, shown nowhere else, was judged otherwise than by its next letter's case"
    )


@given(
    head=letters,
    tail=letters,
    after=st.sampled_from(["", ".", ",", ")", ";"]),
    hyphen=st.sampled_from(pdf.HYPHENS),
    shown=st.sampled_from(["whole", "compound", "neither"]),
)
def test_a_word_that_goes_on_in_the_next_line_is_put_together_as_the_document_writes_it(
    head: str, tail: str, after: str, hyphen: str, shown: str
) -> None:
    whole, compound = f"{head}{tail}".casefold(), f"{head}{pdf.HYPHEN}{tail}".casefold()
    known = frozenset({"whole": {whole}, "compound": {compound}, "neither": set()}[shown])
    last, first = f"{head}{hyphen}", f"{tail}{after}"
    broken = shown == "whole" or (shown == "neither" and tail[0].islower())
    expected = f"{head}{tail}{after}" if broken else f"{head}{hyphen}{tail}{after}"
    assert put_together([pdf.line_end(last, first, known), first]) == expected, (
        f"the line end {last!r} before {first!r} was not put together as the document writes it"
    )


@given(last=letters, first=letters, known=known_words)
def test_a_line_that_does_not_end_in_a_hyphen_ends_where_it_ends(
    last: str, first: str, known: frozenset[str]
) -> None:
    assert pdf.line_end(last, first, known) == last, f"the line end {last!r} was changed"


@given(head=letters, hyphen=st.sampled_from(pdf.HYPHENS), first=st.sampled_from(["(a", "“so", "—"]))
def test_a_hyphen_before_a_line_that_begins_without_a_letter_stays(
    head: str, hyphen: str, first: str
) -> None:
    last = f"{head}{hyphen}"
    assert pdf.line_end(last, first, frozenset()) == last, (
        f"the line end {last!r} was joined to {first!r}, which begins without a letter"
    )


@st.composite
def lines_of_words(draw: st.DrawFn) -> tuple[list[list[str]], set[str]]:
    # Lines of a text, and the words that stand inside a line: those at a line's ends are of a
    # kind of their own, which never stands inside one.
    edges = draw(st.lists(st.from_regex(r"q[a-z]{1,6}", fullmatch=True), min_size=1, unique=True))
    inner = draw(st.lists(st.from_regex(r"z[a-z]{1,6}", fullmatch=True), min_size=1, unique=True))
    lines = []
    for _ in range(draw(st.integers(1, 5))):
        middle = draw(st.lists(st.sampled_from(inner), max_size=4))
        ends = [draw(st.sampled_from(edges)) for _ in range(2)]
        lines.append(draw(st.sampled_from([[ends[0]], [ends[0], *middle, ends[1]]])))
    return lines, {word for line in lines for word in line[1:-1]}


@given(text=lines_of_words())
def test_the_words_a_document_shows_whole_are_those_inside_its_lines(
    text: tuple[list[list[str]], set[str]],
) -> None:
    lines, inside = text
    page = ParsedPage(
        page_idx=0,
        page_size=(612.0, 792.0),
        preproc_blocks=[
            Text(
                type=RegionType.TEXT,
                index=0,
                bbox=(72.0, 72.0, 540.0, 82.0 + 12 * (len(lines) - 1)),
                lines=[
                    Line(
                        bbox=(72.0, 72.0 + 12 * number, 540.0, 82.0 + 12 * number),
                        spans=[TextSpan(type=SpanType.TEXT, content=" ".join(words))],
                    )
                    for number, words in enumerate(lines)
                ],
            )
        ],
        para_blocks=[],
        discarded_blocks=[],
    )
    assert pdf.whole_words([page], {0: frozenset()}) == inside, (
        "a word at the start or the end of a line, which a line break may have cut, was taken "
        "for a whole word, or a word inside a line was not"
    )


@st.composite
def ligatured_words(draw: st.DrawFn) -> tuple[str, str, str]:
    # A word with one ligature among runs of letters without one: its letters, the word as a
    # text layer writes it, with the ligature's character, and as the engine reads it, which
    # keeps only the ligature's first letter.
    before = "".join(draw(st.lists(runs_without_f, max_size=2)))
    after = "".join(draw(st.lists(runs_without_f, max_size=2)))
    ligature = draw(st.sampled_from(sorted(LIGATURE_OF)))
    return (
        f"{before}{ligature}{after}",
        f"{before}{LIGATURE_OF[ligature]}{after}",
        f"{before}{ligature[0]}{after}",
    )


@given(word=ligatured_words())
def test_a_ligature_of_the_text_layer_is_read_as_its_letters(word: tuple[str, str, str]) -> None:
    whole, in_layer, _ = word
    assert in_layer.translate(pdf.LIGATURES) == whole, (
        f"the text layer's {in_layer!r} was not read as {whole!r}"
    )


@given(word=ligatured_words())
def test_a_word_the_engine_read_short_of_a_ligature_is_spelled_as_the_layer_writes_it(
    word: tuple[str, str, str],
) -> None:
    whole, _, read = word
    assert pdf.respelled(read, frozenset({whole})) == whole, (
        f"{read!r}, which the text layer writes {whole!r} with a ligature, was not respelled"
    )
    assert pdf.respelled(read, frozenset()) == read, (
        f"{read!r} was respelled, though the text layer holds no word it could stand for"
    )


# The samples whose stored engine results hold formulas.
FORMULA_SAMPLES = ("special", "odd_scan")
FORMULA_SPANS = frozenset({SpanType.INLINE_FORMULA, SpanType.DISPLAY_FORMULA})
DISPLAY_FORMULA = "\\int_{0}^{1} x \\, dx = \\frac{1}{2}"
# Where the display formula a test adds is set on its page, in the engine's points: below
# everything the page of the sample holds.
DISPLAY_BOX = (100.0, 700.0, 400.0, 730.0)


def engine_results(sample: str) -> dict[str, Any]:
    written: dict[str, Any] = json.loads(
        gzip.decompress((PDF_DATA / "engine" / f"{sample}.json.gz").read_bytes())
    )
    return written


def engine_formulas(node: object) -> list[str]:
    # The content of every formula span in the regions given, in the order they stand.
    match node:
        case {"type": kind, "content": str(content)} if kind in FORMULA_SPANS:
            return [content]
        case dict():
            return [found for value in node.values() for found in engine_formulas(value)]
        case list():
            return [found for value in node for found in engine_formulas(value)]
        case _:
            return []


def unspaced(text: str) -> str:
    return "".join(text.split())


def formula_texts(extracted: Extracted) -> list[str]:
    return [
        block.text[mark.start : mark.end]
        for block in extracted.blocks
        if not isinstance(block, Code | Table | Figure)
        for mark in block.marks
        if isinstance(mark, FormulaMark)
    ]


@pytest.mark.parametrize("sample", FORMULA_SAMPLES)
def test_every_formula_of_a_pdf_has_a_formula_mark_over_its_latex(
    sample: str, standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    pages = engine_results(sample)["middle"]["pdf_info"]
    given = [formula for page in pages for formula in engine_formulas(page["preproc_blocks"])]
    assert given, f"the stored engine results of {sample} hold no formula to judge"
    marked = formula_texts(reading_of(sample, standin_pdf_engine(sample), tmp_path))
    assert sorted(map(unspaced, marked)) == sorted(map(unspaced, given)), (
        f"the formula marks of {sample} do not cover exactly the LaTeX of the engine's formulas: "
        f"{marked} for {given}"
    )


def test_a_display_formula_is_a_paragraph_one_formula_mark_covers_whole(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    results = engine_results("special")
    box = list(DISPLAY_BOX)
    span = {"bbox": box, "type": SpanType.DISPLAY_FORMULA, "content": DISPLAY_FORMULA}
    region = {
        "bbox": box,
        "type": RegionType.DISPLAY_FORMULA,
        "angle": 0,
        "lines": [{"bbox": box, "spans": [span]}],
    }
    results["middle"]["pdf_info"][0]["preproc_blocks"].append(region)
    changed = tmp_path / "special-with-display.json.gz"
    changed.write_bytes(gzip.compress(json.dumps(results).encode()))
    extracted = reading_of("special", standin_pdf_engine("special", changed), tmp_path)
    displayed = [
        block for block in extracted.blocks if unspaced(DISPLAY_FORMULA) in unspaced(block.text)
    ]
    assert len(displayed) == 1, f"the display formula is not one block of its own: {displayed}"
    [block] = displayed
    assert isinstance(block, Paragraph), f"a display formula was read as a {block.kind}"
    assert [(mark.kind, mark.start, mark.end) for mark in block.marks] == [
        ("formula", 0, len(block.text))
    ], f"a display formula is not a paragraph one formula mark covers whole: {block.marks}"
    assert unspaced(block.text) == unspaced(DISPLAY_FORMULA), (
        f"the display formula's paragraph holds more than its LaTeX: {block.text!r}"
    )


# The pages a test draws are US Letter, in points, and so is each page as the engine is given
# it. The engine's boxes are measured from a page's top left corner, a PDF's from its bottom
# left. A test draws in red and blue, and writes with Helvetica, which every PDF reader holds,
# as the font F1.
LETTER = (612.0, 792.0)
RED = "1 0 0 rg"
BLUE = "0 0 1 rg"
FONT = "F1"
# What draws a page as it is shown onto the page stored on its side, a quarter turn
# anticlockwise, for the file to show it turned back a quarter clockwise.
STORED_TURNED = f"0 1 -1 0 {LETTER[1]:g} 0 cm"
# The engine and its version, as its results name them.
[HYBRID] = get_args(HybridBackend)
[ENGINE_VERSION] = get_args(EngineVersion)
# The colours of those fills as a picture holds them. A colour of a picture is a fill's where
# each of its channels stands this near the fill's, as WebP keeps a colour only nearly; and a
# picture holds a fill where one of its pixels has that much more of the fill's own channel
# than of the other fill's.
FILL_COLOURS = {"red": (255, 0, 0), "blue": (0, 0, 255)}
NEAR = 60
WHITE = "1 1 1 rg"
# The sides of a picture, each a quarter of it deep, as parts of its width and height.
PICTURE_SIDES = {
    "top": (0.0, 0.0, 1.0, 0.25),
    "bottom": (0.0, 0.75, 1.0, 1.0),
    "left": (0.0, 0.0, 0.25, 1.0),
    "right": (0.75, 0.0, 1.0, 1.0),
}


def filled(fill: str, box: Box) -> str:
    # Fills an engine's box of a page with a colour, in a PDF's own measure.
    left, top, right, bottom = box
    return f"{fill} {left:g} {LETTER[1] - bottom:g} {right - left:g} {bottom - top:g} re f"


def written(words: str, box: Box) -> str:
    left, _, _, bottom = box
    return f"BT /{FONT} 10 Tf {left:g} {LETTER[1] - bottom:g} Td ({words}) Tj ET"


def drawn_pdf(file: Path, pages: list[tuple[tuple[float, float], int, str]]) -> Path:
    # A PDF of pages a test draws: each stored so many points wide and high, shown turned
    # clockwise by so many quarters, and drawn by the content given.
    with pikepdf.new() as document:
        font = document.make_indirect(
            pikepdf.Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
        )
        for (width, height), quarters, content in pages:
            page = pikepdf.Dictionary(
                Type=Name.Page,
                MediaBox=[0, 0, width, height],
                Rotate=90 * quarters,
                Resources=pikepdf.Dictionary(Font=pikepdf.Dictionary({f"/{FONT}": font})),
                Contents=pikepdf.Stream(document, content.encode()),
            )
            document.pages.append(pikepdf.Page(page))
        document.save(file)
    return file


def words_region(kind: RegionType, box: Box, words: str) -> dict[str, Any]:
    # A region of words as the engine writes one, in a line as wide as the region.
    line = {"bbox": list(box), "spans": [{"type": SpanType.TEXT, "content": words}]}
    return {"type": kind, "bbox": list(box), "lines": [line]}


def text_region(index: int, box: Box, words: str) -> dict[str, Any]:
    return {**words_region(RegionType.TEXT, box, words), "index": index}


def group(kind: RegionType, members: list[dict[str, Any]]) -> dict[str, Any]:
    # A group as the engine writes one, with the box of its body.
    [held] = [member["bbox"] for member in members if member["type"].endswith("_body")]
    return {"type": kind, "bbox": held, "blocks": members}


def body(kind: RegionType, box: Box) -> dict[str, Any]:
    return {"type": kind, "bbox": list(box)}


def drawn_reading(
    file: Path,
    regions: list[dict[str, Any]],
    standin_pdf_engine: Callable[..., Path],
    folder: Path,
) -> Extracted:
    # A PDF a test drew, read from the engine's results it writes for its one page: the page
    # read from its text layer, holding these regions.
    page = {
        "page_idx": 0,
        "page_size": list(LETTER),
        "preproc_blocks": regions,
        "para_blocks": [],
        "discarded_blocks": [],
    }
    results = {
        BACKEND_KEY: HYBRID,
        VERSION_KEY: ENGINE_VERSION,
        READ_AS_PICTURES_KEY: False,
        "pdf_info": [page],
    }
    stored = folder / "drawn.json.gz"
    stored.write_bytes(gzip.compress(json.dumps({"middle": results}).encode()))
    return pdf.extract_pdf(file, file.stem, standin_pdf_engine(DRAWN, stored), folder)


def picture_of(extracted: Extracted, block: int) -> Image.Image:
    [picture] = [picture for picture in extracted.pictures if picture.block == block]
    image = Image.open(picture.file)
    assert image.format == "WEBP" and picture.media_type == "image/webp", (
        f"the picture of block {block} is {image.format}, said to be {picture.media_type}"
    )
    assert image.size == (picture.width, picture.height), (
        f"the picture of block {block} is {image.size}, said to be {picture.width, picture.height}"
    )
    return image.convert("RGB")


def fill_of(colour: Sequence[float]) -> str | None:
    # The fill whose colour a colour stands near in each channel, if any.
    return next(
        (
            name
            for name, filled_in in FILL_COLOURS.items()
            if all(
                abs(channel - own) < NEAR for channel, own in zip(colour, filled_in, strict=True)
            )
        ),
        None,
    )


def holds(image: Image.Image, fill: str) -> bool:
    # Whether any pixel of a picture has more of the fill's channel than of the other fill's,
    # by NEAR or more: white, red, blue and their blends with white hold no other fill.
    own, other = ("R", "B") if fill == "red" else ("B", "R")
    more = ImageChops.subtract(image.getchannel(own), image.getchannel(other))
    [(_, most)] = ImageStat.Stat(more).extrema
    return most >= NEAR


def side_fills(image: Image.Image) -> dict[str, str | None]:
    # The fill of each side of a picture, by the mean of its colours.
    fills = {}
    for side, (left, top, right, bottom) in PICTURE_SIDES.items():
        edges = (left * image.width, top * image.height, right * image.width, bottom * image.height)
        left_edge, top_edge, right_edge, bottom_edge = (round(edge) for edge in edges)
        part = image.crop((left_edge, top_edge, right_edge, bottom_edge))
        fills[side] = fill_of(ImageStat.Stat(part).mean)
    return fills


# Where a test's page holds a picture and a line of words, in the engine's points. A picture
# takes in two points of its page past each side of its box where nothing lies near, which
# at two pixels a point gives it 8 pixels more each way.
PICTURE = (100.0, 100.0, 300.0, 250.0)
OPENING = (72.0, 600.0, 540.0, 612.0)
MARGINS = 8


def grown(box: Box, by: float) -> Box:
    left, top, right, bottom = box
    return (left - by, top - by, right + by, bottom + by)


def test_pictures_and_charts_are_figures_where_the_engine_puts_them_with_pictures_of_their_size(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    # A caption stands before its picture and a note after its chart, and a picture drawn
    # between two items of a list leaves the list going on after it.
    regions = [
        text_region(0, (72.0, 72.0, 540.0, 84.0), "Two pictures of the harbour follow."),
        group(
            RegionType.IMAGE,
            [
                words_region(
                    RegionType.IMAGE_CAPTION,
                    (72.0, 96.0, 540.0, 108.0),
                    "Figure 1. The harbour by day.",
                ),
                body(RegionType.IMAGE_BODY, (72.0, 120.0, 312.0, 300.0)),
            ],
        ),
        group(
            RegionType.CHART,
            [
                body(RegionType.CHART_BODY, (72.0, 320.0, 372.0, 470.0)),
                words_region(
                    RegionType.CHART_FOOTNOTE,
                    (72.0, 476.0, 540.0, 488.0),
                    "Source: the harbour office.",
                ),
            ],
        ),
        text_region(3, (72.0, 500.0, 540.0, 512.0), "• Ferries leave from the north quay."),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, (72.0, 520.0, 192.0, 640.0))]),
        text_region(5, (72.0, 650.0, 540.0, 662.0), "• Buses leave from the square."),
    ]
    file = drawn_pdf(tmp_path / "harbour.pdf", [(LETTER, 0, "")])
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    read = [(block.kind, block.text) for block in extracted.blocks]
    assert read == [
        ("paragraph", "Two pictures of the harbour follow."),
        ("paragraph", "Figure 1. The harbour by day."),
        ("figure", ""),
        ("figure", ""),
        ("paragraph", "Source: the harbour office."),
        ("list_item", "Ferries leave from the north quay."),
        ("figure", ""),
        ("list_item", "Buses leave from the square."),
    ], f"the picture, the chart and their words were read out of the engine's order: {read}"
    items = [block.position for block in extracted.blocks if isinstance(block, ListItem)]
    assert items == [1, 2], f"a picture between two items of a list ended it: {items}"
    sizes = {picture.block: (picture.width, picture.height) for picture in extracted.pictures}
    regions_drawn = {2: (480, 360), 3: (600, 300), 6: (240, 240)}
    assert sizes == {
        number: (width + MARGINS, height + MARGINS)
        for number, (width, height) in regions_drawn.items()
    }, f"the figures were not given pictures of their regions and margins at 144 dpi: {sizes}"
    for number in sizes:
        picture_of(extracted, number)


def test_a_figures_picture_is_its_region_with_a_margin_of_two_points_and_nothing_further(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    # Red fills the region, white the three points around it, and blue all beyond.
    drawing = " ".join(
        [
            filled(BLUE, grown(PICTURE, 20)),
            filled(WHITE, grown(PICTURE, 3)),
            filled(RED, PICTURE),
            written("A red picture.", OPENING),
        ]
    )
    file = drawn_pdf(tmp_path / "red.pdf", [(LETTER, 0, drawing)])
    regions = [
        text_region(0, OPENING, "A red picture."),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, PICTURE)]),
    ]
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    image = picture_of(extracted, 1)
    inside = image.crop((MARGINS, MARGINS, image.width - MARGINS, image.height - MARGINS))
    assert image.size == (400 + MARGINS, 300 + MARGINS), (
        f"the picture of a region of 400 by 300 pixels with nothing near is {image.size}"
    )
    assert fill_of(ImageStat.Stat(inside).mean) == "red" and not holds(image, "blue"), (
        "the picture of a red region with blue three points around it does not show the red, "
        "or shows the blue"
    )


def test_two_pictures_close_together_each_take_half_the_gap_and_nothing_of_the_other(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    # A red picture stands a point above a blue one: each grows half a point toward the other,
    # one pixel, where two points would take in a strip of the other.
    upper = (100.0, 100.0, 300.0, 200.0)
    lower = (100.0, 201.0, 300.0, 300.0)
    drawing = " ".join([filled(RED, upper), filled(BLUE, lower), written("Two pictures.", OPENING)])
    file = drawn_pdf(tmp_path / "close.pdf", [(LETTER, 0, drawing)])
    regions = [
        text_region(0, OPENING, "Two pictures."),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, upper)]),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, lower)]),
    ]
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    # The regions are 400 by 200 and 400 by 198 pixels; each grows by 4 pixels on its three
    # free sides and by 1 toward the other.
    sizes = {picture.block: (picture.width, picture.height) for picture in extracted.pictures}
    assert sizes == {1: (408, 205), 2: (408, 203)}, (
        f"the pictures did not grow by half the gap toward each other: {sizes}"
    )
    red, blue = picture_of(extracted, 1), picture_of(extracted, 2)
    assert holds(red, "red") and not holds(red, "blue"), "the red picture took in the blue one"
    assert holds(blue, "blue") and not holds(blue, "red"), "the blue picture took in the red one"


def test_a_pictures_margin_stops_at_the_edge_of_its_page(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    near_left = (1.0, 100.0, 201.0, 250.0)
    near_corner = (500.0, 700.0, 611.0, 791.5)
    file = drawn_pdf(tmp_path / "edges.pdf", [(LETTER, 0, "")])
    regions = [
        text_region(0, OPENING, "Two pictures by the edges of their page."),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, near_left)]),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, near_corner)]),
    ]
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    # The first, 200 by 150 points, grows by the point left of it and by 2 points on its
    # other sides; the second, 111 by 91.5, by 2 points left and up, by the point right of it
    # and by the half point under it.
    sizes = {picture.block: (picture.width, picture.height) for picture in extracted.pictures}
    assert sizes == {1: (406, 308), 2: (228, 188)}, (
        f"pictures by the edges of their page did not stop there: {sizes}"
    )


def test_a_box_beside_a_picture_leaves_it_half_the_gap_where_the_margin_reaches_it(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    # Level with the first picture, a box 3 points to its right leaves it a point and a half
    # there. A box 2 points to its left but 3 above it lies out of the margin's reach both
    # ways, and leaves it the whole margin. A box off the second picture's lower right corner,
    # a point from it both ways, leaves it half a point to the right and half a point down.
    first = (300.0, 100.0, 450.0, 200.0)
    beside = (453.0, 120.0, 540.0, 180.0)
    above_beside = (250.0, 30.0, 298.0, 97.0)
    second = (100.0, 400.0, 300.0, 550.0)
    off_the_corner = (301.0, 551.0, 400.0, 590.0)
    file = drawn_pdf(tmp_path / "reach.pdf", [(LETTER, 0, "")])
    regions = [
        text_region(0, OPENING, "Two pictures and the boxes around them."),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, first)]),
        text_region(2, beside, "Beside."),
        text_region(3, above_beside, "Above, beside."),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, second)]),
        text_region(5, off_the_corner, "Off the corner."),
    ]
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    # The first, 150 by 100 points, grows by 2 points but on its right, by a point and a half;
    # the second, 200 by 150, by 2 points left and up and by half a point right and down.
    sizes = {picture.block: (picture.width, picture.height) for picture in extracted.pictures}
    assert sizes == {1: (307, 208), 4: (405, 305)}, (
        f"the pictures did not grow by half the gap to the boxes within reach, and by the whole "
        f"margin past those out of it: {sizes}"
    )


def test_a_box_that_overlaps_a_picture_stops_its_margin_on_each_side_it_reaches_past(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    # One box lies in the picture but for the 10 points it reaches past its left side, as the
    # words of a paragraph around a large first letter do; another reaches past its top and
    # its right. The picture grows down alone.
    past_left = (90.0, 110.0, 150.0, 150.0)
    past_top_and_right = (250.0, 90.0, 320.0, 120.0)
    file = drawn_pdf(tmp_path / "overlaps.pdf", [(LETTER, 0, "")])
    regions = [
        text_region(0, OPENING, "A picture that two boxes overlap."),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, PICTURE)]),
        text_region(2, past_left, "Past the left."),
        text_region(3, past_top_and_right, "Past the top and the right."),
    ]
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    sizes = {picture.block: (picture.width, picture.height) for picture in extracted.pictures}
    assert sizes == {1: (400, 300 + MARGINS // 2)}, (
        f"a picture grew into boxes that overlap it: {sizes}"
    )


def test_a_region_too_small_for_a_picture_gives_no_figure_and_leaves_its_caption(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    icon = (400.0, 100.0, 430.0, 130.0)
    drawing = " ".join([filled(RED, PICTURE), filled(BLUE, icon)])
    file = drawn_pdf(tmp_path / "icon.pdf", [(LETTER, 0, drawing)])
    regions = [
        text_region(0, OPENING, "An icon and a picture."),
        group(
            RegionType.IMAGE,
            [
                body(RegionType.IMAGE_BODY, icon),
                words_region(RegionType.IMAGE_CAPTION, (400.0, 134.0, 540.0, 146.0), "An icon."),
            ],
        ),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, PICTURE)]),
    ]
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    read = [(block.kind, block.text) for block in extracted.blocks]
    sizes = {picture.block: (picture.width, picture.height) for picture in extracted.pictures}
    assert read == [
        ("paragraph", "An icon and a picture."),
        ("paragraph", "An icon."),
        ("figure", ""),
    ], f"an icon of 68 by 68 pixels, margins and all, was kept as a figure: {read}"
    assert sizes == {2: (400 + MARGINS, 300 + MARGINS)}, (
        f"the picture is not the one of its figure: {sizes}"
    )
    assert set(side_fills(picture_of(extracted, 2)).values()) == {"red"}, (
        "the figure left after the icon was given another region's picture"
    )


def test_a_picture_whose_drawing_fails_is_lost_with_its_figure_and_told_of_and_the_reading_goes_on(
    standin_pdf_engine: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The memory for the pixels of the red picture, on the left of the page, cannot be had, as
    # it may not be for a very large region; the blue one beside it is drawn.
    beside = (350.0, 100.0, 500.0, 250.0)
    drawing = " ".join([filled(RED, PICTURE), filled(BLUE, beside)])
    file = drawn_pdf(tmp_path / "unmade.pdf", [(LETTER, 0, drawing)])
    regions = [
        text_region(0, OPENING, "Two pictures, one never drawn."),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, PICTURE)]),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, beside)]),
    ]
    drawn = pdfium.OpenPage.drawn

    def out_of_memory_on_the_left(
        page: pdfium.OpenPage, edges: pdfium.PageEdges, scale: float
    ) -> Image.Image:
        if edges[0] < 0.5:
            raise MemoryError
        return drawn(page, edges, scale)

    monkeypatch.setattr(pdfium.OpenPage, "drawn", out_of_memory_on_the_left)
    caplog.set_level(logging.WARNING, logger=pdf.__name__)
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    read = [(block.kind, block.text) for block in extracted.blocks]
    sizes = {picture.block: (picture.width, picture.height) for picture in extracted.pictures}
    assert read == [("paragraph", "Two pictures, one never drawn."), ("figure", "")], (
        f"a figure whose picture could not be drawn was kept, or the other was lost: {read}"
    )
    assert sizes == {1: (300 + MARGINS, 300 + MARGINS)}, f"the pictures kept are {sizes}"
    assert set(side_fills(picture_of(extracted, 1)).values()) == {"blue"}, (
        "the figure left was given the picture that failed"
    )
    told = [record.getMessage() for record in caplog.records if record.name == pdf.__name__]
    assert told == [
        "a figure on page 1 is left without a picture: drawing or encoding its region "
        "(98.0, 98.0, 302.0, 252.0) failed: MemoryError"
    ], f"the log does not tell of the lost picture by its page and region: {told}"


def test_a_picture_on_a_page_the_file_stores_turned_is_its_region_as_the_reader_sees_it(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    # The page is stored on its side and shown upright, so the engine is given it upright
    # and finds the picture where the reader sees it: red above, blue below.
    top, middle, bottom = PICTURE[1], (PICTURE[1] + PICTURE[3]) / 2, PICTURE[3]
    upper = (PICTURE[0], top, PICTURE[2], middle)
    lower = (PICTURE[0], middle, PICTURE[2], bottom)
    shown = " ".join([filled(RED, upper), filled(BLUE, lower)])
    stored = f"q {STORED_TURNED} {shown} Q"
    file = drawn_pdf(tmp_path / "stored.pdf", [((LETTER[1], LETTER[0]), 1, stored)])
    regions = [
        text_region(0, OPENING, "A page stored on its side."),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, PICTURE)]),
    ]
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    image = picture_of(extracted, 1)
    sides = side_fills(image)
    upright = (400 + MARGINS, 300 + MARGINS)
    assert image.size == upright and (sides["top"], sides["bottom"]) == ("red", "blue"), (
        f"the picture of a page stored turned, {image.size}, is not red above blue: {sides}"
    )


def test_a_picture_on_a_page_the_file_only_shows_turned_is_its_region_turned_with_the_page(
    standin_pdf_engine: Callable[..., Path], tmp_path: Path
) -> None:
    # The page is stored upright, with words that stand so, and shown turned a quarter
    # clockwise. The engine is given it as it is stored, red above blue; the reader sees the
    # picture turned with the page, red to the right of blue.
    top, middle, bottom = PICTURE[1], (PICTURE[1] + PICTURE[3]) / 2, PICTURE[3]
    upper = (PICTURE[0], top, PICTURE[2], middle)
    lower = (PICTURE[0], middle, PICTURE[2], bottom)
    words = "Words that stand as the page is stored."
    stored = " ".join([filled(RED, upper), filled(BLUE, lower), written(words, OPENING)])
    file = drawn_pdf(tmp_path / "shown.pdf", [(LETTER, 1, stored)])
    regions = [
        text_region(0, OPENING, words),
        group(RegionType.IMAGE, [body(RegionType.IMAGE_BODY, PICTURE)]),
    ]
    extracted = drawn_reading(file, regions, standin_pdf_engine, tmp_path)
    image = picture_of(extracted, 1)
    sides = side_fills(image)
    turned = (300 + MARGINS, 400 + MARGINS)
    assert image.size == turned and (sides["left"], sides["right"]) == ("blue", "red"), (
        f"the picture of a page shown turned, {image.size}, is not blue left of red: {sides}"
    )
