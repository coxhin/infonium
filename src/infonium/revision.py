from typing import Annotated

from pydantic import AwareDatetime, BaseModel, Field

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
