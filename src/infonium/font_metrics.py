import re
import string
from collections.abc import Iterator
from pathlib import Path

import pikepdf
from pikepdf import Name
from pydantic import BaseModel

from infonium.pdfium import LetterReach, letter_reaches

# A reader of a PDF's text layer, MinerU among them, asks PDFium for the box each character
# stands in. PDFium gives a box from the baseline up and down as far as the character's font
# says its letters reach, stretched to the character's ink where that reaches further, and the
# reader tells the line a character belongs to, and whether it is raised or lowered, by the
# middle of its box. A font that says its letters reach less far than they do leaves each
# letter in a box of its own outline: a letter without an ascender then stands lower than one
# with, and a letter with a descender lower still, so plain letters are taken for raised or
# lowered ones, and a letter whose middle falls too far from its line's is lost. A paper set
# in XCharter by pdfTeX says its letters reach no higher than the x-height and not below the
# baseline. Such a font is made to say how far its letters reach, as its letters show.

# The letters measured, those of the Latin alphabet. Letters of other scripts reach otherwise
# above and below their lines, and a font is not measured by them.
MEASURED_LETTERS = frozenset(string.ascii_letters)
# A font sets its letters in boxes of one height where the tops of the boxes, and their
# bottoms, lie no further apart than this part of the size the letters are drawn at. In the
# fonts tried that say how far their letters reach, they lay within 0.015 of it; in the
# paper's XCharter, a quarter apart.
EVEN = 0.1
# Letters of text stand on the baseline, and reach below it by no more than this part of
# their size. A font that draws its codes for letters as other shapes reaches further, as a
# mathematical font does whose large operators hang below the line, and is left as it is.
DEEPEST_LETTER = -0.5
# What a font says of its letters is in thousandths of its size.
FIGURES_PER_SIZE = 1000
# A font that a PDF embeds in part is named with a tag of six capitals and a plus sign before
# its own name. PDFium gives some names with the tag and some without.
SUBSET_TAG = re.compile(r"^[A-Z]{6}\+")
# The kinds of font that say how far their letters reach in thousandths of their size: all
# but a font drawn by the PDF's own procedures, whose figures are in units of its own. A
# composite font is described by the font it is made from, which is one of these.
FIGURED_FONTS = frozenset(
    {Name.Type1, Name.MMType1, Name.TrueType, Name.CIDFontType0, Name.CIDFontType2}
)
# pikepdf writes a name as the file does, after a solidus.
NAME_SOLIDUS = "/"


class FontMetrics(BaseModel):
    # How far a font says its letters reach above the baseline and below it, in thousandths
    # of its size.
    ascent: int
    descent: int


def unsubset(name: str) -> str:
    return SUBSET_TAG.sub("", name)


def uneven(reaches: list[LetterReach]) -> bool:
    # Whether a font sets its letters in boxes of other heights.
    tops = [reach.box_top for reach in reaches]
    bottoms = [reach.box_bottom for reach in reaches]
    return max(tops) - min(tops) > EVEN or max(bottoms) - min(bottoms) > EVEN


def text_letters(reaches: list[LetterReach]) -> bool:
    # Whether a font's letters stand on the baseline as letters of text do.
    bottom = min(reach.ink_bottom for reach in reaches)
    return DEEPEST_LETTER <= bottom <= 0 < max(reach.ink_top for reach in reaches)


def measured_metrics(reaches: list[LetterReach]) -> FontMetrics:
    # What a font is to say of its letters for them to stand in one box: from the top of the
    # highest of their ink to the bottom of the lowest.
    return FontMetrics(
        ascent=round(FIGURES_PER_SIZE * max(reach.ink_top for reach in reaches)),
        descent=round(FIGURES_PER_SIZE * min(reach.ink_bottom for reach in reaches)),
    )


def described_fonts(document: pikepdf.Pdf) -> Iterator[tuple[str, pikepdf.Dictionary]]:
    # Each font of a file that says how far its letters reach in thousandths of its size, by
    # its name without a tag, with the dictionary that says it.
    for font in document.objects:
        if isinstance(font, pikepdf.Dictionary):
            kind = font.get(Name.Subtype)
            name = font.get(Name.BaseFont)
            descriptor = font.get(Name.FontDescriptor)
            if (
                isinstance(kind, Name)
                and kind in FIGURED_FONTS
                and isinstance(name, Name)
                and isinstance(descriptor, pikepdf.Dictionary)
            ):
                yield unsubset(str(name).removeprefix(NAME_SOLIDUS)), descriptor


def measured_file(file: Path, copy: Path) -> Path:
    # A file whose fonts each set their letters of text in boxes of one height: the file
    # itself where they do, and otherwise a copy, written where it is asked for, in which each
    # font that does not says how far its letters reach as they show it.
    measured = {
        unsubset(name): [*reaches.values()]
        for name, reaches in letter_reaches(file, MEASURED_LETTERS).items()
    }
    mended = {
        name: measured_metrics(reaches)
        for name, reaches in measured.items()
        if uneven(reaches) and text_letters(reaches)
    }
    if not mended:
        return file
    with pikepdf.open(file) as document:
        described = [
            (descriptor, mended[name])
            for name, descriptor in described_fonts(document)
            if name in mended
        ]
        if not described:
            return file
        for descriptor, metrics in described:
            descriptor[Name.Ascent] = metrics.ascent
            descriptor[Name.Descent] = metrics.descent
        document.save(copy)
    return copy
