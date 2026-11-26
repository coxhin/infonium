import ctypes
import math
import re
import sys
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from pypdfium2 import PdfBitmap, PdfDocument, PdfiumError, PdfTextPage, raw

from infonium.document import WORD_GAP

# PDFium may be used by one thread at a time, whatever the file. infonium calls it in this
# module and nowhere else, and every function here holds this lock for as long as it has a
# file open in PDFium, so no caller can let a second thread in.
PDFIUM_IN_USE = threading.Lock()
TITLE_ENTRY = "Title"
# A file turns a page clockwise by quarters of a whole turn, and a character is taken to be
# drawn at the quarter nearest to its angle.
WHOLE_TURN = 4
QUARTER = math.tau / WHOLE_TURN
Quarters = Annotated[int, Field(ge=0, lt=WHOLE_TURN)]
# What turns the content of a page clockwise by one, two and three quarters. Whole numbers
# leave a character that was drawn a quarter turn off standing at exactly no angle, which the
# cosine of a quarter turn, worked out, does not.
CLOCKWISE = {1: (0, -1, 1, 0), 2: (-1, 0, 0, -1), 3: (0, 1, -1, 0)}
UNFLATTENED = "PDFium could not draw the annotations of a page into its content"
# How far a letter reaches is told to a thousandth of the size it is drawn at, so that a
# letter drawn alike at several sizes is counted as one.
REACH_DIGITS = 3
# A character beyond the first 65,536 of Unicode, which UTF-16 writes in two units.
FIRST_WIDE = 0x10000
WIDE_CHARACTER = re.compile(f"[{chr(FIRST_WIDE)}-{chr(sys.maxunicode)}]")
# A region of a page is drawn as pypdfium2 draws a whole page unless told otherwise: on white,
# with the page's annotations, three bytes a pixel.
PAPER = (255, 255, 255, 255)
# What drawing a region can raise: PdfiumError where PDFium fails to fill the drawing, and
# MemoryError where the memory for its pixels cannot be had.
DRAWING_FAILURES = (PdfiumError, MemoryError)

# The left, top, right and bottom edges of a region of a page, as parts of the page's width and
# height, measured from the top left corner of the page as its file shows it.
Region = tuple[float, float, float, float]


class StoredPage(BaseModel):
    # What a file says of one of its pages before the page is read: what its text layer holds,
    # nothing when it has none, and whether its characters stand straight, which is to say
    # that most of them stand at exactly no angle.
    text: str
    straight: bool


class StoredFile(BaseModel):
    title: str
    pages: list[StoredPage]


class UprightFile(BaseModel):
    # A file that turns none of its pages, and for each page by how many quarters, clockwise,
    # the file it was made from shows the page turned from the way it stands here.
    file: Path
    turned: list[Quarters]


class LetterReach(BaseModel):
    # How far a letter drawn in a font reaches above its baseline and below it, as parts of
    # the size it is drawn at: the outline of its ink, and the box PDFium says it stands in.
    model_config = ConfigDict(frozen=True)

    ink_top: float
    ink_bottom: float
    box_top: float
    box_bottom: float


def own_angles(layer: PdfTextPage) -> list[float]:
    # The angle each character of a page is drawn at, in radians and clockwise. A character
    # that PDFium added itself, such as the space between two words, is not drawn.
    return [
        raw.FPDFText_GetCharAngle(layer, index)
        for index in range(layer.count_chars())
        if not raw.FPDFText_IsGenerated(layer, index)
    ]


def righting_quarters(rotation: Quarters, angles: list[float]) -> Quarters:
    # By how many quarters the stored content of a page must be turned for its characters to
    # stand upright, when its file shows the page turned by its rotation, in quarters, and
    # draws its characters at these angles. A page stored turned needs the file's own turn. A
    # page that is only shown turned needs none: more of its characters stand upright as it is
    # stored than once it is turned. Characters that stand neither way, as the labels along
    # the side of a figure do, tell nothing, and where none tells, the page is taken as its
    # file shows it.
    standing = Counter((rotation + round(angle / QUARTER)) % WHOLE_TURN for angle in angles)
    return 0 if standing[rotation] > standing[0] else rotation


def upright_matrix(
    righting: Quarters, left: float, bottom: float, right: float, top: float
) -> tuple[float, float, float, float, float, float]:
    # The matrix that turns what lies between these edges of a page clockwise by one to three
    # quarters, and sets the lower left corner of what it has turned at the origin.
    lands = {1: (-bottom, right), 2: (right, top), 3: (top, -left)}
    return (*CLOCKWISE[righting], *lands[righting])


