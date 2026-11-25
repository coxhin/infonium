import json
import tempfile
from pathlib import Path

import pikepdf
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pikepdf import Name

from infonium import pdf
from infonium.font_metrics import (
    DEEPEST_LETTER,
    FIGURES_PER_SIZE,
    MEASURED_LETTERS,
    NAME_SOLIDUS,
    bare_name,
    like_text,
    measured_file,
    unsubset,
)
from infonium.pdfium import LetterReach, letter_reaches, stored_file
from tests.pdf_engine_standin import PDF_DATA
from tests.test_pdf import REFERENCE, SAMPLES, as_stored, differences

# The samples whose text layer is written in Latin letters, in fonts that say how far their
# letters reach.
LATIN_SAMPLES = ["marks_digital", "mixed_layers", "turned_digital"]
# The sample whose fonts are composite, which PDFium names with the tags of their subsets.
COMPOSITE_SAMPLE = "marks_digital"
# What XCharter says of its letters in the paper where plain letters were taken for lowered
# ones and letters with descenders were lost: they reach the x-height and not below the
# baseline.
XCHARTER_ASCENT = 492
XCHARTER_DESCENT = 0
# Letters stand in boxes of one height when the tops of the boxes, and their bottoms, lie no
# further apart than this part of the size they are drawn at: within it lie the letters of
# every font of the samples, and of the fonts of the paper that say how far their letters
# reach.
ONE_HEIGHT = 0.02
# A font made to say how far its letters reach says it to within this many thousandths of its
# size of what their ink shows.
SAID_WITHIN = 20
COPY = "measured.pdf"
MISSTATED = "misstated.pdf"
RENAMED = "renamed.pdf"
# The name of a Japanese font, MS Mincho, as Japanese PDFs write it: in Shift JIS, which is
# not UTF-8.
SHIFT_JIS_NAME = (
    "\N{FULLWIDTH LATIN CAPITAL LETTER M}\N{FULLWIDTH LATIN CAPITAL LETTER S} 明朝".encode(
        "shift_jis"
    )
)
# Two subsets of one font in the composite sample, once the second is named as the first is
# but for its tag.
FIRST_SUBSET = b"BAAAAA+LiberationSerif"
SECOND_SUBSET = b"CAAAAA+LiberationSerif"
SECOND_SUBSET_NAMED = b"CAAAAA+LiberationSerif-Bold"
# A page that a font of the PDF's own procedures writes on: each letter a filled box as high,
# in thousandths of the font's size, as the letter it stands for, in a font that says its
# letters reach a tenth of its size above the baseline and not below it.
BOX_LETTERS = {"a": 500, "l": 750}
BOX_WORD = "all"
BOX_PAGE = f"BT /Boxes 24 Tf 72 700 Td ({BOX_WORD}) Tj ET"


def sample_file(sample: str) -> Path:
    return PDF_DATA / "samples" / f"{sample}.pdf"


def descriptors_of(document: pikepdf.Pdf) -> list[pikepdf.Dictionary]:
    return [
        found
        for found in document.objects
        if isinstance(found, pikepdf.Dictionary) and found.get(Name.Type) == Name.FontDescriptor
    ]


def misstated(file: Path, ascent: int, descent: int, chosen: set[int], copy: Path) -> Path:
    # A copy of a file in which the fonts chosen, by the place of their descriptors among the
    # file's, say that their letters reach so far above the baseline and so far below it.
    with pikepdf.open(file) as document:
        for place, descriptor in enumerate(descriptors_of(document)):
            if place in chosen:
                descriptor[Name.Ascent] = ascent
                descriptor[Name.Descent] = descent
        document.save(copy)
    return copy


def all_misstated(file: Path, copy: Path) -> Path:
    # A copy of a file in which every font says of its letters what XCharter says.
    with pikepdf.open(file) as document:
        every = set(range(len(descriptors_of(document))))
    return misstated(file, XCHARTER_ASCENT, XCHARTER_DESCENT, every, copy)


