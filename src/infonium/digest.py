from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from infonium.citing import (
    BLOCKS_EXPLAINED,
    CITING_RULES,
    DraftStatement,
    Statement,
    grounded,
)
from infonium.document import CHINESE_MARKS, Document
from infonium.recipe import DigestStyle, RecipeStyle, recipe_of

INSTRUCTIONS_CHARACTERS = 2_000
# What a custom style's description holds at least: one character that is not white space.
SAID_SOMETHING = r"\S"

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
document itself, such as "the article says" or "the author argues", unless a section asks \
what the document is. Name who holds a view, such as the author, a speaker or a party, only \
where it matters who holds it: for an opinion, a disputed claim, or one of several voices.
- Keep the names, numbers, dates and units that the document gives.
- In each statement, mark with double asterisks, **like this**, the one or two words that a \
reader skimming the digest most needs: a key term, a name or a number. Use no other Markdown.
- Give each section a form: "list" shows its statements one under another, and "paragraph" \
shows them as one running paragraph. Use the form that the description of the sections \
below gives, and where it gives none, the one that fits what it asks for.
- Within the ranges given below, write as many statements as the document's length and \
substance support. When the document is too short to support them, write fewer.
- Leave out a section that the document gives nothing for, rather than fill it with what the \
document does not say.

{sections}"""
# The sections of a custom style: the reader's own description, as they wrote it.
CUSTOM_SECTIONS = """\
The reader describes the digest they want below, in their own words. Follow the description \
in the digest's structure, length, tone and focus. The rules above hold whatever it says.

The reader's description:
{instructions}
"""


class SectionForm(StrEnum):
    # How a section shows its statements: one under another, or as one running paragraph.
    LIST = "list"
    PARAGRAPH = "paragraph"


CustomInstructions = Annotated[
    str,
    StringConstraints(max_length=INSTRUCTIONS_CHARACTERS, pattern=SAID_SOMETHING),
    Field(
        description=(
            "The reader's own description of the digest they want: its structure, length, tone"
            " and focus. infonium's rules of citing, of the language and of its punctuation hold"
            " whatever it says."
        )
    ),
]


class RecipeChoice(BaseModel):
    # A style written by its recipe, which takes no instructions of the reader's.
    model_config = ConfigDict(extra="forbid")

    style: RecipeStyle


class CustomChoice(BaseModel):
    # The reader's own style: their description of the digest in place of a recipe.
    style: Literal[DigestStyle.CUSTOM]
    instructions: CustomInstructions


# The style a reader chooses a digest in, with the description of a custom one.
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
    match chosen:
        case CustomChoice(instructions=instructions):
            sections = CUSTOM_SECTIONS.format(instructions=instructions)
        case RecipeChoice(style=style):
            sections = recipe_of(style).sections
    return INSTRUCTIONS.format(
        blocks_explained=BLOCKS_EXPLAINED,
        citing_rules=CITING_RULES,
        language=language,
        chinese_marks=CHINESE_MARKS,
        sections=sections,
    )


def grounded_sections(document: Document, draft: DraftDigest) -> list[Section]:
    return [
        Section(
            title=section.title,
            form=section.form,
            statements=[grounded(document, statement) for statement in section.statements],
        )
        for section in draft.sections
    ]