def upright_file(file: Path, copy: Path) -> UprightFile:
    # A file for a reader that misreads a turned page: the file itself when it turns no page,
    # and otherwise a copy, written where it is asked for, in which every page has a rotation
    # of 0. The content of a page stored turned is turned in the copy, and the page is given
    # the width and height it is shown with, so that the copy shows the page as the file does.
    # Its annotations are drawn into its content before it is turned. PDFium draws them only
    # within the box the page holds itself, and within 612 by 792 points on a page that holds
    # none, so a page that inherits its box is first given the one it is shown with.
    # A page that is only shown turned keeps its content, and stands in the copy as it is
    # stored. A file may list one page more than once. Every listing is decided from the
    # file as it is given, before any page is edited. A page is edited where it is first
    # listed, and a later listing, which finds it without the rotation the file gave it,
    # leaves it as it is.
    with PDFIUM_IN_USE, PdfDocument(file) as document:
        rotations: list[Quarters] = []
        rightings: list[Quarters] = []
        for page in document:
            rotation = raw.FPDFPage_GetRotation(page)
            rotations.append(rotation)
            rightings.append(
                righting_quarters(rotation, own_angles(page.get_textpage()) if rotation else [])
            )
        if not any(rotations):
            return UprightFile(file=file, turned=rotations)
        for page, rotation, righting in zip(document, rotations, rightings, strict=True):
            if righting and raw.FPDFPage_GetRotation(page) == rotation:
                left, bottom, right, top = page.get_bbox()
                if (
                    page.get_mediabox(fallback_ok=False) is None
                    and page.get_cropbox(fallback_ok=False) is None
                ):
                    page.set_mediabox(left, bottom, right, top)
                # PDFium flattens only an appearance that is written in the file, and writes
                # the one it would draw for an annotation without any when it lists the
                # page's annotations, as it does to move them: here by nothing.
                raw.FPDFPage_TransformAnnots(page, 1, 0, 0, 1, 0, 0)
                if raw.FPDFPage_Flatten(page, raw.FLAT_NORMALDISPLAY) == raw.FLATTEN_FAIL:
                    raise PdfiumError(UNFLATTENED)
                matrix = upright_matrix(righting, left, bottom, right, top)
                # PDFium says it transformed nothing only of a page without content.
                raw.FPDFPage_TransFormWithClip(page, raw.FS_MATRIX(*matrix), None)
                # An odd number of quarters lays the page on its side.
                width, height = (
                    (top - bottom, right - left) if righting % 2 else (right - left, top - bottom)
                )
                page.set_mediabox(0, 0, width, height)
                page.set_cropbox(0, 0, width, height)
            page.set_rotation(0)
        document.save(copy)
        return UprightFile(
            file=copy,
            turned=[
                (rotation - righting) % WHOLE_TURN
                for rotation, righting in zip(rotations, rightings, strict=True)
            ],
        )


def stored_file(file: Path) -> StoredFile:
    with PDFIUM_IN_USE, PdfDocument(file) as document:
        pages: list[StoredPage] = []
        for page in document:
            layer = page.get_textpage()
            angles = own_angles(layer)
            pages.append(
                StoredPage(
                    text=layer.get_text_range(), straight=2 * angles.count(0.0) > len(angles)
                )
            )
        return StoredFile(
            title=WORD_GAP.join(document.get_metadata_value(TITLE_ENTRY).split()),
            pages=pages,
        )


def draws_by_program(font: object) -> bool:
    # Whether a font draws its letters by a program, as every font does but one that the PDF
    # draws by procedures of its own, for which PDFium holds none. A font that the file names
    # without holding it is drawn by the program PDFium puts in its place.
    size = ctypes.c_size_t()
    raw.FPDFFont_GetFontData(font, None, 0, size)
    return size.value > 0


