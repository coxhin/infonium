from pydantic import BaseModel

from infonium.document import Document
from infonium.library import Holding, Library, NotInLibrary, OwnerTransaction
from infonium.passage import Passage, Standing, standing_in
from infonium.revision import RevisionNumber, Revisions


class StandingNow(BaseModel):
    # A kept passage as a reader is shown it: the number of the revision it was marked in,
    # and where it stands in the document its item holds now.
    revision: RevisionNumber
    standing: Standing


class Standings:
    # Where kept passages stand in the documents their items hold now, worked out as they are
    # read in one transaction. Each document and each item's revisions are read once however
    # many passages lie in them.
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.library = Library(transaction)
        self.revisions = Revisions(transaction)
        self.holdings: dict[str, Holding] = {}
        self.numbers: dict[str, dict[str, RevisionNumber]] = {}
        self.documents: dict[str, Document] = {}

    def holding(self, item_id: str) -> Holding:
        # What the item holds now; an item without a document is not in the library.
        if item_id not in self.holdings:
            self.holdings[item_id] = self.library.holding(item_id)
        return self.holdings[item_id]

    def revision_number(self, item_id: str, document_id: str) -> RevisionNumber:
        if item_id not in self.numbers:
            self.numbers[item_id] = {
                revision.document: revision.number for revision in self.revisions.of_item(item_id)
            }
        numbers = self.numbers[item_id]
        if document_id not in numbers:
            raise NotInLibrary(f"document {document_id} is no revision of item {item_id}")
        return numbers[document_id]

    def document(self, document_id: str) -> Document:
        if document_id not in self.documents:
            self.documents[document_id] = self.library.owned_document(document_id)
        return self.documents[document_id]

    def now(self, item_id: str, passage: Passage) -> StandingNow:
        # The document the item holds now is the one a passage is most often marked in, and
        # it is read with the item, not again.
        current = self.holding(item_id).document
        marked_in = (
            current if passage.document_id == current.id else self.document(passage.document_id)
        )
        return StandingNow(
            revision=self.revision_number(item_id, passage.document_id),
            standing=standing_in(passage, marked_in, current),
        )
