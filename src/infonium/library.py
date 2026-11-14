from hashlib import sha256
from typing import Any

import sqlalchemy
from pydantic import AwareDatetime, BaseModel
from sqlalchemy.dialects import sqlite

from infonium.digest import Digest
from infonium.document import Document

LOCAL_OWNER = "local"
LIBRARY_FILE = "library.sqlite"

metadata = sqlalchemy.MetaData()
items = sqlalchemy.Table(
    "item",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column("added", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)
documents = sqlalchemy.Table(
    "document",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column(
        "item_id", sqlalchemy.String, sqlalchemy.ForeignKey(items.c.id), nullable=False, index=True
    ),
    sqlalchemy.Column("kept", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)
digests = sqlalchemy.Table(
    "digest",
    metadata,
    sqlalchemy.Column(
        "document_id", sqlalchemy.String, sqlalchemy.ForeignKey(documents.c.id), primary_key=True
    ),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)


class NotInLibrary(LookupError):
    pass


class Item(BaseModel):
    id: str
    url: str
    title: str
    snapshot: str
    added: AwareDatetime


def item_identity(owner: str, url: str) -> str:
    return sha256(f"{owner}\n{url}".encode()).hexdigest()


def replacing_insert(
    table: sqlalchemy.Table, row: dict[sqlalchemy.Column[Any], str]
) -> sqlite.Insert:
    insert = sqlite.insert(table).values(row)
    return insert.on_conflict_do_update(
        index_elements=list(table.primary_key.columns),
        set_={column: insert.excluded[column.name] for column in row if not column.primary_key},
    )


class Library:
    def __init__(self, connection: sqlalchemy.Connection, owner: str) -> None:
        self.connection = connection
        self.owner = owner

    def keep_page(self, item: Item, document: Document) -> None:
        added = item.added.isoformat()
        self.connection.execute(
            replacing_insert(
                items,
                {
                    items.c.id: item.id,
                    items.c.owner: self.owner,
                    items.c.added: added,
                    items.c.body: item.model_dump_json(),
                },
            )
        )
        self.connection.execute(
            replacing_insert(
                documents,
                {
                    documents.c.id: document.id,
                    documents.c.owner: self.owner,
                    documents.c.item_id: document.item_id,
                    documents.c.kept: added,
                    documents.c.body: document.model_dump_json(),
                },
            )
        )

    def keep_digest(self, digest: Digest) -> None:
        self.connection.execute(
            replacing_insert(
                digests,
                {
                    digests.c.document_id: digest.document_id,
                    digests.c.owner: self.owner,
                    digests.c.body: digest.model_dump_json(),
                },
            )
        )

    def items(self) -> list[Item]:
        bodies = self.connection.execute(
            sqlalchemy.select(items.c.body)
            .where(items.c.owner == self.owner)
            .order_by(items.c.added.desc())
        ).scalars()
        return [Item.model_validate_json(body) for body in bodies]

    def item(self, item_id: str) -> Item:
        body = self.connection.execute(
            sqlalchemy.select(items.c.body).where(
                items.c.owner == self.owner, items.c.id == item_id
            )
        ).scalar_one_or_none()
        if body is None:
            raise NotInLibrary(f"item {item_id} is not in the library")
        return Item.model_validate_json(body)

    def document(self, item_id: str) -> Document:
        body = self.connection.execute(
            sqlalchemy.select(documents.c.body)
            .where(documents.c.owner == self.owner, documents.c.item_id == item_id)
            .order_by(documents.c.kept.desc())
            .limit(1)
        ).scalar_one_or_none()
        if body is None:
            raise NotInLibrary(f"item {item_id} has no document in the library")
        return Document.model_validate_json(body)

    def digests(self, document_id: str) -> list[Digest]:
        bodies = self.connection.execute(
            sqlalchemy.select(digests.c.body).where(
                digests.c.owner == self.owner, digests.c.document_id == document_id
            )
        ).scalars()
        return [Digest.model_validate_json(body) for body in bodies]
