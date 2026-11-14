from pydantic import AwareDatetime, BaseModel

from infonium.anchor import Anchor, Citation, anchors_of
from infonium.document import BlockKind, Document
from infonium.llm import LanguageModel

INSTRUCTIONS = """\
You digest a document for a reader who wants to grasp it quickly and check every claim \
against the source.

The document arrives as numbered blocks. Each block starts with its number in square brackets. \
A block that starts with # after its number is a heading.

Write the digest in {language}.

Grounding rules:
- State only what the document says. Add no outside knowledge and no opinion of your own.
- Give every statement one to three citations. A citation names one block by its number and \
quotes the words in that block that support the statement.
- Copy each quote from the block character for character, in the document's own language. \
Do not translate it, correct it, or shorten it with an ellipsis.
- Keep a quote to one sentence or a part of one, within a single block. Choose the passage \
where the document makes the point most directly.

{recipe}
"""
HEADING_SIGN = "# "


class Statement(BaseModel):
    text: str
    anchors: list[Anchor]


class Section(BaseModel):
    title: str
    statements: list[Statement]


class Digest(BaseModel):
    document_id: str
    model: str
    written: AwareDatetime
    input_tokens: int
    output_tokens: int
    sections: list[Section]


class CitedStatement(BaseModel):
    text: str
    citations: list[Citation]


class CitedSection(BaseModel):
    title: str
    statements: list[CitedStatement]


class CitedDigest(BaseModel):
    sections: list[CitedSection]


class DigestBrief(BaseModel):
    title: str
    recipe: str
    language: str
    written: AwareDatetime


def numbered_blocks(document: Document) -> str:
    return "\n\n".join(
        f"[{number}] {HEADING_SIGN if block.kind == BlockKind.HEADING else ''}{block.text}"
        for number, block in enumerate(document.blocks)
        if block.text
    )


def grounded_sections(document: Document, cited: CitedDigest) -> list[Section]:
    return [
        Section(
            title=section.title,
            statements=[
                Statement(text=statement.text, anchors=anchors_of(document, statement.citations))
                for statement in section.statements
            ],
        )
        for section in cited.sections
    ]


def write_digest(document: Document, brief: DigestBrief, language_model: LanguageModel) -> Digest:
    completion = language_model.structured(
        instructions=INSTRUCTIONS.format(language=brief.language, recipe=brief.recipe),
        prompt=f"Title: {brief.title}\n\n{numbered_blocks(document)}",
        output=CitedDigest,
    )
    return Digest(
        document_id=document.id,
        model=language_model.name,
        written=brief.written,
        input_tokens=completion.input_tokens,
        output_tokens=completion.output_tokens,
        sections=grounded_sections(document, completion.output),
    )
