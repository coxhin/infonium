import math
import threading
from collections import Counter
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field
from pypdfium2 import PdfDocument, PdfiumError, PdfTextPage, raw

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


def own_angles(layer: PdfTextPage) -> list[float]:
    # The angle each character of a page is drawn at, in radians and clockwise. A character
    # that PDFium added itself, such as the space between two words, is not drawn.
    return [
        raw.FPDFText_GetCharAngle(layer, index)
        for index in range(layer.count_chars())
        if not raw.FPDFText_IsGenerated(layer, index)
    ]


def righting_quarters(rotation: int, angles: list[float]) -> int:
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
    righting: int, left: float, bottom: float, right: float, top: float
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
        rotations = [raw.FPDFPage_GetRotation(page) for page in document]
        if not any(rotations):
            return UprightFile(file=file, turned=rotations)
        rightings = [
            righting_quarters(rotation, own_angles(page.get_textpage()) if rotation else [])
            for page, rotation in zip(document, rotations, strict=True)
        ]
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
