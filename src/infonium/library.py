import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Any, BinaryIO, Literal

import sqlalchemy
import sqlite_vec
from filelock import FileLock
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, TypeAdapter
from sqlalchemy.dialects import sqlite

from infonium.digest import Digest
from infonium.document import HTML_MEDIA_TYPE, Document, MediaType, Tagged

# The one user of a self-hosted library. Item ids hash the owner, so a deployment that adds
# accounts keeps this id for that first user.
LOCAL_OWNER = "local"
LIBRARY_FILE = "library.sqlite"
LOCK_FILE = "library.lock"
SNAPSHOT_DIRECTORY = "snapshots"
UPLOAD_DIRECTORY = "uploads"
WORK_DIRECTORY = "work"
CONTENT_HASH = "sha256"
# Stored with every body, so that a later change to the models can tell which form a row has.
# Form 1 held web pages only: its documents name no media type, and its items name a url and
# a title. Form 2 gave documents a media type. Form 3 gave items an origin and a reading.
BODY_FORMAT = 3
EARLIER_DOCUMENT_FORMAT = 1
# A reading that the service's stopping cut off this many times is given up.
READING_STARTS = 3
CUT_OFF = "the reading was cut off {starts} times by the service stopping"
NOT_UNDERWAY = "no reading of the item is under way"
ALREADY_UNDERWAY = "a reading of the item is under way already"
CONNECT = "connect"
ENFORCE_FOREIGN_KEYS = "PRAGMA foreign_keys = ON"
# Several threads write to the library's file: the readings, and the work other modules do in
# the background on tables of their own. With a write-ahead log one writes while the others
# read, which the file remembers once it is set; and a writer that finds another at work waits
# for it this long before it fails.
WRITE_AHEAD = "PRAGMA journal_mode = WAL"
WRITER_PATIENCE_MILLISECONDS = 30_000
AWAIT_WRITER = f"PRAGMA busy_timeout = {WRITER_PATIENCE_MILLISECONDS}"
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


class OriginKind(StrEnum):
    LINK = "link"
    UPLOAD = "upload"


class Link(Tagged):
    kind: Literal[OriginKind.LINK] = OriginKind.LINK
    url: str


class Upload(Tagged):
    kind: Literal[OriginKind.UPLOAD] = OriginKind.UPLOAD
    name: Annotated[str, StringConstraints(min_length=1)]


Origin = Annotated[Link | Upload, Field(discriminator="kind")]


class AttemptKind(StrEnum):
    UNDERWAY = "underway"
    FAILED = "failed"
    SETTLED = "settled"


class Underway(Tagged):
    # The attempt waits its turn or runs; starts counts the times it began to run.
    kind: Literal[AttemptKind.UNDERWAY] = AttemptKind.UNDERWAY
    starts: Annotated[int, Field(ge=0)]


class Failed(Tagged):
    kind: Literal[AttemptKind.FAILED] = AttemptKind.FAILED
    reason: str


class Settled(Tagged):
    kind: Literal[AttemptKind.SETTLED] = AttemptKind.SETTLED


class ReadingKind(StrEnum):
    UNREAD = "unread"
    READ = "read"


class Unread(Tagged):
    # Nothing has been read from the item's source yet.
    kind: Literal[ReadingKind.UNREAD] = ReadingKind.UNREAD
    attempt: Annotated[Underway | Failed, Field(discriminator="kind")]


class Read(Tagged):
    # The library keeps a document read from the item's source. The attempt is the latest
    # one, which may be a later one than the one that gave the document.
    kind: Literal[ReadingKind.READ] = ReadingKind.READ
    title: str
    document: str
    attempt: Annotated[Underway | Failed | Settled, Field(discriminator="kind")]


# What has come of reading an item's source into a document, and how the latest attempt at it
# stands. The four functions that follow are every change a reading goes through, and each
# refuses a reading it does not apply to.
Reading = Annotated[Unread | Read, Field(discriminator="kind")]

FIRST_READING = Unread(attempt=Underway(starts=0))


def is_underway(reading: Reading) -> bool:
    return isinstance(reading.attempt, Underway)


