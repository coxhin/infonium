import json
import math
import tempfile
from pathlib import Path

import pikepdf
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pikepdf import Name

from infonium import pdf
from infonium.font_metrics import FIGURES_PER_SIZE, MEASURED_LETTERS, measured_file, unsubset
from infonium.pdfium import letter_reaches, stored_file
from tests.pdf_engine_standin import PDF_DATA
from tests.test_pdf import REFERENCE, SAMPLES, as_stored, differences

# The samples whose text layer is written in Latin letters, in fonts that say how far their
# letters reach.
LATIN_SAMPLES = ["marks_digital", "mixed_layers", "turned_digital"]
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
# The ink of a font's letters may reach a little beyond what the font says of them: in the fonts
# tried that say it truly, by up to 15 thousandths of their size, and in the samples by 11.
BEYOND_SAID = 20
COPY = "measured.pdf"
MISSTATED = "misstated.pdf"


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


def figures_of(file: Path) -> dict[str, tuple[float, float]]:
    # What each font of a file says of how far its letters reach, by the font's name without
    # a tag, in the order the file holds the fonts' descriptors.
    with pikepdf.open(file) as document:
        return {
            unsubset(str(descriptor[Name.FontName]).removeprefix("/")): (
                float(descriptor[Name.Ascent]),
                float(descriptor[Name.Descent]),
            )
            for descriptor in descriptors_of(document)
        }


def understated(file: Path) -> dict[str, tuple[float, float]]:
    # Each font of a file that says its letters reach less far above or below the baseline
    # than their ink does, by its name, with how far it says they reach.
    said = figures_of(file)
    found = {}
    for name, reaches in letter_reaches(file, MEASURED_LETTERS).items():
        ascent, descent = said.get(unsubset(name), (math.inf, -math.inf))
        top = FIGURES_PER_SIZE * max(reach.ink_top for reach in reaches.values())
        bottom = FIGURES_PER_SIZE * min(reach.ink_bottom for reach in reaches.values())
        if ascent < top - BEYOND_SAID or descent > bottom + BEYOND_SAID:
            found[unsubset(name)] = (ascent, descent)
    return found


def unevenly_set(file: Path) -> dict[str, float]:
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
        with pikepdf.open(sample_file(sample)) as document:
            count = len(descriptors_of(document))
        chosen = data.draw(st.sets(st.integers(0, count - 1), min_size=1), label="fonts")
        broken = misstated(sample_file(sample), ascent, descent, chosen, Path(folder) / MISSTATED)
        before = unevenly_set(broken)
        assert before, f"no font of {sample} said to reach {ascent} and {descent} sets unevenly"
        handed = measured_file(broken, Path(folder) / COPY)
        assert handed != broken, f"{sample}, whose fonts set unevenly {before}, was handed as it is"
        after = unevenly_set(handed)
        assert not after, f"in the copy of {sample}, fonts still set unevenly: {after}"
        short = understated(handed)
        assert not short, f"in the copy of {sample}, fonts say their letters reach less: {short}"
        # The fonts are chosen in the order the file holds their descriptors, which a copy
        # need not keep.
        sound, mended = figures_of(sample_file(sample)), figures_of(handed)
        untouched = {name for place, name in enumerate(sound) if place not in chosen}
        changed = {name for name in untouched if mended[name] != sound[name]}
        assert not changed, f"in the copy of {sample}, fonts that said the truth changed: {changed}"
        assert stored_file(handed).pages == stored_file(sample_file(sample)).pages, (
            f"the copy of {sample} holds other text than the file"
        )


@pytest.mark.mineru
@pytest.mark.parametrize("sample", ["marks_digital", "turned_digital"])
def test_a_pdf_whose_fonts_say_their_letters_reach_less_far_reads_as_the_reference(
    sample: str, pdf_engine: Path, tmp_path: Path
) -> None:
    with pikepdf.open(sample_file(sample)) as document:
        every = set(range(len(descriptors_of(document))))
    broken = misstated(
        sample_file(sample), XCHARTER_ASCENT, XCHARTER_DESCENT, every, tmp_path / f"{sample}.pdf"
    )
    folder = tmp_path / "work"
    folder.mkdir()
    extracted = pdf.extract_pdf(broken, sample, pdf_engine, folder)
    expected = json.loads((PDF_DATA / "readings" / f"{sample}.json").read_text("utf-8"))
    read = as_stored(extracted)
    assert read == expected, (
        f"{sample}, with fonts that say their letters reach as XCharter says, is read otherwise "
        f"than {REFERENCE} read it: {differences(expected, read)}"
    )
