import os
import signal
import subprocess
import unicodedata
from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, Field

from infonium.document import HeadingLevel, PageNumber
from infonium.pdfium import Shown, StoredFile, file_of_pages

# MinerU parses a PDF, run as a program of its own. Its hybrid backend reads tables,
# formulas and listings best, and gives a box for every line as long as it takes the words
# from the file's text layer. When it has to read the pages as pictures it gives one box for
# a whole block, so such a file is read by the pipeline backend, which keeps the lines.
ENGINE_INPUT = "-p"
ENGINE_OUTPUT = "-o"
ENGINE_BACKEND = "-b"
ENGINE_METHOD = "-m"
ENGINE_FIRST_PAGE = "-s"
ENGINE_LAST_PAGE = "-e"
ENGINE_RESULTS = "*_middle.json"
ENGINE_LOG = "{backend}.log"
# The file in the folder that holds the pages read as pictures, when they are not all the
# pages of the PDF.
PICTURED_PAGES = "pictured.pdf"
# The version of the engine this module is written to, and the key under which the results
# name the version that wrote them. Two things the module relies on are this version's own
# and show in no shape of the results: it numbers the pages of a run from 0 wherever the run
# starts, and it reads from a picture the letters listed below. Results of another version
# are refused where they enter, so that a later engine fails by name and loses no page
# unnoticed.
EngineVersion = Literal["3.4.2"]
VERSION_KEY = "_version_name"
# The keys under which the results say which backend wrote them, and whether the hybrid
# backend judged the text layer unfit and read the pages as pictures.
BACKEND_KEY = "_backend"
READ_AS_PICTURES_KEY = "_ocr_enable"
# The key under which the results give a page's number, and the name of the field that holds
# it in a parsed page.
PAGE_NUMBER_KEY = "page_idx"
# The variable from which the engine takes how long it waits for its own results. When that
# time has passed it gives up, stops its server and leaves, whoever started it.
ENGINE_PATIENCE = "MINERU_TASK_RESULT_TIMEOUT_SECONDS"
# The variable from which a program takes where to keep its temporary files. The engine is
# given the folder, so that whatever it leaves behind is cleared with the folder.
TEMPORARY_FILES = "TMPDIR"
# Measured with one engine on an RTX 5090: a run takes 10 to 15 seconds to start and stop its
# server, and between 0.4 and 8.5 seconds a page. For its work the engine is given a minute
# and more than twice the slowest of those a page, and the call a minute more around that, in
# which the engine starts, and stops itself once it has given up.
PAGE_SECONDS = 20
SPARE_SECONDS = 60
SILENT_ENGINE = "the PDF engine left with status {status} and wrote nothing"
STOPPED_ENGINE = "the PDF engine was stopped by {signal}"
CONTROL_CHARACTER = "Cc"
# The control characters that lay text out; a listing keeps them.
LAYOUT_CHARACTERS = "\n\t"
# The letters the engine reads from a picture when it is told no language, by code point:
# Latin as far as the phonetic alphabet, the Greek capitals and small letters that formulas
# use, without accents and without the final sigma, kana, and Chinese characters. They are
# read from the dictionary of the recognition model the engine then uses, ppocrv6_dict.txt
# in the version named above. It holds every one of these Latin and Greek letters, all kana
# but three rare ones, and the three quarters of the Chinese characters that are in common
# use. It holds no letter of Cyrillic, Hangul, Arabic, Hebrew, Devanagari or Thai, and but
# two of the letters with tone marks that Vietnamese adds to Latin.
PICTURE_LETTERS = (
    range(0x0041, 0x02B0),
    range(0x0391, 0x03AA),
    range(0x03B1, 0x03C2),
    range(0x03C3, 0x03CA),
    range(0x3041, 0x3100),
    range(0x4E00, 0xA000),
)
# The normal form in which a letter is the plain one, however it was set: a ligature is its
# letters, and a letter of full width or in the style of a formula is the common one.
PLAIN_FORM: Final = "NFKC"
MODIFIER_LETTER = "Lm"


class Backend(StrEnum):
    HYBRID = "hybrid-engine"
    PIPELINE = "pipeline"


class Method(StrEnum):
    AUTO = "auto"
    OCR = "ocr"


