from datetime import datetime
from typing import Annotated, Self
from uuid import uuid4

import sqlalchemy
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator

from infonium.library import (
    Library,
    NotInLibrary,
    OwnerTransaction,
    document_table,
    item_table,
    metadata,
)
from infonium.passage import Passage, held_passage
from infonium.standings import MarkedStanding, StandingReader

# How the target of a link in a note's Markdown begins when it leads to one of the note's
# passages; the passage's id follows.
PASSAGE_LINK = "passage:"
PASSAGE_ID_PATTERN = r"^[0-9a-z]{1,32}$"
TITLE_CHARACTERS = 200
MARKDOWN_CHARACTERS = 500_000
REPEATED_ID = "each passage of a note has an id of its own"
# What the body of a note's passage is called where it is read beside the body of its note.
PASSAGE_BODY = "passage_body"
# Stored with every body, as the library's tables do.
NOTE_FORMAT = 1

# A note is kept in two tables: its title, its Markdown and its times in one row, and each of
# its passages, as it was linked, in a row of its own under its place in the note. A passage's
# row names the item and the document it lies in, so that the items of every note are read
# without reading a document, and so that a document a note links into cannot be deleted.
# A note is always read by one statement over both tables: two statements of one transaction
# may see two states of the library, since a transaction begins only at its first write, and
# a note read by two could show the Markdown of one draft with the passages of another.
note_table = sqlalchemy.Table(
    "note",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)