def renamed(file: Path, old: bytes, new: bytes, copy: Path) -> Path:
    # A copy of a file in which the font named so, and its descriptor, are named otherwise, in
    # whatever bytes are given: a name is written with each byte escaped.
    escaped = NAME_SOLIDUS + b"".join(b"#%02X" % byte for byte in new)
    with pikepdf.open(file) as document:
        for found in document.objects:
            if isinstance(found, pikepdf.Dictionary):
                for key in (Name.BaseFont, Name.FontName):
                    name = found.get(key)
                    if isinstance(name, Name) and bare_name(name) == old:
                        found[key] = pikepdf.Object.parse(escaped)
        document.save(copy)
    return copy


def upside_down(file: Path, copy: Path) -> Path:
    # A copy of a file whose pages are drawn upside down, so that each letter hangs below its
    # baseline as far as it stood above it.
    with pikepdf.open(file) as document:
        for page in document.pages:
            turn = f"1 0 0 -1 0 {float(page.mediabox[3])} cm".encode()
            page.contents_add(pikepdf.Stream(document, turn), prepend=True)
        document.save(copy)
    return copy


def boxes_file(path: Path) -> Path:
    with pikepdf.new() as document:
        procedures = {
            f"/{letter}": pikepdf.Stream(
                document, f"500 0 0 0 400 {top} d1 0 0 400 {top} re f".encode()
            )
            for letter, top in BOX_LETTERS.items()
        }
        codes = sorted(ord(letter) for letter in BOX_LETTERS)
        font = pikepdf.Dictionary(
            Type=Name.Font,
            Subtype=Name.Type3,
            BaseFont=Name("/Boxes"),
            FontBBox=[0, 0, 500, max(BOX_LETTERS.values())],
            FontMatrix=[0.001, 0, 0, 0.001, 0, 0],
            CharProcs=pikepdf.Dictionary(procedures),
            Encoding=pikepdf.Dictionary(
                Type=Name.Encoding,
                Differences=[
                    code
                    for letter in sorted(BOX_LETTERS)
                    for code in (ord(letter), Name(f"/{letter}"))
                ],
            ),
            FirstChar=codes[0],
            LastChar=codes[-1],
            Widths=[500] * (codes[-1] - codes[0] + 1),
            FontDescriptor=document.make_indirect(
                pikepdf.Dictionary(
                    Type=Name.FontDescriptor,
                    FontName=Name("/Boxes"),
                    Flags=32,
                    FontBBox=[0, 0, 500, max(BOX_LETTERS.values())],
                    ItalicAngle=0,
                    Ascent=100,
                    Descent=0,
                    CapHeight=max(BOX_LETTERS.values()),
                    StemV=80,
                )
            ),
        )
        page = pikepdf.Dictionary(
            Type=Name.Page,
            MediaBox=[0, 0, 612, 792],
            Resources=pikepdf.Dictionary(
                Font=pikepdf.Dictionary(Boxes=document.make_indirect(font))
            ),
            Contents=pikepdf.Stream(document, BOX_PAGE.encode()),
        )
        document.pages.append(pikepdf.Page(page))
        document.save(path)
    return path


def figures_of(file: Path) -> dict[bytes, tuple[float, float]]:
    # What each font of a file says of how far its letters reach, by the name of its
    # descriptor, in the order the file holds the descriptors.
    with pikepdf.open(file) as document:
        return {
            bare_name(name): (float(descriptor[Name.Ascent]), float(descriptor[Name.Descent]))
            for descriptor in descriptors_of(document)
            if isinstance(name := descriptor.get(Name.FontName), Name)
        }


def ink_of(file: Path) -> dict[bytes, tuple[float, float]]:
    # How far the ink of each font's letters reaches above the baseline and below it, in
    # thousandths of the font's size, by the name PDFium gives the font.
    return {
        name: (
            FIGURES_PER_SIZE * max(reach.ink_top for reach in reaches.values()),
            FIGURES_PER_SIZE * min(reach.ink_bottom for reach in reaches.values()),
        )
        for name, reaches in letter_reaches(file, MEASURED_LETTERS).items()
    }