class ParsedBy(StrEnum):
    # The backends as their results name them.
    HYBRID = "hybrid"
    PIPELINE = "pipeline"


class SpanType(StrEnum):
    TEXT = "text"
    INLINE_FORMULA = "inline_equation"
    DISPLAY_FORMULA = "interline_equation"
    TABLE = "table"


class RegionType(StrEnum):
    # The engine calls these blocks; here they are regions, so that a block stays what a
    # document is made of.
    TEXT = "text"
    TITLE = "title"
    ABSTRACT = "abstract"
    CONTENTS = "index"
    LIST = "list"
    REFERENCE = "ref_text"
    DISPLAY_FORMULA = "interline_equation"
    IMAGE = "image"
    IMAGE_BODY = "image_body"
    IMAGE_CAPTION = "image_caption"
    IMAGE_FOOTNOTE = "image_footnote"
    CHART = "chart"
    CHART_BODY = "chart_body"
    CHART_CAPTION = "chart_caption"
    CHART_FOOTNOTE = "chart_footnote"
    TABLE = "table"
    TABLE_BODY = "table_body"
    TABLE_CAPTION = "table_caption"
    TABLE_FOOTNOTE = "table_footnote"
    CODE = "code"
    CODE_BODY = "code_body"
    CODE_CAPTION = "code_caption"
    CODE_FOOTNOTE = "code_footnote"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    ASIDE = "aside_text"
    PAGE_FOOTNOTE = "page_footnote"


def legible(text: str) -> str:
    # A text layer may map a glyph to a control character, which is no part of any word.
    return "".join(
        character
        for character in text
        if unicodedata.category(character) != CONTROL_CHARACTER or character in LAYOUT_CHARACTERS
    )


Legible = Annotated[str, AfterValidator(legible)]
Points = Annotated[float, Field(allow_inf_nan=False)]
# Left, top, right and bottom, measured from the top left corner of the page as it is shown.
Box = tuple[Points, Points, Points, Points]


class TextSpan(BaseModel):
    # Words as they are written, with what is set above or below the line wrapped in <sup>
    # and <sub>.
    type: Literal[SpanType.TEXT]
    content: Legible


class FormulaSpan(BaseModel):
    type: Literal[SpanType.INLINE_FORMULA, SpanType.DISPLAY_FORMULA]
    content: Legible


class TableSpan(BaseModel):
    type: Literal[SpanType.TABLE]
    # Absent when the engine could not read the table.
    html: Legible | None = None


class Line(BaseModel):
    bbox: Box
    spans: list[Annotated[TextSpan | FormulaSpan, Field(discriminator="type")]]


class TableLine(BaseModel):
    spans: list[TableSpan]


class Text(BaseModel):
    type: Literal[RegionType.TEXT]
    index: int
    lines: list[Line]


class Title(BaseModel):
    type: Literal[RegionType.TITLE]
    level: HeadingLevel
    lines: list[Line]


class OwnParagraph(BaseModel):
    # A region whose words make a paragraph of their own wherever they stand.
    type: Literal[
        RegionType.ABSTRACT,
        RegionType.CONTENTS,
        RegionType.REFERENCE,
        RegionType.DISPLAY_FORMULA,
        RegionType.IMAGE_CAPTION,
        RegionType.IMAGE_FOOTNOTE,
        RegionType.CHART_CAPTION,
        RegionType.CHART_FOOTNOTE,
        RegionType.TABLE_CAPTION,
        RegionType.TABLE_FOOTNOTE,
        RegionType.CODE_CAPTION,
        RegionType.CODE_FOOTNOTE,
        RegionType.PAGE_FOOTNOTE,
    ]
    bbox: Box
    lines: list[Line]


class PictureBody(BaseModel):
    type: Literal[RegionType.IMAGE_BODY, RegionType.CHART_BODY]


class TableBody(BaseModel):
    type: Literal[RegionType.TABLE_BODY]
    bbox: Box
    lines: list[TableLine]


class CodeBody(BaseModel):
    type: Literal[RegionType.CODE_BODY]
    lines: list[Line]


Member = Annotated[OwnParagraph | PictureBody | TableBody | CodeBody, Field(discriminator="type")]


class Group(BaseModel):
    # A picture, a chart, a table or a listing, together with its captions and notes.
    type: Literal[RegionType.IMAGE, RegionType.CHART, RegionType.TABLE, RegionType.CODE]
    blocks: list[Member]