def letter_reaches(file: Path, letters: frozenset[str]) -> dict[bytes, dict[str, LetterReach]]:
    # For each font that a file draws any of these letters in, by the name PDFium gives it, in
    # the bytes the file writes it in, how far each of those letters reaches as the file most
    # often draws it. PDFium gives a font the name the file gives it, without the tag of a
    # subset for some kinds of font. A letter is measured where it stands at exactly no angle,
    # as only there is it placed by its box when the text layer is read, and where it is drawn
    # at some size. A font that the file draws by procedures of its own (Type 3) is not
    # measured: it is not told how far its letters reach in thousandths of its size, and a
    # font of another kind may go by its name.
    seen: dict[bytes, dict[str, Counter[LetterReach]]] = {}
    ink_left, ink_right, ink_bottom, ink_top, x, y = (ctypes.c_double() for _ in range(6))
    box = raw.FS_RECTF()
    matrix = raw.FS_MATRIX()
    with PDFIUM_IN_USE, PdfDocument(file) as document:
        for page in document:
            layer = page.get_textpage()
            for index in range(layer.count_chars()):
                letter = chr(raw.FPDFText_GetUnicode(layer, index))
                if letter not in letters or raw.FPDFText_GetCharAngle(layer, index) != 0:
                    continue
                font = raw.FPDFTextObj_GetFont(raw.FPDFText_GetTextObject(layer, index))
                if not draws_by_program(font):
                    continue
                # The size of the font as the page shows it: the page may draw the text
                # scaled, as a figure placed smaller than it was made.
                raw.FPDFText_GetMatrix(layer, index, matrix)
                size = raw.FPDFText_GetFontSize(layer, index) * abs(matrix.d)
                if not size:
                    continue
                raw.FPDFText_GetCharBox(layer, index, ink_left, ink_right, ink_bottom, ink_top)
                raw.FPDFText_GetLooseCharBox(layer, index, box)
                raw.FPDFText_GetCharOrigin(layer, index, x, y)
                reach = LetterReach(
                    ink_top=round((ink_top.value - y.value) / size, REACH_DIGITS),
                    ink_bottom=round((ink_bottom.value - y.value) / size, REACH_DIGITS),
                    box_top=round((box.top - y.value) / size, REACH_DIGITS),
                    box_bottom=round((box.bottom - y.value) / size, REACH_DIGITS),
                )
                length = raw.FPDFFont_GetBaseFontName(font, None, 0)
                name = ctypes.create_string_buffer(length)
                raw.FPDFFont_GetBaseFontName(font, name, length)
                by_letter = seen.setdefault(name.value, {})
                by_letter.setdefault(letter, Counter())[reach] += 1
    return {
        name: {letter: counted.most_common(1)[0][0] for letter, counted in by_letter.items()}
        for name, by_letter in seen.items()
    }


def holds_wide_characters(file: Path) -> bool:
    # Whether the text layer of a file holds a character beyond the first 65,536 of Unicode.
    with PDFIUM_IN_USE, PdfDocument(file) as document:
        return any(WIDE_CHARACTER.search(page.get_textpage().get_text_range()) for page in document)


class OpenPage:
    # A page of a file, open in PDFium, whose regions are drawn as the file shows the page:
    # turned the way it says, and with its annotations. Its width and height are those it is
    # shown with, in points. A region is drawn alone, as it stands in a drawing of the whole
    # page, which has the page's sides in points times the scale, rounded up, for its sides.
    def __init__(self, page: Any) -> None:
        self.page = page
        width, height = page.get_size()
        self.width: float = width
        self.height: float = height

    def whole(self, scale: float) -> tuple[int, int]:
        return math.ceil(self.width * scale), math.ceil(self.height * scale)

    def pixels(self, region: Region, scale: float) -> tuple[int, int, int, int]:
        # The edges of a region in a drawing of the whole page at so many pixels a point.
        across, down = self.whole(scale)
        left, top, right, bottom = region
        return round(left * across), round(top * down), round(right * across), round(bottom * down)

    def drawn(self, region: Region, scale: float) -> Image.Image:
        across, down = self.whole(scale)
        left, top, right, bottom = self.pixels(region, scale)
        width, height = right - left, bottom - top
        bitmap = PdfBitmap.new_native(width, height, raw.FPDFBitmap_BGR)
        bitmap.fill_rect(PAPER, 0, 0, width, height)
        raw.FPDF_RenderPageBitmap(bitmap, self.page, -left, -top, across, down, 0, raw.FPDF_ANNOT)
        drawing: Image.Image = bitmap.to_pil()
        return drawing


@contextmanager
def open_page(file: Path, number: int) -> Iterator[OpenPage]:
    # A page of a file, open for as long as its caller draws regions of it, which holds PDFium
    # as long.
    with PDFIUM_IN_USE, PdfDocument(file) as document:
        yield OpenPage(document[number])