def reading_with(reading: Reading, attempt: Underway | Failed) -> Reading:
    match reading:
        case Read(title=title, document=document):
            return Read(title=title, document=document, attempt=attempt)
        case _:
            return Unread(attempt=attempt)


def reading_started(reading: Reading) -> Reading:
    # A new attempt joins the readings that wait their turn.
    if is_underway(reading):
        raise ValueError(ALREADY_UNDERWAY)
    return reading_with(reading, Underway(starts=0))


def reading_begun(reading: Reading) -> Reading:
    # The attempt begins to run, or begins again after the service's stopping cut it off; one
    # that has begun as often as a reading may is given up instead.
    match reading.attempt:
        case Underway(starts=starts) if starts < READING_STARTS:
            return reading_with(reading, Underway(starts=starts + 1))
        case Underway():
            return reading_with(reading, Failed(reason=CUT_OFF.format(starts=READING_STARTS)))
        case _:
            raise ValueError(NOT_UNDERWAY)


def reading_failed(reading: Reading, reason: str) -> Reading:
    if not is_underway(reading):
        raise ValueError(NOT_UNDERWAY)
    return reading_with(reading, Failed(reason=reason))


def reading_settled(reading: Reading, title: str, document: str) -> Reading:
    if not is_underway(reading):
        raise ValueError(NOT_UNDERWAY)
    return Read(title=title, document=document, attempt=Settled())


class Item(BaseModel):
    id: str
    origin: Origin
    added: AwareDatetime
    reading: Reading


def item_with(item: Item, reading: Reading) -> Item:
    return Item(id=item.id, origin=item.origin, added=item.added, reading=reading)


class EarlierItem(BaseModel):
    # An item as forms 1 and 2 stored it: a web page, read when it was kept.
    id: str
    url: str
    title: str
    added: AwareDatetime


class Holding(BaseModel):
    # The document the library keeps for an item, and what the item is called by it.
    title: str
    document: Document


class KeptSource(BaseModel):
    snapshot: str
    media_type: MediaType


class UnderwayItem(BaseModel):
    owner: str
    item_id: str


def field_path(*steps: tuple[type[BaseModel], str]) -> str:
    # The path of a field inside a stored body, for a query to read. Each step names a model
    # and a field of it, so a field that is renamed stops the service where the path is
    # built, not where a query quietly finds nothing.
    for model, field in steps:
        if field not in model.model_fields:
            raise LookupError(f"{model.__name__} has no field {field}")
    return ".".join(["$", *(field for _, field in steps)])


attempt_kind = sqlalchemy.func.json_extract(
    item_table.c.body, field_path((Item, "reading"), (Read, "attempt"), (Underway, "kind"))
)
kept_document_id = sqlalchemy.func.json_extract(
    item_table.c.body, field_path((Item, "reading"), (Read, "document"))
)
kept_title = sqlalchemy.func.json_extract(
    item_table.c.body, field_path((Item, "reading"), (Read, "title"))
)
SNAPSHOT_PATH = field_path((Document, "snapshot"))
MEDIA_TYPE_PATH = field_path((Document, "media_type"))


def item_identity(owner: str, distinction: str) -> str:
    # What tells one of an owner's items from another: a link's url, or the hash of an
    # upload's bytes.
    return sha256(f"{owner}\n{distinction}".encode()).hexdigest()


def timestamp_text(instant: datetime) -> str:
    return str(TIMESTAMP.dump_python(instant, mode="json"))


def replacing_insert(
    table: sqlalchemy.Table, row: dict[sqlalchemy.Column[Any], str | int]
) -> sqlite.Insert:
    insert = sqlite.insert(table).values(row)
    return insert.on_conflict_do_update(
        index_elements=list(table.primary_key.columns),
        set_={column: insert.excluded[column.name] for column in row if not column.primary_key},
    )


def set_up_connection(connection: sqlite3.Connection, record: object) -> None:
    # What every connection to the library's file is given before it is used: the settings,
    # and sqlite-vec's functions over vectors, for the index of blocks. No other extension can
    # be loaded through the connection afterwards.
    for setting in (ENFORCE_FOREIGN_KEYS, WRITE_AHEAD, AWAIT_WRITER):
        connection.execute(setting)
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)


