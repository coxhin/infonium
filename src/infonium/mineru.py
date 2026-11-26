import re
import signal
import subprocess
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, TypeAdapter

from infonium import lineage
from infonium.document import HeadingLevel, PageFraction, PageNumber

# MinerU parses a PDF, run as a program of its own, with its hybrid backend, which is left to
# choose how it reads a file. From a text layer it judges fit it takes the words, and gives
# every line with its own box. A file without one, or with one it judges unfit, as it judges
# a scan's hidden layer, it reads as pictures of the pages: a language model then recognises
# the words of a whole region at once, which keeps them truer than reading line by line does.
#
# The engine takes a character from the text layer only when it stands within a tenth of a
# degree of a quarter turn, and it measures where a character sits in its line by a box of
# the line's height only when the character stands at exactly no angle. Any other it measures
# by the outline of its ink, and then it splits words and takes plain letters for raised or
# lowered ones. On a page that is only shown turned the lines lie, and it reads those worse
# still. So it is given a file that turns no page: pdfium.upright_file makes one. A box of the
# line's height is as high and as deep as the character's font says its letters reach, so it
# is also given a file whose fonts say that truly: font_metrics.measured_file makes one. It
# takes either half of a character beyond the first 65,536 of Unicode for a broken character,
# and the letters of formulas set in a sentence are often such characters, so the fonts of the
# file it is given say plain letters for them: plain_letters.plain_letters_file makes it.
ENGINE_INPUT = "-p"
ENGINE_OUTPUT = "-o"
ENGINE_BACKEND = "-b"
HYBRID_BACKEND = "hybrid-engine"
ENGINE_METHOD = "-m"
OWN_CHOICE = "auto"
# Where in the folder it is given the engine writes its results, and where what it says while
# it works is kept.
RESULTS_FOLDER = "results"
ENGINE_LOG = "engine.log"
ENGINE_RESULTS = "*_middle.json"
# Beside its results the engine writes what it detected on each page before it made regions
# of it. A line it detected without reading its words is among that, as this kind.
ENGINE_DETECTIONS = "*_model.json"
DETECTED_LINE = "ocr_text"
# The version of the engine this module is written to, and the key under which the results
# name the version that wrote them. How the engine treats a character by its angle is this
# version's own and shows in no shape of the results, so results of another version are
# refused where they enter.
EngineVersion = Literal["3.4.2"]
VERSION_KEY = "_version_name"
# The keys under which the results say which backend wrote them, and whether it read the
# pages as pictures. The hybrid backend names itself so, and results of another are refused.
BACKEND_KEY = "_backend"
HybridBackend = Literal["hybrid"]
READ_AS_PICTURES_KEY = "_ocr_enable"
# The variable from which the engine takes how long it waits for its own results. When that
# time has passed it gives up, stops its server and leaves, whoever started it.
ENGINE_PATIENCE = "MINERU_TASK_RESULT_TIMEOUT_SECONDS"
# The variable from which a program takes where to keep its temporary files. The engine is
# given the folder, so that whatever it leaves behind is cleared with the folder.
TEMPORARY_FILES = "TMPDIR"
# The engine takes, of infonium's environment, what it reads that a person sets for it, and
# hands all it is given on to the server it starts and the workers that server starts: its
# own settings; the GPU, which the CUDA driver chooses and orders among its settings by
# CUDA_VISIBLE_DEVICES and CUDA_DEVICE_ORDER, and whose driver triton and the loader find on
# LD_LIBRARY_PATH where the system's list of libraries lacks it; its configuration file in
# the home, which names where its models are; the hubs it fetches models from, the caches it
# keeps them in, under the home unless XDG_CACHE_HOME or the hubs' own settings say
# otherwise, and the proxies to the hubs; and the PATH and the locale.
ENGINE_TAKES = re.compile(
    "|".join(
        (
            lineage.SEARCH_PATH,
            lineage.LOCALE,
            lineage.PROXIES,
            r"(?:MINERU|CUDA|HF|HUGGINGFACE|MODELSCOPE)_\w+",
            "LD_LIBRARY_PATH|HOME|XDG_CACHE_HOME",
        )
    )
)
# Measured with one engine on an RTX 5090: a run takes 10 to 15 seconds to start and stop its
# server, and between 0.4 and 8.5 seconds a page. For its work the engine is given a minute
# and more than twice the slowest of those a page, and the call a minute more around that, in
# which the engine starts, and stops itself once it has given up.
PAGE_SECONDS = 20
SPARE_SECONDS = 60
SILENT_ENGINE = "the PDF engine left with status {status} and wrote nothing"
STOPPED_ENGINE = "the PDF engine was stopped by signal {signal}"
# The signals that have a name, by their numbers. A system has more signals than names, and
# one without a name goes by its number.
SIGNAL_NAMES = {int(named): named.name for named in signal.Signals}
CONTROL_CHARACTER = "Cc"
# The control characters that lay text out; a listing keeps them.
LAYOUT_CHARACTERS = "\n\t"


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
    # A picture or a chart, by the box of the page it is drawn in.
    type: Literal[RegionType.IMAGE_BODY, RegionType.CHART_BODY]
    bbox: Box


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
    backend: HybridBackend = Field(alias=BACKEND_KEY)
    version: EngineVersion = Field(alias=VERSION_KEY)
    read_as_pictures: bool = Field(alias=READ_AS_PICTURES_KEY)
    pdf_info: list[ParsedPage]