Region = Annotated[Text | Title | OwnParagraph | Group, Field(discriminator="type")]


class Furniture(BaseModel):
    type: Literal[RegionType.HEADER, RegionType.FOOTER, RegionType.PAGE_NUMBER, RegionType.ASIDE]


Margin = Annotated[Furniture | OwnParagraph, Field(discriminator="type")]


class MergedRegion(BaseModel):
    # A region after the engine has joined what runs on: the lines of a text that goes on
    # from the text before it, in the next column or on the next page, are moved there.
    type: RegionType
    index: int
    lines_deleted: bool = False


class ParsedPage(BaseModel):
    # The regions are read as they were found on their page, where every line still has its
    # own page; the merged ones only tell which texts go on from the text before them.
    page_idx: PageNumber
    page_size: tuple[Points, Points]
    preproc_blocks: list[Region]
    para_blocks: list[MergedRegion]
    discarded_blocks: list[Margin]

    def continued(self) -> set[int]:
        return {
            merged.index
            for merged in self.para_blocks
            if merged.type == RegionType.TEXT and merged.lines_deleted
        }


class ParsedFile(BaseModel):
    # The results of the pipeline backend, which reads pages as pictures.
    backend: Literal[ParsedBy.PIPELINE] = Field(alias=BACKEND_KEY)
    version: EngineVersion = Field(alias=VERSION_KEY)
    pdf_info: list[ParsedPage]


class HybridParsedFile(BaseModel):
    # The results of the hybrid backend, which reads a text layer unless it judges it unfit.
    backend: Literal[ParsedBy.HYBRID] = Field(alias=BACKEND_KEY)
    version: EngineVersion = Field(alias=VERSION_KEY)
    read_as_pictures: bool = Field(alias=READ_AS_PICTURES_KEY)
    pdf_info: list[ParsedPage]


def word_lines(region: Region | Member | Margin) -> list[Line]:
    match region:
        case Text() | Title() | OwnParagraph():
            return region.lines
        case Group():
            return [line for member in region.blocks for line in word_lines(member)]
        case _:
            return []


class FileToParse(BaseModel):
    # A PDF, the engine that parses it, and the folder in which the engine may write.
    file: Path
    engine: Path
    folder: Path


class UnparsedFile(Exception):
    # The engine ended without results. The message is the last line it wrote, or how it
    # ended when it wrote nothing or was stopped by a signal.
    pass


def engine_results(to_parse: FileToParse, backend: Backend, method: Method, pages: range) -> bytes:
    # One run of the engine over some pages of the file. What the engine writes goes to a
    # file in the folder and not to the caller, so an engine whose caller has died can still
    # write: it finishes its work and leaves by itself.
    patience = SPARE_SECONDS + PAGE_SECONDS * len(pages)
    found = to_parse.folder / backend
    where: list[str | Path] = [ENGINE_INPUT, to_parse.file, ENGINE_OUTPUT, found]
    how: list[str | Path] = [ENGINE_BACKEND, backend, ENGINE_METHOD, method]
    which = [ENGINE_FIRST_PAGE, str(pages.start), ENGINE_LAST_PAGE, str(pages.stop - 1)]
    told = {ENGINE_PATIENCE: str(patience), TEMPORARY_FILES: str(to_parse.folder)}
    log = to_parse.folder / ENGINE_LOG.format(backend=backend)
    with log.open("wb") as logged:
        ended = subprocess.run(
            [to_parse.engine, *where, *how, *which],
            check=False,
            stdout=logged,
            stderr=subprocess.STDOUT,
            env={**os.environ, **told},
            timeout=patience + SPARE_SECONDS,
        )
    results = [*found.rglob(ENGINE_RESULTS)]
    if ended.returncode < 0:
        # A signal stops the engine in the middle of its work, where the last line it wrote
        # says only how far it had come. A program stopped so leaves with the signal's number
        # as its status, below zero.
        raise UnparsedFile(STOPPED_ENGINE.format(signal=signal.Signals(-ended.returncode).name))
    if ended.returncode or not results:
        stripped = [line.strip() for line in log.read_text("utf-8", "replace").splitlines()]
        log_lines = [line for line in stripped if line]
        raise UnparsedFile(
            log_lines[-1] if log_lines else SILENT_ENGINE.format(status=ended.returncode)
        )
    [kept] = results
    return kept.read_bytes()