def bring_up_to_date(connection: sqlalchemy.Connection) -> None:
    # Rows of an earlier form are rewritten in the current one, once.
    latest_document = (
        sqlalchemy.select(document_table.c.id)
        .where(document_table.c.item_id == item_table.c.id)
        .order_by(document_table.c.kept.desc())
        .limit(1)
        .scalar_subquery()
    )
    earlier_items = connection.execute(
        sqlalchemy.select(item_table.c.body, latest_document).where(
            item_table.c.format < BODY_FORMAT
        )
    ).all()
    for body, document_id in earlier_items:
        earlier = EarlierItem.model_validate_json(body)
        item = Item(
            id=earlier.id,
            origin=Link(url=earlier.url),
            added=earlier.added,
            reading=Read(title=earlier.title, document=document_id, attempt=Settled()),
        )
        connection.execute(
            sqlalchemy.update(item_table)
            .where(item_table.c.id == item.id)
            .values(format=BODY_FORMAT, body=item.model_dump_json())
        )
    connection.execute(
        sqlalchemy.update(document_table)
        .where(document_table.c.format == EARLIER_DOCUMENT_FORMAT)
        .values(
            body=sqlalchemy.func.json_set(document_table.c.body, MEDIA_TYPE_PATH, HTML_MEDIA_TYPE)
        )
    )
    for table in (document_table, digest_table):
        connection.execute(
            sqlalchemy.update(table).where(table.c.format < BODY_FORMAT).values(format=BODY_FORMAT)
        )


class OwnerTransaction:
    # One transaction on the library's database, and the owner on whose behalf it reads and
    # writes. Every store of rows is built on one, the library's own and those that other
    # modules keep in tables of theirs, and holds each of its queries to that owner.
    def __init__(self, connection: sqlalchemy.Connection, owner: str) -> None:
        self.connection = connection
        self.owner = owner


