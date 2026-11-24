from typing import Annotated

import sqlalchemy
from pydantic import AwareDatetime, BaseModel, Field

from infonium.library import TIMESTAMP, NotInLibrary, OwnerTransaction, revision_table

RevisionNumber = Annotated[
    int,
    Field(
        ge=1,
        description=(
            "The place of a revision among the revisions of its item, counted from 1 in the"
            " order they were first read."
        ),
    ),
]


class Revision(BaseModel):
    # One reading of an item's source, kept as it was read: the document it gave, and when
    # that document was first read. A reading that gives a document the item was read into
    # before is that revision again, not a new one.
    number: RevisionNumber
    document: str
    read: AwareDatetime


class Revisions:
    # The revisions of one owner's items, read in one transaction. A revision is numbered when
    # its document is kept (Library.keep_document), and its number never changes. No revision
    # or document is ever removed; one that would be must reckon with the rows of highlights
    # and of notes' passages, whose foreign keys to the document and the item it points into
    # forbid deleting them.
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.connection = transaction.connection
        self.owner = transaction.owner

    def revisions_where(self, *conditions: sqlalchemy.ColumnElement[bool]) -> list[Revision]:
        rows = self.connection.execute(
            sqlalchemy.select(
                revision_table.c.number, revision_table.c.document_id, revision_table.c.read
            )
            .where(revision_table.c.owner == self.owner, *conditions)
            .order_by(revision_table.c.number)
        ).all()
        return [
            Revision(number=number, document=document, read=TIMESTAMP.validate_python(read))
            for number, document, read in rows
        ]

    def of_item(self, item_id: str) -> list[Revision]:
        # The item's revisions, oldest first; none for an item that was never read.
        return self.revisions_where(revision_table.c.item_id == item_id)

    def of_document(self, document_id: str) -> Revision:
        # The revision that holds a document of the owner's.
        found = self.revisions_where(revision_table.c.document_id == document_id)
        if not found:
            raise NotInLibrary(f"document {document_id} is not in the library")
        return found[0]

    def numbered(self, item_id: str, number: int) -> Revision:
        # The item's revision of this number.
        found = self.revisions_where(
            revision_table.c.item_id == item_id, revision_table.c.number == number
        )
        if not found:
            raise NotInLibrary(f"item {item_id} has no revision {number} in the library")
        return found[0]
