from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from infonium.citing import (
    BLOCKS_EXPLAINED,
    CITING_RULES,
    DraftStatement,
    Statement,
    grounded,
)
from infonium.document import CHINESE_MARKS, Document
from infonium.recipe import RECIPES, DigestStyle, RecipeStyle

CUSTOM_INSTRUCTIONS_CHARACTERS = 2_000
# What a custom style's instructions hold at least: one character that is not white space.
SAID_SOMETHING = r"\S"
# The engine that matches it: Python's own, whose white space is that of str.isspace, where
# the Rust engine pydantic uses by default takes U+001C to U+001F for words.
PYTHON_REGEX: Final = "python-re"

INSTRUCTIONS = """\
You digest a document for a reader who wants to grasp it quickly and check every claim \
against the source.

{blocks_explained}
Write the digest in {language}, punctuated as {language} is written: in Chinese, with the \
marks {chinese_marks} and never a comma or a colon of Latin script.

Grounding rules:
- Give every statement one to three citations.
{citing_rules}
Writing rules:
- Each statement says one thing, and no two statements of the digest say the same thing.
- Say what the document says directly. Do not begin a statement with words about the \
document itself or about its section, such as "the article says", "the author argues" or \
"the bottom line is", unless a section asks what the document is. Name who holds a view, \
such as the author, a speaker or a party, only where it matters who holds it: for an \
opinion, a disputed claim, or one of several voices.
- Keep the names, numbers, dates and units that the document gives.
- Every statement, in every section, marks one or two of its words with double asterisks, \
**like this**: the words a reader skimming the digest most needs, such as a key term, a \
name or a number. Never mark a word that only names the document or its kind, such as \
"article" or "paper". Put the asterisks right against the words they mark, with no space \
that the words would not have without them. Use no other Markdown.
- Give each section a form: "list" shows its statements one under another, and "paragraph" \
shows them as one running paragraph. Use the form that the description of the sections \
below gives, and where it gives none, the one that fits what it asks for.
- Keep to the ranges given below, and within them write as many statements as the \
document's length and substance support. When the document is too short to support them, \
write fewer.
- Leave out a section that the document gives nothing for, rather than fill it with what the \
document does not say.

"""
# The section rules of a custom style: what leads the reader's own instructions, which follow
# it as they wrote them.
CUSTOM_SECTION_RULES = """\
The reader describes the digest they want below, in their own words. Follow the description \
in the digest's structure, length, tone and focus. The rules above hold whatever it says.

The reader's description:
"""


class SectionForm(StrEnum):
    # How a section shows its statements: one under another, or as one running paragraph.
    LIST = "list"
    PARAGRAPH = "paragraph"


CustomInstructions = Annotated[
    str,
    StringConstraints(max_length=CUSTOM_INSTRUCTIONS_CHARACTERS, pattern=SAID_SOMETHING),
    Field(
        description=(
            "The reader's own instructions for the digest they want: its structure, length, tone"
            " and focus. infonium's rules of citing, of the language and of its punctuation hold"
            " whatever they say."
        )
    ),
]


class RecipeChoice(BaseModel):
    # A style written by its recipe, which takes no instructions of the reader's: a body may
    # give them as null, which is none.
    model_config = ConfigDict(extra="forbid")

    style: RecipeStyle
    instructions: None = None


class CustomChoice(BaseModel):
    # The reader's own style: their instructions for the digest in place of a recipe.
    model_config = ConfigDict(extra="forbid", regex_engine=PYTHON_REGEX)

    style: Literal[DigestStyle.CUSTOM]
    instructions: CustomInstructions


# The style a reader chooses a digest in, with the instructions of a custom one.
ChosenStyle = Annotated[RecipeChoice | CustomChoice, Field(discriminator="style")]


class Section(BaseModel):
    title: str
    form: SectionForm
    statements: list[Statement]


class Digest(BaseModel):
    # A document keeps one digest of each style, written as the reader chose it.
    document_id: str
    chosen: ChosenStyle
    model: str
    written: AwareDatetime
    input_tokens: int
    output_tokens: int
    sections: list[Section]


class EarlierSection(BaseModel):
    title: str
    statements: list[Statement]


class EarlierDigest(BaseModel):
    # A digest as the library kept it before digests had styles: one of each document, written
    # as an overview is, every section a list.
    document_id: str
    model: str
    written: AwareDatetime
    input_tokens: int
    output_tokens: int
    sections: list[EarlierSection]


class DraftSection(BaseModel):
    title: str
    form: SectionForm
    statements: list[DraftStatement]


class DraftDigest(BaseModel):
    sections: list[DraftSection]


def digest_instructions(language: str, chosen: ChosenStyle) -> str:
    # The style's section rules follow the rules every style shares. A reader's instructions
    # are joined on as they were written, whatever marks they hold.
    match chosen:
        case CustomChoice(instructions=instructions):
            section_rules = CUSTOM_SECTION_RULES + instructions + "\n"
        case RecipeChoice(style=style):
            section_rules = RECIPES[style].section_rules
    shared = INSTRUCTIONS.format(
        blocks_explained=BLOCKS_EXPLAINED,
        citing_rules=CITING_RULES,
        language=language,
        chinese_marks=CHINESE_MARKS,
    )
    return shared + section_rules


def grounded_sections(document: Document, draft: DraftDigest) -> list[Section]:
    return [
        Section(
            title=section.title,
            form=section.form,
            statements=[grounded(document, statement) for statement in section.statements],
        )
        for section in draft.sections
    ]