def unevenly_set(file: Path) -> dict[bytes, float]:
    # For each font that sets its letters in boxes of other heights, by its name, how far
    # apart the tops of the boxes, or their bottoms, lie at the most.
    spreads = {}
    for name, reaches in letter_reaches(file, MEASURED_LETTERS).items():
        tops = [reach.box_top for reach in reaches.values()]
        bottoms = [reach.box_bottom for reach in reaches.values()]
        spreads[name] = max(max(tops) - min(tops), max(bottoms) - min(bottoms))
    return {name: spread for name, spread in spreads.items() if spread > ONE_HEIGHT}


@pytest.mark.parametrize("sample", SAMPLES)
def test_a_pdf_whose_fonts_set_their_letters_in_one_height_is_handed_as_it_is(
    sample: str, tmp_path: Path
) -> None:
    handed = measured_file(sample_file(sample), tmp_path / COPY)
    assert handed == sample_file(sample), (
        f"{sample}, whose fonts say how far their letters reach, was handed as a copy"
    )
    assert not (tmp_path / COPY).exists(), f"a copy of {sample} was written though none was handed"


# A font that says its letters reach neither above the baseline nor below it says nothing that
# PDFium sets their boxes by, so the fonts are made to say they reach a little way up.
@settings(max_examples=50, deadline=None)
@given(
    data=st.data(),
    sample=st.sampled_from(LATIN_SAMPLES),
    ascent=st.integers(1, 500),
    descent=st.integers(-30, 0),
)
def test_fonts_that_say_their_letters_reach_less_far_are_made_to_say_how_far_they_do(
    data: st.DataObject, sample: str, ascent: int, descent: int
) -> None:
    with tempfile.TemporaryDirectory() as folder:
        # The fonts are chosen in the order the file holds their descriptors, which a copy
        # need not keep. The samples name no two fonts alike but for their tags.
        sound = figures_of(sample_file(sample))
        names = [*sound]
        chosen = data.draw(st.sets(st.integers(0, len(names) - 1), min_size=1), label="fonts")
        broken = misstated(sample_file(sample), ascent, descent, chosen, Path(folder) / MISSTATED)
        before = unevenly_set(broken)
        assert before, f"no font of {sample} said to reach {ascent} and {descent} sets unevenly"
        handed = measured_file(broken, Path(folder) / COPY)
        assert handed != broken, f"{sample}, whose fonts set unevenly {before}, was handed as it is"
        after = unevenly_set(handed)
        assert not after, f"in the copy of {sample}, fonts still set unevenly: {after}"
        said = figures_of(handed)
        ink = {unsubset(name): reach for name, reach in ink_of(handed).items()}
        for name in (names[place] for place in chosen):
            (ascent_said, descent_said), (top, bottom) = said[name], ink[unsubset(name)]
            assert abs(ascent_said - top) <= SAID_WITHIN, (
                f"in the copy of {sample}, {name!r} says its letters reach {ascent_said} above "
                f"the baseline, and their ink reaches {top}"
            )
            assert abs(descent_said - bottom) <= SAID_WITHIN, (
                f"in the copy of {sample}, {name!r} says its letters reach {descent_said} below "
                f"the baseline, and their ink reaches {bottom}"
            )
        changed = {
            name
            for place, name in enumerate(names)
            if place not in chosen and said[name] != sound[name]
        }
        assert not changed, f"in the copy of {sample}, fonts that said the truth changed: {changed}"
        assert stored_file(handed).pages == stored_file(sample_file(sample)).pages, (
            f"the copy of {sample} holds other text than the file"
        )


def test_a_font_named_in_bytes_that_are_not_utf8_is_mended_as_any_other(tmp_path: Path) -> None:
    broken = all_misstated(sample_file(COMPOSITE_SAMPLE), tmp_path / MISSTATED)
    named = renamed(broken, b"DAAAAA+LiberationMono", SHIFT_JIS_NAME, tmp_path / RENAMED)
    assert SHIFT_JIS_NAME in unevenly_set(named), (
        "the font named in Shift JIS does not set unevenly"
    )
    handed = measured_file(named, tmp_path / COPY)
    after = unevenly_set(handed)
    assert not after, f"in the copy, fonts still set unevenly: {after}"
    said = figures_of(handed)[SHIFT_JIS_NAME]
    assert said != (XCHARTER_ASCENT, XCHARTER_DESCENT), "the font named in Shift JIS was not mended"


