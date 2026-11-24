from typing import Annotated, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

from infonium.passage import Passage

# How the target of a link in a note's Markdown begins when it leads to one of the note's
# passages; the passage's id follows.
PASSAGE_LINK = "passage:"
PASSAGE_ID_PATTERN = r"^[0-9a-z]{1,32}$"
TITLE_CHARACTERS = 200
MARKDOWN_CHARACTERS = 500_000
REPEATED_ID = "each passage of a note has an id of its own"

PassageId = Annotated[
    str,
    StringConstraints(pattern=PASSAGE_ID_PATTERN),
    Field(
        description=(
            "What a note's Markdown calls one of the note's passages by: a link whose target"
            f" is '{PASSAGE_LINK}' followed by this id leads to the passage. The writer of the"
            " note chooses it."
        )
    ),
]


class NotePassage(BaseModel):
    # A passage a note links to, under the id its Markdown names it by.
    id: PassageId
    passage: Passage


class NoteDraft(BaseModel):
    # A note as it is written: what it is called, its text as Markdown, and the passages that
    # the Markdown links to. The passages may lie in any documents of the library.
    title: Annotated[str, StringConstraints(max_length=TITLE_CHARACTERS)]
    markdown: Annotated[str, StringConstraints(max_length=MARKDOWN_CHARACTERS)]
    passages: list[NotePassage]

    @model_validator(mode="after")
    def ids_apart(self) -> Self:
        ids = [passage.id for passage in self.passages]
        if len(set(ids)) < len(ids):
            raise ValueError(REPEATED_ID)
        return self
