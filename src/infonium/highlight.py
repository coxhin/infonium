from collections import defaultdict
from datetime import datetime
from hashlib import sha256
from typing import Annotated
from uuid import uuid4

import sqlalchemy
from pydantic import AwareDatetime, BaseModel, StringConstraints
from sqlalchemy.dialects import sqlite

from infonium.library import NotInLibrary, OwnerTransaction, document_table, item_table, metadata
from infonium.passage import Found, Lost, Passage, held_passage
from infonium.standings import MarkedStanding, StandingReader

# The most a reader notes on one highlight.
COMMENT_CHARACTERS = 10_000
CommentText = Annotated[str, StringConstraints(max_length=COMMENT_CHARACTERS)]
# Stored with every body, as the library's tables do.
HIGHLIGHT_FORMAT = 1

# A highlight is kept as it was marked: its passage names the revision it was marked in, and
# where it stands in a later revision is worked out when it is read. The passage key is a hash
# of the passage, so that the same anchors are marked once on an item.
highlight_table = sqlalchemy.Table(
    "highlight",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column(
        "item_id", sqlalchemy.String, sqlalchemy.ForeignKey(item_table.c.id), nullable=False
    ),
    sqlalchemy.Column(
        "document_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(document_table.c.id),
        nullable=False,
    ),
    sqlalchemy.Column("passage_key", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
    sqlalchemy.UniqueConstraint("owner", "item_id", "passage_key"),
)
# What tells one highlight of an item from another: the passage it marks.
MARKED_PASSAGE = [
    highlight_table.c.owner,
    highlight_table.c.item_id,
    highlight_table.c.passage_key,
]


class HighlightDraft(BaseModel):
    # What a reader marks: a passage of one revision of an item's source, and what the reader
    # notes on it, which may be nothing.
    passage: Passage
    comment: CommentText = ""


class Comment(BaseModel):
    # What a reader notes on a highlight; no text takes the comment away.
    text: CommentText


class Highlight(BaseModel):
    # A highlight as it is kept.
    id: str
    item_id: str
    passage: Passage
    comment: str
    made: AwareDatetime


class ShownHighlight(BaseModel):
    # A highlight with where it stands in the document its item holds now.
    highlight: Highlight
    marked: MarkedStanding


class MarkedItem(BaseModel):
    # An item that has highlights, with what it is called now and its highlights in the order
    # a reader is shown them.
    item_id: str
    title: str
    highlights: list[ShownHighlight]


def passage_key(passage: Passage) -> str:
    return sha256(passage.model_dump_json().encode()).hexdigest()


def reading_order(shown: ShownHighlight) -> tuple[int, int, int, datetime, str]:
    # Those that stand in the document come first, by where they stand in it, and the lost
    # after them, in the order they were made.
    made = shown.highlight.made
    match shown.marked.standing:
        case Found(passage=passage):
            first = passage.anchors[0]
            return (0, first.block_number, first.start, made, shown.highlight.id)
        case Lost():
            return (1, 0, 0, made, shown.highlight.id)


def missing_highlight(highlight_id: str) -> NotInLibrary:
    return NotInLibrary(f"highlight {highlight_id} is not in the library")


class Highlights:
    # The highlights of one owner's items, read and written in one transaction.
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.connection = transaction.connection
        self.owner = transaction.owner
        self.standing_reader = StandingReader(transaction)

    def of_highlight(self, item_id: str, highlight_id: str) -> list[sqlalchemy.ColumnElement[bool]]:
        return [
            highlight_table.c.owner == self.owner,
            highlight_table.c.item_id == item_id,
            highlight_table.c.id == highlight_id,
        ]

    def mark(self, item_id: str, draft: HighlightDraft, made: datetime) -> ShownHighlight:
        # The passage must lie in a revision of the item and be held there as it is given. A
        # passage the item has a highlight of already is that highlight, unchanged.
        passage = draft.passage
        self.standing_reader.revision_number(item_id, passage.document_id)
        held_passage(passage, self.standing_reader.document(passage.document_id))
        key = passage_key(passage)
        highlight = Highlight(
            id=uuid4().hex, item_id=item_id, passage=passage, comment=draft.comment, made=made
        )
        self.connection.execute(
            sqlite.insert(highlight_table)
            .values(
                {
                    highlight_table.c.id: highlight.id,
                    highlight_table.c.owner: self.owner,
                    highlight_table.c.item_id: item_id,
                    highlight_table.c.document_id: passage.document_id,
                    highlight_table.c.passage_key: key,
                    highlight_table.c.format: HIGHLIGHT_FORMAT,
                    highlight_table.c.body: highlight.model_dump_json(),
                }
            )
            .on_conflict_do_nothing(index_elements=MARKED_PASSAGE)
        )
        body = self.connection.execute(
            sqlalchemy.select(highlight_table.c.body).where(
                highlight_table.c.owner == self.owner,
                highlight_table.c.item_id == item_id,
                highlight_table.c.passage_key == key,
            )
        ).scalar_one()
        return self.shown(Highlight.model_validate_json(body))

    def shown(self, highlight: Highlight) -> ShownHighlight:
        return ShownHighlight(
            highlight=highlight,
            marked=self.standing_reader.marked_standing(highlight.item_id, highlight.passage),
        )

    def in_reading_order(self, highlights: list[Highlight]) -> list[ShownHighlight]:
        return sorted((self.shown(highlight) for highlight in highlights), key=reading_order)

    def kept(self, *where: sqlalchemy.ColumnElement[bool]) -> list[Highlight]:
        bodies = self.connection.execute(
            sqlalchemy.select(highlight_table.c.body).where(
                highlight_table.c.owner == self.owner, *where
            )
        ).scalars()
        return [Highlight.model_validate_json(body) for body in bodies]

    def of_item(self, item_id: str) -> list[ShownHighlight]:
        # An item without a document is not in the library, whether it has highlights or not.
        self.standing_reader.holding(item_id)
        return self.in_reading_order(self.kept(highlight_table.c.item_id == item_id))

    def of_library(self) -> list[MarkedItem]:
        # The item marked last first.
        by_item: dict[str, list[Highlight]] = defaultdict(list)
        for highlight in self.kept():
            by_item[highlight.item_id].append(highlight)
        latest = sorted(
            by_item.items(),
            key=lambda marked: max(highlight.made for highlight in marked[1]),
            reverse=True,
        )
        return [
            MarkedItem(
                item_id=item_id,
                title=self.standing_reader.holding(item_id).title,
                highlights=self.in_reading_order(highlights),
            )
            for item_id, highlights in latest
        ]

    def highlight(self, item_id: str, highlight_id: str) -> Highlight:
        found = self.kept(*self.of_highlight(item_id, highlight_id))
        if not found:
            raise missing_highlight(highlight_id)
        return found[0]

    def keep_comment(self, item_id: str, highlight_id: str, comment: Comment) -> ShownHighlight:
        # The text takes the place of what was noted; no text takes the comment away.
        highlight = self.highlight(item_id, highlight_id).model_copy(
            update={"comment": comment.text}
        )
        # The highlight may have been removed since it was read: then it is not found, and the
        # comment is not answered as kept.
        changed = self.connection.execute(
            sqlalchemy.update(highlight_table)
            .where(*self.of_highlight(item_id, highlight_id))
            .values(format=HIGHLIGHT_FORMAT, body=highlight.model_dump_json())
        )
        if changed.rowcount == 0:
            raise missing_highlight(highlight_id)
        return self.shown(highlight)

    def remove(self, item_id: str, highlight_id: str) -> None:
        removed = self.connection.execute(
            sqlalchemy.delete(highlight_table).where(*self.of_highlight(item_id, highlight_id))
        )
        if removed.rowcount == 0:
            raise missing_highlight(highlight_id)