class Library:
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.connection = transaction.connection
        self.owner = transaction.owner

    def keep_item(self, item: Item) -> None:
        self.connection.execute(
            replacing_insert(
                item_table,
                {
                    item_table.c.id: item.id,
                    item_table.c.owner: self.owner,
                    item_table.c.added: timestamp_text(item.added),
                    item_table.c.format: BODY_FORMAT,
                    item_table.c.body: item.model_dump_json(),
                },
            )
        )

    def keep_document(self, document: Document, kept: datetime) -> None:
        self.connection.execute(
            replacing_insert(
                document_table,
                {
                    document_table.c.id: document.id,
                    document_table.c.owner: self.owner,
                    document_table.c.item_id: document.item_id,
                    document_table.c.kept: timestamp_text(kept),
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

    def sought(self, item_id: str) -> list[Item]:
        # The item of this id when the library holds it, and nothing when it does not.
        bodies = self.connection.execute(
            sqlalchemy.select(item_table.c.body).where(
                item_table.c.owner == self.owner, item_table.c.id == item_id
            )
        ).scalars()
        return [Item.model_validate_json(body) for body in bodies]

    def item(self, item_id: str) -> Item:
        found = self.sought(item_id)
        if not found:
            raise NotInLibrary(f"item {item_id} is not in the library")
        return found[0]

    def holding(self, item_id: str) -> Holding:
        kept = self.connection.execute(
            sqlalchemy.select(kept_title, document_table.c.body)
            .select_from(item_table)
            .join(document_table, document_table.c.id == kept_document_id)
            .where(item_table.c.owner == self.owner, item_table.c.id == item_id)
        ).one_or_none()
        if kept is None:
            raise NotInLibrary(f"item {item_id} has no document in the library")
        return Holding(title=kept[0], document=Document.model_validate_json(kept[1]))

    def held_document(self, document_id: str) -> Holding:
        # A document by its id, while an item of the owner holds it now, with what the item
        # is called; a document that no item holds now, or another owner's, is not found.
        kept = self.connection.execute(
            sqlalchemy.select(kept_title, document_table.c.body)
            .select_from(item_table)
            .join(document_table, document_table.c.id == kept_document_id)
            .where(item_table.c.owner == self.owner, document_table.c.id == document_id)
        ).first()
        if kept is None:
            raise NotInLibrary(f"no item of the library holds document {document_id}")
        return Holding(title=kept[0], document=Document.model_validate_json(kept[1]))

    def kept_source(self, item_id: str) -> KeptSource:
        source = self.connection.execute(
            sqlalchemy.select(
                sqlalchemy.func.json_extract(document_table.c.body, SNAPSHOT_PATH),
                sqlalchemy.func.json_extract(document_table.c.body, MEDIA_TYPE_PATH),
            )
            .select_from(item_table)
            .join(document_table, document_table.c.id == kept_document_id)
            .where(item_table.c.owner == self.owner, item_table.c.id == item_id)
        ).one_or_none()
        if source is None:
            raise NotInLibrary(f"item {item_id} has no document in the library")
        return KeptSource(snapshot=source[0], media_type=source[1])

    def kept_digests(self, document_id: str) -> list[Digest]:
        bodies = self.connection.execute(
            sqlalchemy.select(digest_table.c.body).where(
                digest_table.c.owner == self.owner, digest_table.c.document_id == document_id
            )
        ).scalars()
        return [Digest.model_validate_json(body) for body in bodies]


class LibraryStore:
    def __init__(self, directory: Path) -> None:
        self.snapshots = directory / SNAPSHOT_DIRECTORY
        self.uploads = directory / UPLOAD_DIRECTORY
        self.work = directory / WORK_DIRECTORY
        for kept in (self.snapshots, self.uploads, self.work):
            kept.mkdir(parents=True, exist_ok=True)
        # One service at a time works on a library; a second one stops here.
        self.lock = FileLock(directory / LOCK_FILE)
        self.lock.acquire(timeout=0)
        self.engine = sqlalchemy.create_engine(f"sqlite:///{directory / LIBRARY_FILE}")
        sqlalchemy.event.listen(self.engine, CONNECT, set_up_connection)
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            bring_up_to_date(connection)

    @contextmanager
    def transaction(self, owner: str) -> Iterator[OwnerTransaction]:
        with self.engine.begin() as connection:
            yield OwnerTransaction(connection=connection, owner=owner)

    @contextmanager
    def library(self, owner: str) -> Iterator[Library]:
        with self.transaction(owner) as transaction:
            yield Library(transaction)

    def owners(self) -> list[str]:
        # Everyone who has an item in the library.
        with self.engine.begin() as connection:
            return list(
                connection.execute(sqlalchemy.select(item_table.c.owner).distinct()).scalars()
            )

    def underway(self) -> list[UnderwayItem]:
        with self.engine.begin() as connection:
            rows = connection.execute(
                sqlalchemy.select(item_table.c.owner, item_table.c.id).where(
                    attempt_kind == AttemptKind.UNDERWAY
                )
            )
            return [UnderwayItem(owner=owner, item_id=item_id) for owner, item_id in rows]

    def clear_work(self) -> None:
        # What a reading leaves in the work directory when the service stops under it.
        shutil.rmtree(self.work)
        self.work.mkdir()

    def clear_uploads(self, awaited: set[str]) -> None:
        # A waiting copy that no reading under way is going to read, left when the service
        # stopped between the end of a reading and the removal of its copy.
        for waiting in self.uploads.iterdir():
            if waiting.name not in awaited:
                waiting.unlink()

    def staged(self, content: BinaryIO) -> Path:
        # Bytes arrive in the work directory and are moved to their place whole.
        with NamedTemporaryFile(dir=self.work, delete=False) as arrived:
            shutil.copyfileobj(content, arrived)
        return Path(arrived.name)

    def snapshot(self, name: str) -> Path:
        return self.snapshots / name[:2] / name

    def keep_snapshot(self, file: Path) -> str:
        with file.open("rb") as content:
            name = file_digest(content, CONTENT_HASH).hexdigest()
            content.seek(0)
            arrived = self.staged(content)
        kept = self.snapshot(name)
        kept.parent.mkdir(parents=True, exist_ok=True)
        arrived.replace(kept)
        return name

    def upload(self, item_id: str) -> Path:
        # Where an upload waits, under the id of its item, while its reading is under way.
        return self.uploads / item_id

    def upload_identity(self, owner: str, arrived: Path) -> str:
        with arrived.open("rb") as content:
            return item_identity(owner, file_digest(content, CONTENT_HASH).hexdigest())