def numbered_in_file(parsed: list[ParsedPage], numbers: Sequence[PageNumber]) -> list[ParsedPage]:
    # The engine counts the pages of a run from 0, whichever pages of the file they are.
    return [page.model_copy(update={PAGE_NUMBER_KEY: numbers[page.page_idx]}) for page in parsed]


def picture_pages(to_parse: FileToParse, numbers: list[PageNumber]) -> list[ParsedPage]:
    # These pages of a file as the engine reads them from pictures. A run reads every page
    # from its first to its last, so the engine is given a file that holds no page it need
    # not read: the pages asked for, each after the page that comes before it in the file,
    # from which the engine tells whether the first text of a page goes on from there.
    given = sorted({*numbers, *(number - 1 for number in numbers if number)})
    to_read = FileToParse(
        file=file_of_pages(to_parse.file, given, to_parse.folder / PICTURED_PAGES),
        engine=to_parse.engine,
        folder=to_parse.folder,
    )
    results = engine_results(to_read, Backend.PIPELINE, Method.OCR, range(len(given)))
    parsed = numbered_in_file(ParsedFile.model_validate_json(results).pdf_info, given)
    return [page for page in parsed if page.page_idx in numbers]


def lettered_pages(to_parse: FileToParse, pages: range) -> list[ParsedPage]:
    # The pages the engine read from their text layer: none, when it judged the layer unfit,
    # as it judges a scan's hidden one, and read the pages as pictures of its own accord.
    results = engine_results(to_parse, Backend.HYBRID, Method.AUTO, pages)
    hybrid = HybridParsedFile.model_validate_json(results)
    return [] if hybrid.read_as_pictures else numbered_in_file(hybrid.pdf_info, pages)


class ParsedPages(BaseModel):
    # A file's pages as the engine read them, in order, and the numbers of those it read as
    # pictures.
    pages: list[ParsedPage]
    pictures: frozenset[PageNumber]


def readable_from_pictures(text: str) -> bool:
    # Whether the engine reads from a picture every letter that a text holds. A letter counts
    # as the plain one it is a form of. A modifier letter, such as an accent or a length mark
    # set beside a letter, belongs to no script and counts for nothing.
    letters = [
        letter
        for letter in unicodedata.normalize(PLAIN_FORM, text)
        if letter.isalpha() and unicodedata.category(letter) != MODIFIER_LETTER
    ]
    return all(any(ord(letter) in block for block in PICTURE_LETTERS) for letter in letters)


def parsed_pages(to_parse: FileToParse, stored: StoredFile) -> ParsedPages:
    # How the engine reads each page of a file. It reads a page from the file's text layer
    # when the file has one, but misreads the layer of a page that is turned. A page stored
    # turned is read as a picture instead, which shows its characters upright, when the
    # engine reads every letter of the page from a picture. In any other script the picture
    # would lose the page's words, so the page stays with its layer, which holds them. So
    # does a page that is only shown turned: in a picture its characters lie, and the engine
    # reads those worse still. Every page of a file without a text layer, or whose layer the
    # engine judges unfit, is read as a picture too. The engine runs once for each way of
    # reading: over the layer's pages from the first of them to the last, which keeps every
    # page beside its neighbours, and over the pictures' pages in a file of their own.
    numbers = range(len(stored.pages))
    pictured = [
        number
        for number in numbers
        if stored.pages[number].shown == Shown.TURNED_UPRIGHT
        and readable_from_pictures(stored.pages[number].text)
    ]
    layered = [number for number in numbers if number not in pictured]
    from_layer = (
        [
            page
            for page in lettered_pages(to_parse, range(min(layered), max(layered) + 1))
            if page.page_idx in layered
        ]
        if layered and any(page.text for page in stored.pages)
        else []
    )
    taken = {page.page_idx for page in from_layer}
    rest = [number for number in numbers if number not in taken]
    from_pictures = picture_pages(to_parse, rest) if rest else []
    return ParsedPages(
        pages=sorted([*from_layer, *from_pictures], key=lambda page: page.page_idx),
        pictures=frozenset(rest),
    )
