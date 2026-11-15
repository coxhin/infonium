import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import sqlalchemy
from pydantic import AwareDatetime, BaseModel, TypeAdapter
from sqlalchemy.dialects import sqlite

from infonium.digest import Digest
from infonium.document import HTML_MEDIA_TYPE, Document

# The one user of a self-hosted library. Item ids hash the owner, so a deployment that adds
# accounts keeps this id for that first user.
LOCAL_OWNER = "local"
LIBRARY_FILE = "library.sqlite"
SNAPSHOT_DIRECTORY = "snapshots"
# Stored with every body, so that a later change to the models can tell which form a row has.
# Form 1 held web pages only: its documents do not name a media type, and its blocks have no
# places.
BODY_FORMAT = 2
MEDIA_TYPE_FIELD = "media_type"
CONNECT = "connect"
ENFORCE_FOREIGN_KEYS = "PRAGMA foreign_keys = ON"
TIMESTAMP = TypeAdapter(AwareDatetime)

metadata = sqlalchemy.MetaData()
item_table = sqlalchemy.Table(
    "item",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column("added", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)
document_table = sqlalchemy.Table(
    "document",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column(
        "item_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(item_table.c.id),
        nullable=False,
        index=True,
    ),
    sqlalchemy.Column("kept", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)
digest_table = sqlalchemy.Table(
    "digest",
    metadata,
    sqlalchemy.Column(
        "document_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(document_table.c.id),
        primary_key=True,
    ),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)


class NotInLibrary(LookupError):
    pass


class Item(BaseModel):
    id: str
    url: str
    title: str
    added: AwareDatetime


class Holding(BaseModel):
    item: Item
    document: Document


def item_identity(owner: str, url: str) -> str:
    return sha256(f"{owner}\n{url}".encode()).hexdigest()


def kept_document(body: str) -> Document:
    # A body that names no media type is of form 1, and so a web page.
    return Document.model_validate({MEDIA_TYPE_FIELD: HTML_MEDIA_TYPE, **json.loads(body)})


def timestamp_text(moment: datetime) -> str:
    return str(TIMESTAMP.dump_python(moment, mode="json"))


def replacing_insert(
    table: sqlalchemy.Table, row: dict[sqlalchemy.Column[Any], str | int]
) -> sqlite.Insert:
    insert = sqlite.insert(table).values(row)
    return insert.on_conflict_do_update(
        index_elements=list(table.primary_key.columns),
        set_={column: insert.excluded[column.name] for column in row if not column.primary_key},
    )


def enforce_foreign_keys(connection: sqlite3.Connection, record: object) -> None:
    connection.execute(ENFORCE_FOREIGN_KEYS)


class Library:
    def __init__(self, connection: sqlalchemy.Connection, owner: str, snapshots: Path) -> None:
        self.connection = connection
        self.owner = owner
        self.snapshots = snapshots

    def keep_snapshot(self, content: bytes) -> str:
        name = sha256(content).hexdigest()
        path = self.snapshots / name[:2] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return name

    def keep_page(self, item: Item, document: Document) -> None:
        added = timestamp_text(item.added)
        self.connection.execute(
            replacing_insert(
                item_table,
                {
                    item_table.c.id: item.id,
                    item_table.c.owner: self.owner,
                    item_table.c.added: added,
                    item_table.c.format: BODY_FORMAT,
                    item_table.c.body: item.model_dump_json(),
                },
            )
        )
        self.connection.execute(
            replacing_insert(
                document_table,
                {
                    document_table.c.id: document.id,
                    document_table.c.owner: self.owner,
                    document_table.c.item_id: document.item_id,
                    document_table.c.kept: added,
                    document_table.c.format: BODY_FORMAT,
                    document_table.c.body: document.model_dump_json(),
                },
            )
        )

    def keep_digest(self, digest: Digest) -> None:
        self.connection.execute(
            replacing_insert(
                digest_table,
                {
                    digest_table.c.document_id: digest.document_id,
                    digest_table.c.owner: self.owner,
                    digest_table.c.format: BODY_FORMAT,
                    digest_table.c.body: digest.model_dump_json(),
                },
            )
        )

    def items(self) -> list[Item]:
        bodies = self.connection.execute(
            sqlalchemy.select(item_table.c.body)
            .where(item_table.c.owner == self.owner)
            .order_by(item_table.c.added.desc())
        ).scalars()
        return [Item.model_validate_json(body) for body in bodies]

    def holding(self, item_id: str) -> Holding:
        bodies = self.connection.execute(
            sqlalchemy.select(item_table.c.body, document_table.c.body)
            .join(document_table, document_table.c.item_id == item_table.c.id)
            .where(item_table.c.owner == self.owner, item_table.c.id == item_id)
            .order_by(document_table.c.kept.desc())
            .limit(1)
        ).one_or_none()
        if bodies is None:
            raise NotInLibrary(f"item {item_id} is not in the library")
        return Holding(
            item=Item.model_validate_json(bodies[0]),
            document=kept_document(bodies[1]),
        )

    def kept_digests(self, document_id: str) -> list[Digest]:
        bodies = self.connection.execute(
            sqlalchemy.select(digest_table.c.body).where(
                digest_table.c.owner == self.owner, digest_table.c.document_id == document_id
            )
        ).scalars()
        return [Digest.model_validate_json(body) for body in bodies]


class LibraryStore:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.snapshots = directory / SNAPSHOT_DIRECTORY
        self.engine = sqlalchemy.create_engine(f"sqlite:///{directory / LIBRARY_FILE}")
        sqlalchemy.event.listen(self.engine, CONNECT, enforce_foreign_keys)
        metadata.create_all(self.engine)

    @contextmanager
    def library(self, owner: str) -> Iterator[Library]:
        with self.engine.begin() as connection:
            yield Library(connection=connection, owner=owner, snapshots=self.snapshots)
