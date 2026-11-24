from typing import Annotated

import sqlalchemy
from pydantic import AwareDatetime, BaseModel, Field

from infonium.library import TIMESTAMP, NotInLibrary, OwnerTransaction, document_table

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
    # The revisions of one owner's items, read in one transaction. This first form numbers an
    # item's documents in the order they were last kept, so a reading that gives an earlier
    # document again moves that document's number; the numbers get a table of their own, and
    # stay, once revisions are built.
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.connection = transaction.connection
        self.owner = transaction.owner

    def of_item(self, item_id: str) -> list[Revision]:
        # The item's revisions, oldest first; none for an item that was never read.
        kept = self.connection.execute(
            sqlalchemy.select(document_table.c.id, document_table.c.kept)
            .where(document_table.c.owner == self.owner, document_table.c.item_id == item_id)
            .order_by(document_table.c.kept)
        ).all()
        return [
            Revision(number=number, document=document, read=TIMESTAMP.validate_python(read))
            for number, (document, read) in enumerate(kept, start=1)
        ]

    def of_document(self, document_id: str) -> Revision:
        # The revision that holds a document of the owner's.
        item_id = self.connection.execute(
            sqlalchemy.select(document_table.c.item_id).where(
                document_table.c.owner == self.owner, document_table.c.id == document_id
            )
        ).scalar_one_or_none()
        held = [
            revision for revision in self.of_item(item_id or "") if revision.document == document_id
        ]
        if not held:
            raise NotInLibrary(f"document {document_id} is not in the library")
        return held[0]
