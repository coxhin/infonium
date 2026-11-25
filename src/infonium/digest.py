from pydantic import AwareDatetime, BaseModel

from infonium.citing import (
    BLOCKS_EXPLAINED,
    CITING_RULES,
    DraftStatement,
    Statement,
    grounded,
)
from infonium.document import CHINESE_MARKS, Document

INSTRUCTIONS = """\
You digest a document for a reader who wants to grasp it quickly and check every claim \
against the source.

{blocks_explained}
Write the digest in {language}, punctuated as {language} is written: in Chinese, with the \
marks {chinese_marks} and never a comma or a colon of Latin script.

Grounding rules:
- Give every statement one to three citations.
{citing_rules}
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


class DraftSection(BaseModel):
    title: str
    statements: list[DraftStatement]


class DraftDigest(BaseModel):
    sections: list[DraftSection]


def digest_instructions(language: str) -> str:
    return INSTRUCTIONS.format(
        blocks_explained=BLOCKS_EXPLAINED,
        citing_rules=CITING_RULES,
        language=language,
        chinese_marks=CHINESE_MARKS,
    )


def grounded_sections(document: Document, draft: DraftDigest) -> list[Section]:
    return [
        Section(
            title=section.title,
            statements=[grounded(document, statement) for statement in section.statements],
        )
        for section in draft.sections
    ]
