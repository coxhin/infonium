import subprocess
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from infonium.document import PageNumber

# MinerU 3.4.2 parses a PDF, run as a program of its own. Its hybrid backend reads tables,
# formulas and listings best, and gives a box for every line as long as it takes the words
# from the file's text layer. When it has to read the pages as pictures it gives one box for
# a whole block, so such a file is read by the pipeline backend, which keeps the lines.
ENGINE_INPUT = "-p"
ENGINE_OUTPUT = "-o"
ENGINE_BACKEND = "-b"
ENGINE_METHOD = "-m"
ENGINE_RESULTS = "*_middle.json"
CONTROL_CHARACTER = "Cc"
# The control characters that lay text out; a listing keeps them.
LAYOUT_CHARACTERS = "\n\t"


class Backend(StrEnum):
    HYBRID = "hybrid-engine"
    PIPELINE = "pipeline"


class Method(StrEnum):
    AUTO = "auto"
    OCR = "ocr"


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
    level: Annotated[int, Field(ge=1, le=6)]
    lines: list[Line]


class Plain(BaseModel):
    # Words that make a paragraph of their own wherever they stand.
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


Member = Annotated[Plain | PictureBody | TableBody | CodeBody, Field(discriminator="type")]


class Group(BaseModel):
    # A picture, a chart, a table or a listing, together with its captions and notes.
    type: Literal[RegionType.IMAGE, RegionType.CHART, RegionType.TABLE, RegionType.CODE]
    blocks: list[Member]


Region = Annotated[Text | Title | Plain | Group, Field(discriminator="type")]


class Furniture(BaseModel):
    type: Literal[RegionType.HEADER, RegionType.FOOTER, RegionType.PAGE_NUMBER, RegionType.ASIDE]


Margin = Annotated[Furniture | Plain, Field(discriminator="type")]


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
    pdf_info: list[ParsedPage]


class HybridParsedFile(ParsedFile):
    read_as_pictures: bool = Field(alias="_ocr_enable")


def word_lines(region: Region | Member | Margin) -> list[Line]:
    match region:
        case Text() | Title() | Plain():
            return region.lines
        case Group():
            return [line for member in region.blocks for line in word_lines(member)]
        case _:
            return []


def engine_results(
    file: Path, engine: Path, folder: Path, backend: Backend, method: Method
) -> bytes:
    where: list[str | Path] = [ENGINE_INPUT, file, ENGINE_OUTPUT, folder]
    how: list[str | Path] = [ENGINE_BACKEND, backend, ENGINE_METHOD, method]
    subprocess.run([engine, *where, *how], check=True, capture_output=True)
    [results] = folder.rglob(ENGINE_RESULTS)
    return results.read_bytes()


def picture_pages(file: Path, engine: Path, folder: Path) -> list[ParsedPage]:
    results = engine_results(file, engine, folder / Backend.PIPELINE, Backend.PIPELINE, Method.OCR)
    return ParsedFile.model_validate_json(results).pdf_info


def lettered_pages(file: Path, engine: Path, folder: Path) -> list[ParsedPage]:
    # The engine may still judge the text layer unfit, a scan's hidden one for example.
    results = engine_results(file, engine, folder / Backend.HYBRID, Backend.HYBRID, Method.AUTO)
    hybrid = HybridParsedFile.model_validate_json(results)
    if hybrid.read_as_pictures:
        return picture_pages(file, engine, folder)
    return hybrid.pdf_info
