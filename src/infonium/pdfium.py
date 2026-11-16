import math
import threading
from collections import Counter
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel
from pypdfium2 import PdfDocument, raw

from infonium.document import WORD_GAP, PageNumber

# PDFium may be used by one thread at a time, whatever the file. infonium calls it in this
# module and nowhere else, and every function here holds this turn for as long as it has a
# file open in PDFium, so no caller can let a second thread in.
PDFIUM_TURN = threading.Lock()
TITLE_ENTRY = "Title"
# A file turns a page by quarters of a whole turn, and a character is taken to be drawn at
# the quarter nearest to its angle.
QUARTERS = 4
QUARTER_TURN = math.tau / QUARTERS


class Shown(StrEnum):
    # How a file asks for a page to be shown: as it is stored, or turned. Turning sets upright
    # a page that is stored turned, with its characters drawn on their side or upside down;
    # any other page it turns away, so that characters drawn upright are shown lying.
    AS_STORED = "as stored"
    TURNED_UPRIGHT = "turned upright"
    TURNED_AWAY = "turned away"


class StoredPage(BaseModel):
    # What a file says of one of its pages before the page is read: what its text layer holds,
    # nothing when it has none, and how the page is to be shown.
    text: str
    shown: Shown


class StoredFile(BaseModel):
    title: str
    pages: list[StoredPage]


def shown_as(rotation: int, angles: list[float]) -> Shown:
    # How a page is shown that its file turns clockwise by a rotation in degrees, and whose
    # characters are drawn at these angles, in radians and clockwise too. The page is stored
    # turned when more of its characters stand upright once it is turned than stand any
    # other way.
    if not rotation:
        return Shown.AS_STORED
    standing = Counter(
        round((math.radians(rotation) + angle) / QUARTER_TURN) % QUARTERS for angle in angles
    )
    match standing.most_common(1):
        case [(0, _)]:
            return Shown.TURNED_UPRIGHT
        case _:
            return Shown.TURNED_AWAY


def file_of_pages(file: Path, numbers: list[PageNumber], copy: Path) -> Path:
    # A file that holds these pages of a file, in this order, and no other page: the file
    # itself when they are all its pages, and otherwise a copy, written where it is asked for.
    with PDFIUM_TURN, PdfDocument(file) as document:
        if numbers == [*range(len(document))]:
            return file
        with PdfDocument.new() as copied:
            copied.import_pages(document, numbers)
            copied.save(copy)
        return copy


def stored_file(file: Path) -> StoredFile:
    with PDFIUM_TURN, PdfDocument(file) as document:
        pages: list[StoredPage] = []
        for page in document:
            layer = page.get_textpage()
            rotation = page.get_rotation()
            # Only a page that is turned is asked how its characters are drawn. A character
            # that PDFium added itself, such as the space between two words, is not drawn.
            drawn = range(layer.count_chars()) if rotation else range(0)
            angles = [
                raw.FPDFText_GetCharAngle(layer, index)
                for index in drawn
                if not raw.FPDFText_IsGenerated(layer, index)
            ]
            pages.append(StoredPage(text=layer.get_text_range(), shown=shown_as(rotation, angles)))
        return StoredFile(
            title=WORD_GAP.join(document.get_metadata_value(TITLE_ENTRY).split()),
            pages=pages,
        )