class Detection(BaseModel):
    # One thing the engine detected on a page before it made regions of it: what it took it
    # for, and its box, whose edges are parts of the page's width and height.
    type: str
    bbox: tuple[PageFraction, PageFraction, PageFraction, PageFraction]


# What the engine detected in a file, page by page.
DETECTIONS = TypeAdapter(list[list[Detection]])


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


def engine_results(to_parse: FileToParse, page_count: int) -> Path:
    # One run of the engine over a file of so many pages, and the file of its results. What
    # the engine writes while it works goes to a file in the folder and not to the caller, so
    # an engine whose caller has died can still write: it finishes its work and leaves by
    # itself.
    patience = SPARE_SECONDS + PAGE_SECONDS * page_count
    output = to_parse.folder / RESULTS_FOLDER
    where: list[str | Path] = [ENGINE_INPUT, to_parse.file, ENGINE_OUTPUT, output]
    how = [ENGINE_BACKEND, HYBRID_BACKEND, ENGINE_METHOD, OWN_CHOICE]
    told = {ENGINE_PATIENCE: str(patience), TEMPORARY_FILES: str(to_parse.folder)}
    log = to_parse.folder / ENGINE_LOG
    with (
        log.open("wb") as logged,
        lineage.started(
            [to_parse.engine, *where, *how],
            ENGINE_TAKES,
            told,
            stdin=None,
            stdout=logged,
            stderr=subprocess.STDOUT,
        ) as engine,
    ):
        status = engine.wait(timeout=patience + SPARE_SECONDS)
    results = [*output.rglob(ENGINE_RESULTS)]
    if status < 0:
        # A signal stops the engine in the middle of its work, where the last line it wrote
        # says only how far it had come. A program stopped so leaves with the signal's number
        # as its status, below zero.
        stopped_by = -status
        raise UnparsedFile(STOPPED_ENGINE.format(signal=SIGNAL_NAMES.get(stopped_by, stopped_by)))
    if status or not results:
        stripped = [line.strip() for line in log.read_text("utf-8", "replace").splitlines()]
        log_lines = [line for line in stripped if line]
        raise UnparsedFile(log_lines[-1] if log_lines else SILENT_ENGINE.format(status=status))
    [kept] = results
    return kept


class ReadFrom(StrEnum):
    TEXT_LAYER = "text_layer"
    PICTURES = "pictures"


class LayerPages(BaseModel):
    # A file's pages as the engine read them from its text layer, in order. Every line has
    # its own box.
    read_from: Literal[ReadFrom.TEXT_LAYER] = ReadFrom.TEXT_LAYER
    pages: list[ParsedPage]


class PicturePages(BaseModel):
    # A file's pages as the engine read them as pictures, in order. A region then comes as
    # one line that holds all its words, in the region's box. The lines themselves the engine
    # detects apart from the words, and only in texts and headings; their boxes are kept by
    # the number of their page.
    read_from: Literal[ReadFrom.PICTURES] = ReadFrom.PICTURES
    pages: list[ParsedPage]
    line_boxes: dict[PageNumber, list[Box]]


ParsedPages = Annotated[LayerPages | PicturePages, Field(discriminator="read_from")]


def line_boxes(page: ParsedPage, detections: list[Detection]) -> list[Box]:
    # The lines among what was detected on a page, measured like every other box of the page.
    width, height = page.page_size
    return [
        (left * width, top * height, right * width, bottom * height)
        for left, top, right, bottom in (
            detection.bbox for detection in detections if detection.type == DETECTED_LINE
        )
    ]


def parsed_pages(to_parse: FileToParse, page_count: int) -> ParsedPages:
    # A file of so many pages as the engine reads it: from its text layer when it has one
    # that the engine judges fit, and otherwise as pictures, every page of it.
    results = engine_results(to_parse, page_count)
    parsed = ParsedFile.model_validate_json(results.read_bytes())
    if not parsed.read_as_pictures:
        return LayerPages(pages=parsed.pdf_info)
    [written] = results.parent.glob(ENGINE_DETECTIONS)
    detections = DETECTIONS.validate_json(written.read_bytes())
    return PicturePages(
        pages=parsed.pdf_info,
        line_boxes={
            page.page_idx: line_boxes(page, on_page)
            for page, on_page in zip(parsed.pdf_info, detections, strict=True)
        },
    )
