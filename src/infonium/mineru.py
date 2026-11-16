import os
import signal
import subprocess
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, Field

from infonium.document import HeadingLevel, PageNumber
from infonium.pdfium import StoredFile

# MinerU parses a PDF, run as a program of its own. Its hybrid backend reads tables,
# formulas and listings best, and gives a box for every line as long as it takes the words
# from the file's text layer. When it has to read the pages as pictures it gives one box for
# a whole block, so such a file is read by the pipeline backend, which keeps the lines.
#
# The engine takes a character from the text layer only when it stands within a tenth of a
# degree of a quarter turn, and it measures where a character sits in its line by a box of
# the line's height only when the character stands at exactly no angle. Any other it measures
# by the outline of its ink, and then it splits words and takes plain letters for raised or
# lowered ones. On a page that is only shown turned the lines lie, and it reads those worse
# still. So it is given a file that turns no page: pdfium.upright_file makes one.
ENGINE_INPUT = "-p"
ENGINE_OUTPUT = "-o"
ENGINE_BACKEND = "-b"
ENGINE_METHOD = "-m"
ENGINE_RESULTS = "*_middle.json"
ENGINE_LOG = "{backend}.log"
# The version of the engine this module is written to, and the key under which the results
# name the version that wrote them. How the engine treats a character by its angle is this
# version's own and shows in no shape of the results, so results of another version are
# refused where they enter.
EngineVersion = Literal["3.4.2"]
VERSION_KEY = "_version_name"
# The keys under which the results say which backend wrote them, and whether the hybrid
# backend judged the text layer unfit and read the pages as pictures.
BACKEND_KEY = "_backend"
READ_AS_PICTURES_KEY = "_ocr_enable"
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
# The normal form in which a letter is the plain one, however it was set: a ligature is its
# letters, and a letter of full width or in the style of a formula is the common one.
PLAIN_FORM: Final = "NFKC"


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
# Left, top, right and bottom, measured from the top left corner of the page as the engine
# was given it.
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
    # A PDF that turns none of its pages, the engine that parses it, and the folder in which
    # the engine may write.
    file: Path
    engine: Path
    folder: Path


class UnparsedFile(Exception):
    # The engine ended without results. The message is the last line it wrote, or how it
    # ended when it wrote nothing or was stopped by a signal.
    pass


def engine_results(
    to_parse: FileToParse, backend: Backend, method: Method, page_count: int
) -> bytes:
    # One run of the engine over a file of so many pages. What the engine writes goes to a
    # file in the folder and not to the caller, so an engine whose caller has died can still
    # write: it finishes its work and leaves by itself.
    patience = SPARE_SECONDS + PAGE_SECONDS * page_count
    found = to_parse.folder / backend
    where: list[str | Path] = [ENGINE_INPUT, to_parse.file, ENGINE_OUTPUT, found]
    how: list[str | Path] = [ENGINE_BACKEND, backend, ENGINE_METHOD, method]
    told = {ENGINE_PATIENCE: str(patience), TEMPORARY_FILES: str(to_parse.folder)}
    log = to_parse.folder / ENGINE_LOG.format(backend=backend)
    with log.open("wb") as logged:
        ended = subprocess.run(
            [to_parse.engine, *where, *how],
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


def lettered_pages(to_parse: FileToParse, page_count: int) -> list[ParsedPage]:
    # The pages the engine read from their text layer: none, when it judged the layer unfit,
    # as it judges a scan's hidden one, and read the pages as pictures of its own accord.
    results = engine_results(to_parse, Backend.HYBRID, Method.AUTO, page_count)
    hybrid = HybridParsedFile.model_validate_json(results)
    return [] if hybrid.read_as_pictures else hybrid.pdf_info


class ParsedPages(BaseModel):
    # A file's pages as the engine read them, in order, and whether it read them as pictures.
    pages: list[ParsedPage]
    from_pictures: bool


def parsed_pages(to_parse: FileToParse, stored: StoredFile) -> ParsedPages:
    # How the engine reads a file: from its text layer when it has one that the engine
    # judges fit, and otherwise as pictures, every page of it.
    page_count = len(stored.pages)
    layered = any(page.text for page in stored.pages)
    if layered and (pages := lettered_pages(to_parse, page_count)):
        return ParsedPages(pages=pages, from_pictures=False)
    pictures = engine_results(to_parse, Backend.PIPELINE, Method.OCR, page_count)
    return ParsedPages(pages=ParsedFile.model_validate_json(pictures).pdf_info, from_pictures=True)
