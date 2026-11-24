from typing import Annotated

from pydantic import BaseModel, StringConstraints

from infonium.passage import Passage

# The most a reader notes on one highlight.
COMMENT_CHARACTERS = 10_000
CommentText = Annotated[str, StringConstraints(max_length=COMMENT_CHARACTERS)]


class HighlightDraft(BaseModel):
    # What a reader marks: a passage of one revision of an item's source, and what the reader
    # notes on it, which may be nothing.
    passage: Passage
    comment: CommentText = ""


class Comment(BaseModel):
    # What a reader notes on a highlight; no text takes the comment away.
    text: CommentText