note_passage_table = sqlalchemy.Table(
    "note_passage",
    metadata,
    sqlalchemy.Column(
        "note_id", sqlalchemy.String, sqlalchemy.ForeignKey(note_table.c.id), primary_key=True
    ),
    sqlalchemy.Column("position", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column(
        "item_id", sqlalchemy.String, sqlalchemy.ForeignKey(item_table.c.id), nullable=False
    ),
    sqlalchemy.Column(
        "document_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(document_table.c.id),
        nullable=False,
    ),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)

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


class NoteText(BaseModel):
    # What the row of a note keeps: all of the note but its passages.
    id: str
    title: str
    markdown: str
    made: AwareDatetime
    changed: AwareDatetime


class KeptPassage(BaseModel):
    # A passage of a note as it is kept, with the item it lies in.
    item_id: str
    note_passage: NotePassage


class KeptNote(BaseModel):
    # A note as it is kept: its text, and its passages in the order of the note.
    text: NoteText
    passages: list[KeptPassage]

    def draft(self) -> NoteDraft:
        return NoteDraft(
            title=self.text.title,
            markdown=self.text.markdown,
            passages=[kept.note_passage for kept in self.passages],
        )


class ShownPassage(BaseModel):
    # A passage of a note as a reader is shown it: the item it lies in and what that item is
    # called now, and where it stands in the document the item holds now.
    note_passage: NotePassage
    item_id: str
    title: str
    marked: MarkedStanding


class ShownNote(BaseModel):
    text: NoteText
    passages: list[ShownPassage]


class NoteListing(BaseModel):
    # A note as a list of notes gives it: its text without the Markdown, and the items its
    # passages lie in, each once, in the order of its passages.
    text: NoteText
    items: list[str]


def missing_note(note_id: str) -> NotInLibrary:
    return NotInLibrary(f"note {note_id} is not in the library")


class Notes:
    # The notes of one owner's library, read and written in one transaction.
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.connection = transaction.connection
        self.owner = transaction.owner
        self.library = Library(transaction)
        self.standing_reader = StandingReader(transaction)

    def linked_items(self, draft: NoteDraft) -> list[str]:
        # The item each passage of the draft lies in. A passage is refused unless its document
        # is one of the owner's and holds it as it is given. A draft is checked against the
        # library itself, not the documents kept for standings, which do not ask whether the
        # owner holds a document; each document of the draft is read once.
        documents = {
            document_id: self.library.owned_document(document_id)
            for document_id in dict.fromkeys(
                linked.passage.document_id for linked in draft.passages
            )
        }
        items = []
        for linked in draft.passages:
            document = documents[linked.passage.document_id]
            held_passage(linked.passage, document)
            items.append(document.item_id)
        return items

    def keep(self, text: NoteText, draft: NoteDraft, items: list[str]) -> None:
        self.connection.execute(
            sqlalchemy.insert(note_table).values(
                {
                    note_table.c.id: text.id,
                    note_table.c.owner: self.owner,
                    note_table.c.format: NOTE_FORMAT,
                    note_table.c.body: text.model_dump_json(),
                }
            )
        )
        if draft.passages:
            self.connection.execute(
                sqlalchemy.insert(note_passage_table),
                [
                    {
                        note_passage_table.c.note_id.name: text.id,
                        note_passage_table.c.position.name: position,
                        note_passage_table.c.owner.name: self.owner,
                        note_passage_table.c.item_id.name: item_id,
                        note_passage_table.c.document_id.name: linked.passage.document_id,
                        note_passage_table.c.format.name: NOTE_FORMAT,
                        note_passage_table.c.body.name: linked.model_dump_json(),
                    }
                    for position, (linked, item_id) in enumerate(
                        zip(draft.passages, items, strict=True)
                    )
                ],
            )

    def remove(self, note_id: str) -> None:
        # Its passages first, which name it.
        self.connection.execute(
            sqlalchemy.delete(note_passage_table).where(
                note_passage_table.c.owner == self.owner, note_passage_table.c.note_id == note_id
            )
        )
        removed = self.connection.execute(
            sqlalchemy.delete(note_table).where(
                note_table.c.owner == self.owner, note_table.c.id == note_id
            )
        )
        if removed.rowcount == 0:
            raise missing_note(note_id)

    def write(self, draft: NoteDraft, made: datetime) -> ShownNote:
        items = self.linked_items(draft)
        text = NoteText(
            id=uuid4().hex, title=draft.title, markdown=draft.markdown, made=made, changed=made
        )
        self.keep(text, draft, items)
        return self.shown(text.id)

    def rewrite(self, note_id: str, draft: NoteDraft, changed: datetime) -> ShownNote:
        # The time it was changed moves only when the draft differs from what was kept.
        kept = self.kept_note(note_id)
        items = self.linked_items(draft)
        if draft != kept.draft():
            self.remove(note_id)
            self.keep(
                kept.text.model_copy(
                    update={"title": draft.title, "markdown": draft.markdown, "changed": changed}
                ),
                draft,
                items,
            )
        return self.shown(note_id)

    def kept_notes(self, *where: sqlalchemy.ColumnElement[bool]) -> list[KeptNote]:
        # The owner's notes, each with its passages, read by one statement.
        passage_body = note_passage_table.c.body.label(PASSAGE_BODY)
        rows = self.connection.execute(
            sqlalchemy.select(
                note_table.c.id, note_table.c.body, note_passage_table.c.item_id, passage_body
            )
            .select_from(
                note_table.outerjoin(
                    note_passage_table,
                    sqlalchemy.and_(
                        note_passage_table.c.note_id == note_table.c.id,
                        note_passage_table.c.owner == self.owner,
                    ),
                )
            )
            .where(note_table.c.owner == self.owner, *where)
            .order_by(note_table.c.id, note_passage_table.c.position)
        )
        notes: dict[str, KeptNote] = {}
        for note_id, note_body, item_id, body in rows:
            if note_id not in notes:
                notes[note_id] = KeptNote(text=NoteText.model_validate_json(note_body), passages=[])
            if body is not None:
                notes[note_id].passages.append(
                    KeptPassage(item_id=item_id, note_passage=NotePassage.model_validate_json(body))
                )
        return list(notes.values())

    def kept_note(self, note_id: str) -> KeptNote:
        found = self.kept_notes(note_table.c.id == note_id)
        if not found:
            raise missing_note(note_id)
        return found[0]

    def shown(self, note_id: str) -> ShownNote:
        kept = self.kept_note(note_id)
        return ShownNote(
            text=kept.text,
            passages=[
                ShownPassage(
                    note_passage=linked.note_passage,
                    item_id=linked.item_id,
                    title=self.standing_reader.holding(linked.item_id).title,
                    marked=self.standing_reader.marked_standing(
                        linked.item_id, linked.note_passage.passage
                    ),
                )
                for linked in kept.passages
            ],
        )

    def listed(self) -> list[NoteListing]:
        # The one changed last first.
        return [
            NoteListing(
                text=note.text,
                items=list(dict.fromkeys(kept.item_id for kept in note.passages)),
            )
            for note in sorted(
                self.kept_notes(), key=lambda note: (note.text.changed, note.text.id), reverse=True
            )
        ]
