from pydantic import AwareDatetime, BaseModel

from infonium.anchor import Anchor, Citation, anchors_of
from infonium.document import BlockKind, Document

HEADING_LABEL = "heading"
INSTRUCTIONS = """\
You digest a document for a reader who wants to grasp it quickly and check every claim \
against the source.

The document arrives as numbered blocks. Each block starts with its number in square \
brackets. A block whose brackets also say "{heading_label}" is a heading.

Write the digest in {language}.

Grounding rules:
- State only what the document says. Add no outside knowledge and no opinion of your own.
- Give every statement one to three citations. A citation names one block by its number and \
quotes the words in that block that support the statement.
- Copy each quote from the block character for character, in the document's own language. \
Do not translate it, correct it, or shorten it with an ellipsis.
- Keep a quote to one sentence or a part of one, within a single block. Choose the passage \
where the document makes the point most directly.

Write two sections.

The first section is titled with the word for "Summary" in the output language. It holds \
three to five statements, one sentence each, that together tell what the document is about, \
what it argues or reports, and what it concludes.

The second section is titled with the word for "Takeaways" in the output language. It holds \
five to ten statements that a careful reader would want to keep: the key claims, findings, \
numbers, definitions, recommendations and caveats. Each statement stands on its own and is \
specific; keep the names and numbers the document gives.

When the document is too short to support these counts, write fewer statements.
"""


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


class DraftStatement(BaseModel):
    text: str
    citations: list[Citation]


class DraftSection(BaseModel):
    title: str
    statements: list[DraftStatement]


class DraftDigest(BaseModel):
    sections: list[DraftSection]


def digest_instructions(language: str) -> str:
    return INSTRUCTIONS.format(heading_label=HEADING_LABEL, language=language)


def digest_prompt(title: str, document: Document) -> str:
    numbered = [
        f"[{number}{f' {HEADING_LABEL}' if block.kind == BlockKind.HEADING else ''}] {block.text}"
        for number, block in enumerate(document.blocks)
        if block.text
    ]
    return "\n\n".join([f"Title: {title}", *numbered])


def grounded_sections(document: Document, draft: DraftDigest) -> list[Section]:
    return [
        Section(
            title=section.title,
            statements=[
                Statement(text=statement.text, anchors=anchors_of(document, statement.citations))
                for statement in section.statements
            ],
        )
        for section in draft.sections
    ]
