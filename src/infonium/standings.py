from collections import OrderedDict
from threading import Lock

from pydantic import BaseModel

from infonium.document import Document
from infonium.library import Holding, Library, NotInLibrary, OwnerTransaction, Read, Unread
from infonium.passage import Passage, Standing, standing_in
from infonium.revision import RevisionNumber, Revisions

# How many parsed documents are kept for working out standings, those used last. A document
# of 2,000 blocks takes about 10 ms to parse, and a note may lie in tens of them.
DOCUMENTS_KEPT = 64


class ParsedDocuments:
    # Documents as parsed from the library, kept by owner and document id, the one used least
    # recently leaving first. A kept document cannot go stale for a standing: a document's
    # blocks never change under its id, which is a hash of them, and a standing reads nothing
    # of a document but its blocks. Its snapshot name may change under the same id, so the
    # library itself keeps nothing of this kind.
    def __init__(self, room: int) -> None:
        self.room = room
        self.documents: OrderedDict[tuple[str, str], Document] = OrderedDict()
        self.lock = Lock()

    def document(self, library: Library, owner: str, document_id: str) -> Document:
        # The library is the owner's, through which a document not yet kept is read.
        key = (owner, document_id)
        with self.lock:
            kept = self.documents.get(key)
            if kept is not None:
                self.documents.move_to_end(key)
                return kept
        parsed = library.owned_document(document_id)
        with self.lock:
            self.documents[key] = parsed
            self.documents.move_to_end(key)
            while len(self.documents) > self.room:
                self.documents.popitem(last=False)
        return parsed


parsed_documents = ParsedDocuments(room=DOCUMENTS_KEPT)


class StandingNow(BaseModel):
    # A kept passage as a reader is shown it: the number of the revision it was marked in,
    # and where it stands in the document its item holds now.
    revision: RevisionNumber
    standing: Standing


class Standings:
    # Where kept passages stand in the documents their items hold now, worked out as they are
    # read in one transaction. Each item's revisions are read once however many passages lie
    # in them, and each document is parsed once while it stays among the documents kept.
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.library = Library(transaction)
        self.owner = transaction.owner
        self.revisions = Revisions(transaction)
        self.holdings: dict[str, Holding] = {}
        self.numbers: dict[str, dict[str, RevisionNumber]] = {}

    def holding(self, item_id: str) -> Holding:
        # What the item holds now; an item without a document is not in the library.
        if item_id not in self.holdings:
            match self.library.item(item_id).reading:
                case Read(title=title, document=document_id):
                    self.holdings[item_id] = Holding(
                        title=title, document=self.document(document_id)
                    )
                case Unread():
                    raise NotInLibrary(f"item {item_id} has no document in the library")
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
        return parsed_documents.document(self.library, self.owner, document_id)

    def now(self, item_id: str, passage: Passage) -> StandingNow:
        return StandingNow(
            revision=self.revision_number(item_id, passage.document_id),
            standing=standing_in(
                passage, self.document(passage.document_id), self.holding(item_id).document
            ),
        )
