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
from infonium.document import HTML_MEDIA_TYPE, Document, Figure, KeptPicture, MediaType, Tagged

# The one user of a self-hosted library. Item ids hash the owner, so a deployment that adds
# accounts keeps this id for that first user.
LOCAL_OWNER = "local"
LIBRARY_FILE = "library.sqlite"
LOCK_FILE = "library.lock"
SNAPSHOT_DIRECTORY = "snapshots"
PICTURE_DIRECTORY = "pictures"
UPLOAD_DIRECTORY = "uploads"
WORK_DIRECTORY = "work"
CONTENT_HASH = "sha256"
# Stored with every body, so that a later change to the models can tell which form a row has.
# Form 1 held web pages only: its documents name no media type, and its items name a url and
# a title. Form 2 gave documents a media type. Form 3 gave items an origin and a reading.
BODY_FORMAT = 3
EARLIER_DOCUMENT_FORMAT = 1
# The form of a revision's row: its number and when it was read, beside its document and item.
REVISION_FORMAT = 1
FIRST_REVISION = 1
# A reading that the service's stopping cut off this many times is given up.
READING_STARTS = 3
CUT_OFF = "the reading was cut off {starts} times by the service stopping"
NOT_UNDERWAY = "no reading of the item is under way"
ALREADY_UNDERWAY = "a reading of the item is under way already"
NO_FIRST_READING = "no first reading of the item is under way"
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
# The revisions of the items: each document an item was read into, under the number it was
# given when it was first read, and the time it was. A row is written with its document and
# never changed, so a number is not given twice. It is kept here, beside the documents, for
# keeping a document and numbering it are one step.
revision_table = sqlalchemy.Table(
    "revision",
    metadata,
    sqlalchemy.Column(
        "document_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(document_table.c.id),
        primary_key=True,
    ),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column(
        "item_id", sqlalchemy.String, sqlalchemy.ForeignKey(item_table.c.id), nullable=False
    ),
    sqlalchemy.Column("number", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("read", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.UniqueConstraint("item_id", "number"),
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


class KeptSource(BaseModel):
    snapshot: str
    media_type: MediaType


class Underway(Tagged):
    # The attempt waits its turn or runs; starts counts the times it began to run.
    kind: Literal[AttemptKind.UNDERWAY] = AttemptKind.UNDERWAY
    starts: Annotated[int, Field(ge=0)]


class FirstUnderway(Tagged):
    # An attempt at reading an item that holds no document yet, under way as one of an item
    # that holds one is. Once its source has arrived as a kind a reader can be shown as it is,
    # the file of the source is kept as it arrived until the attempt ends.
    kind: Literal[AttemptKind.UNDERWAY] = AttemptKind.UNDERWAY
    starts: Annotated[int, Field(ge=0)]
    arrival: KeptSource | None = None


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
    attempt: Annotated[FirstUnderway | Failed, Field(discriminator="kind")]


class Read(Tagged):
    # The library keeps a document read from the item's source. The attempt is the latest
    # one, which may be a later one than the one that gave the document.
    kind: Literal[ReadingKind.READ] = ReadingKind.READ
    title: str
    document: str
    attempt: Annotated[Underway | Failed | Settled, Field(discriminator="kind")]


# What has come of reading an item's source into a document, and how the latest attempt at it
# stands. The five functions that follow are every change a reading goes through, and each
# refuses a reading it does not apply to.
Reading = Annotated[Unread | Read, Field(discriminator="kind")]

FIRST_READING = Unread(attempt=FirstUnderway(starts=0))


def is_underway(reading: Reading) -> bool:
    return isinstance(reading.attempt, Underway | FirstUnderway)


def reading_started(reading: Reading) -> Reading:
    # A new attempt joins the readings that wait their turn.
    match reading:
        case _ if is_underway(reading):
            raise ValueError(ALREADY_UNDERWAY)
        case Read(title=title, document=document):
            return Read(title=title, document=document, attempt=Underway(starts=0))
        case _:
            return FIRST_READING


def reading_begun(reading: Reading) -> Reading:
    # The attempt begins to run, or begins again after the service's stopping cut it off; one
    # that has begun as often as a reading may is given up instead.
    match reading:
        case Read(title=title, document=document, attempt=Underway(starts=starts)) if (
            starts < READING_STARTS
        ):
            return Read(title=title, document=document, attempt=Underway(starts=starts + 1))
        case Unread(attempt=FirstUnderway(starts=starts, arrival=arrival)) if (
            starts < READING_STARTS
        ):
            return Unread(attempt=FirstUnderway(starts=starts + 1, arrival=arrival))
        case _:
            return reading_failed(reading, CUT_OFF.format(starts=READING_STARTS))


def reading_arrived(reading: Reading, arrival: KeptSource) -> Reading:
    # The source of an item that holds no document yet has arrived, and is kept as it arrived
    # while it is read.
    match reading:
        case Unread(attempt=FirstUnderway(starts=starts)):
            return Unread(attempt=FirstUnderway(starts=starts, arrival=arrival))
        case _:
            raise ValueError(NO_FIRST_READING)


def reading_failed(reading: Reading, reason: str) -> Reading:
    failed = Failed(reason=reason)
    match reading:
        case _ if not is_underway(reading):
            raise ValueError(NOT_UNDERWAY)
        case Read(title=title, document=document):
            return Read(title=title, document=document, attempt=failed)
        case _:
            return Unread(attempt=failed)


def reading_settled(reading: Reading, title: str, document: str) -> Reading:
    if not is_underway(reading):
        raise ValueError(NOT_UNDERWAY)
    return Read(title=title, document=document, attempt=Settled())


def arrival_of(reading: Reading) -> list[KeptSource]:
    # The file kept of the source as it arrived for the attempt under way, if one is.
    match reading:
        case Unread(attempt=FirstUnderway(arrival=KeptSource() as arrival)):
            return [arrival]
        case _:
            return []


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


class StagedSnapshot(BaseModel):
    # A source's file, staged in the work directory to be kept under its name.
    name: str
    file: Path


class StagedPicture(BaseModel):
    # A picture's file, staged in the work directory to be kept under its name.
    name: str
    file: Path


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
document_snapshot = sqlalchemy.func.json_extract(document_table.c.body, SNAPSHOT_PATH)
document_media_type = sqlalchemy.func.json_extract(document_table.c.body, MEDIA_TYPE_PATH)
arrival_snapshot = sqlalchemy.func.json_extract(
    item_table.c.body,
    field_path(
        (Item, "reading"), (Unread, "attempt"), (FirstUnderway, "arrival"), (KeptSource, "snapshot")
    ),
)


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
    number_unnumbered_documents(connection)


def number_unnumbered_documents(connection: sqlalchemy.Connection) -> None:
    # A library kept before revisions were numbered holds documents without a number: each
    # item's are numbered after the numbers it has, in the order they were kept, and read when
    # they were kept. What a document holds is left as it is.
    unnumbered = connection.execute(
        sqlalchemy.select(
            document_table.c.id,
            document_table.c.owner,
            document_table.c.item_id,
            document_table.c.kept,
        )
        .outerjoin(revision_table, revision_table.c.document_id == document_table.c.id)
        .where(revision_table.c.document_id.is_(None))
        .order_by(document_table.c.item_id, document_table.c.kept, document_table.c.id)
    ).all()
    numbered = connection.execute(
        sqlalchemy.select(
            revision_table.c.item_id, sqlalchemy.func.max(revision_table.c.number)
        ).group_by(revision_table.c.item_id)
    )
    highest: dict[str, int] = {item_id: number for item_id, number in numbered}
    rows: list[dict[str, str | int]] = []
    for document_id, owner, item_id, kept in unnumbered:
        highest[item_id] = highest.get(item_id, FIRST_REVISION - 1) + 1
        rows.append(
            {
                revision_table.c.document_id.name: document_id,
                revision_table.c.owner.name: owner,
                revision_table.c.item_id.name: item_id,
                revision_table.c.number.name: highest[item_id],
                revision_table.c.read.name: kept,
                revision_table.c.format.name: REVISION_FORMAT,
            }
        )
    if rows:
        connection.execute(sqlalchemy.insert(revision_table), rows)


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

    def keep_document(self, document: Document, read: datetime) -> bool:
        # A document read from an item's source is a revision of the item: a new one, numbered
        # one above the item's highest, unless the item was read into this document before.
        # Then the document stays as it was first kept, with the file kept beside it then, and
        # so does its revision: a document never changes under its id. Whether the document
        # is new to the library.
        kept = self.connection.execute(
            sqlite.insert(document_table)
            .values(
                {
                    document_table.c.id: document.id,
                    document_table.c.owner: self.owner,
                    document_table.c.item_id: document.item_id,
                    document_table.c.kept: timestamp_text(read),
                    document_table.c.format: BODY_FORMAT,
                    document_table.c.body: document.model_dump_json(),
                }
            )
            .on_conflict_do_nothing(index_elements=[document_table.c.id])
        )
        next_number = sqlalchemy.select(
            sqlalchemy.literal(document.id),
            sqlalchemy.literal(self.owner),
            sqlalchemy.literal(document.item_id),
            sqlalchemy.func.coalesce(
                sqlalchemy.func.max(revision_table.c.number), FIRST_REVISION - 1
            )
            + 1,
            sqlalchemy.literal(timestamp_text(read)),
            sqlalchemy.literal(REVISION_FORMAT),
        ).where(revision_table.c.item_id == document.item_id)
        self.connection.execute(
            sqlite.insert(revision_table)
            .from_select(
                [
                    revision_table.c.document_id,
                    revision_table.c.owner,
                    revision_table.c.item_id,
                    revision_table.c.number,
                    revision_table.c.read,
                    revision_table.c.format,
                ],
                next_number,
            )
            .on_conflict_do_nothing(index_elements=[revision_table.c.document_id])
        )
        return bool(kept.rowcount)

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

    def owned_document(self, document_id: str) -> Document:
        # A document of the owner's by its id: one that an item of theirs holds, or held when
        # a reader began to ask of it. Another owner's document is not found.
        body = self.connection.execute(
            sqlalchemy.select(document_table.c.body).where(
                document_table.c.owner == self.owner, document_table.c.id == document_id
            )
        ).scalar_one_or_none()
        if body is None:
            raise NotInLibrary(f"document {document_id} is not in the library")
        return Document.model_validate_json(body)

    def kept_picture(self, document_id: str, name: str) -> KeptPicture:
        # A picture that a figure of the owner's document carries, by its name. A picture that
        # the document does not name is not found, though the library may keep it for another.
        pictures = [
            block.picture
            for block in self.owned_document(document_id).blocks
            if isinstance(block, Figure) and block.picture is not None
        ]
        named = [picture for picture in pictures if picture.name == name]
        if not named:
            raise NotInLibrary(f"document {document_id} holds no picture {name}")
        return named[0]

    def kept_source(self, item_id: str) -> KeptSource:
        # The file of the item's source beside the document it holds, or, while it holds none,
        # the one its reading keeps as it arrived.
        reading = self.item(item_id).reading
        match reading:
            case Read(document=document_id):
                snapshot, media_type = self.connection.execute(
                    sqlalchemy.select(document_snapshot, document_media_type).where(
                        document_table.c.owner == self.owner, document_table.c.id == document_id
                    )
                ).one()
                return KeptSource(snapshot=snapshot, media_type=media_type)
            case _:
                arrival = arrival_of(reading)
                if not arrival:
                    raise NotInLibrary(f"item {item_id} keeps no file of its source")
                return arrival[0]

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
        self.pictures = directory / PICTURE_DIRECTORY
        self.uploads = directory / UPLOAD_DIRECTORY
        self.work = directory / WORK_DIRECTORY
        for kept in (self.snapshots, self.pictures, self.uploads, self.work):
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

    def staged_named(self, file: Path) -> tuple[str, Path]:
        # A copy of a file's bytes in the work directory, and the name those bytes hash to.
        with file.open("rb") as content:
            name = file_digest(content, CONTENT_HASH).hexdigest()
            content.seek(0)
            return name, self.staged(content)

    def snapshot(self, name: str) -> Path:
        return self.snapshots / name[:2] / name

    def staged_snapshot(self, file: Path) -> StagedSnapshot:
        name, staged = self.staged_named(file)
        return StagedSnapshot(name=name, file=staged)

    def keep_snapshot(self, staged: StagedSnapshot) -> None:
        kept = self.snapshot(staged.name)
        kept.parent.mkdir(parents=True, exist_ok=True)
        staged.file.replace(kept)

    def picture(self, name: str) -> Path:
        return self.pictures / name[:2] / name

    def staged_picture(self, file: Path) -> StagedPicture:
        name, staged = self.staged_named(file)
        return StagedPicture(name=name, file=staged)

    def keep_picture(self, staged: StagedPicture) -> None:
        # A picture is kept once, however many figures, documents and owners name it: the
        # bytes kept under its name already are the same bytes.
        kept = self.picture(staged.name)
        kept.parent.mkdir(parents=True, exist_ok=True)
        staged.file.replace(kept)

    def release_arrival(self, arrival: KeptSource) -> None:
        # A file a reading kept of its source as it arrived goes once the reading has let it
        # go, unless a document of anyone's, or another reading under way, keeps the same bytes.
        # Its caller keeps every other change to the files and to what names them waiting
        # meanwhile.
        held = sqlalchemy.select(
            sqlalchemy.exists().where(document_snapshot == arrival.snapshot)
            | sqlalchemy.exists().where(arrival_snapshot == arrival.snapshot)
        )
        with self.engine.begin() as connection:
            if not connection.execute(held).scalar_one():
                self.snapshot(arrival.snapshot).unlink()

    def upload(self, item_id: str) -> Path:
        # Where an upload waits, under the id of its item, while its reading is under way.
        return self.uploads / item_id

    def upload_identity(self, owner: str, arrived: Path) -> str:
        with arrived.open("rb") as content:
            return item_identity(owner, file_digest(content, CONTENT_HASH).hexdigest())
