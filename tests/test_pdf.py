import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pypdfium2
import pytest
from hypothesis import given
from hypothesis import strategies as st

from infonium import pdf
from infonium.document import Extracted, PagePlace, put_together
from infonium.mineru import Line, ParsedPage, RegionType, SpanType, Text, TextSpan, UnparsedFile
from tests.pdf_engine_standin import PDF_DATA

# The samples were drawn for infonium; the reference is what commit 6facef0 read from them,
# which a fresh run of the engine gave again on 2026-11-21. It is what was read then, not what
# is right: in turned_digital a heading reads "accou nt", a known limit of turned pages.
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
    return {
        "title": extracted.title,
        "blocks": [block.model_dump(mode="json") for block in extracted.blocks],
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
    return f"{title}blocks {changed} differ"


@pytest.mark.parametrize("sample", SAMPLES)
def test_a_pdf_reads_as_the_reference_from_the_engines_stored_results(
    sample: str, standin_pdf_engine: Callable[[str], Path], tmp_path: Path
) -> None:
    extracted = reading_of(sample, standin_pdf_engine(sample), tmp_path)
    pages = page_count(sample)
    unplaced = [number for number, block in enumerate(extracted.blocks) if not block.places]
    places = [place for block in extracted.blocks for place in block.places]
    on_pages = [place.page for place in places if isinstance(place, PagePlace)]
    off_pages = set(on_pages) - set(range(pages))
    assert not unplaced, f"blocks {unplaced} of {sample} were read without a place in the file"
    assert len(on_pages) == len(places), f"blocks of {sample} were placed elsewhere than on pages"
    assert not off_pages, f"{sample} has {pages} pages, and places were put on pages {off_pages}"
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
