import threading
from pathlib import Path

from pydantic import BaseModel
from pypdfium2 import PdfDocument

from infonium.document import WORD_GAP

# PDFium may be used by one thread at a time, whatever the file. infonium calls it in this
# module and nowhere else, and every function here holds this turn for as long as it has a
# file open in PDFium, so no caller can let a second thread in.
PDFIUM_TURN = threading.Lock()
TITLE_ENTRY = "Title"


class StoredPage(BaseModel):
    # What a file says of one of its pages before the page is read: whether it has a text
    # layer, and whether it is stored turned, to be shown turned back.
    lettered: bool
    turned: bool


class StoredFile(BaseModel):
    title: str
    pages: list[StoredPage]


def stored_file(file: Path) -> StoredFile:
    with PDFIUM_TURN, PdfDocument(file) as document:
        return StoredFile(
            title=WORD_GAP.join(document.get_metadata_value(TITLE_ENTRY).split()),
            pages=[
                StoredPage(
                    lettered=page.get_textpage().count_chars() > 0,
                    turned=page.get_rotation() != 0,
                )
                for page in document
            ],
        )