def test_subsets_of_one_font_that_pdfium_tells_apart_are_mended_apart(tmp_path: Path) -> None:
    named = renamed(
        sample_file(COMPOSITE_SAMPLE), SECOND_SUBSET_NAMED, SECOND_SUBSET, tmp_path / RENAMED
    )
    sound = figures_of(named)
    first = {[*sound].index(FIRST_SUBSET)}
    broken = misstated(named, XCHARTER_ASCENT, XCHARTER_DESCENT, first, tmp_path / MISSTATED)
    before = unevenly_set(broken)
    assert FIRST_SUBSET in before and SECOND_SUBSET not in before, (
        f"the subsets do not set as made: unevenly {before}"
    )
    handed = measured_file(broken, tmp_path / COPY)
    after = unevenly_set(handed)
    assert not after, f"in the copy, fonts still set unevenly: {after}"
    said = figures_of(handed)
    assert said[SECOND_SUBSET] == sound[SECOND_SUBSET], (
        f"the subset that said the truth was changed to {said[SECOND_SUBSET]}"
    )


def test_a_pdf_whose_letters_hang_below_their_baselines_is_handed_as_it_is(
    tmp_path: Path,
) -> None:
    broken = all_misstated(sample_file(COMPOSITE_SAMPLE), tmp_path / MISSTATED)
    hanging = upside_down(broken, tmp_path / "upside-down.pdf")
    before = unevenly_set(hanging)
    assert before, "no font of the page drawn upside down sets unevenly"
    deepest = {name: bottom for name, (_, bottom) in ink_of(hanging).items()}
    shallow = {name: bottom for name, bottom in deepest.items() if bottom >= DEEPEST_LETTER}
    assert not shallow, f"letters of the page drawn upside down do not hang: {shallow}"
    assert measured_file(hanging, tmp_path / COPY) == hanging, (
        f"fonts whose letters hang below the line as far as {deepest} were mended"
    )


def test_a_pdf_whose_letters_a_font_of_its_own_procedures_draws_is_handed_as_it_is(
    tmp_path: Path,
) -> None:
    file = boxes_file(tmp_path / "boxes.pdf")
    assert BOX_WORD in stored_file(file).pages[0].text, "the page made for the test shows no word"
    measured = letter_reaches(file, MEASURED_LETTERS)
    assert not measured, f"letters of a font the PDF draws itself were measured: {measured}"
    assert measured_file(file, tmp_path / COPY) == file, (
        "a PDF whose only font is drawn by its own procedures was handed as a copy"
    )


reach_parts = st.floats(-1.5, 1.5, allow_nan=False)


@given(reaches=st.lists(st.tuples(reach_parts, reach_parts), min_size=1, max_size=8))
def test_letters_of_text_stand_on_the_baseline_and_hang_no_further_below_than_half_their_size(
    reaches: list[tuple[float, float]],
) -> None:
    letters = [
        LetterReach(ink_top=top, ink_bottom=bottom, box_top=top, box_bottom=bottom)
        for top, bottom in reaches
    ]
    lowest = min(bottom for _, bottom in reaches)
    highest = max(top for top, _ in reaches)
    stand = DEEPEST_LETTER <= lowest <= 0 < highest
    assert like_text(letters) == stand, (
        f"letters reaching {highest} above the baseline and {lowest} below it were "
        f"{'not ' if stand else ''}taken for letters of text"
    )


@pytest.mark.mineru
@pytest.mark.parametrize("sample", ["marks_digital", "turned_digital"])
def test_a_pdf_whose_fonts_say_their_letters_reach_less_far_reads_as_the_reference(
    sample: str, pdf_engine: Path, tmp_path: Path
) -> None:
    broken = all_misstated(sample_file(sample), tmp_path / f"{sample}.pdf")
    folder = tmp_path / "work"
    folder.mkdir()
    extracted = pdf.extract_pdf(broken, sample, pdf_engine, folder)
    expected = json.loads((PDF_DATA / "readings" / f"{sample}.json").read_text("utf-8"))
    read = as_stored(extracted)
    assert read == expected, (
        f"{sample}, with fonts that say their letters reach as XCharter says, is read otherwise "
        f"than {REFERENCE} read it: {differences(expected, read)}"
    )
