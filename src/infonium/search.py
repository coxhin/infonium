import logging
import sqlite3
from array import array
from concurrent.futures import Future, ThreadPoolExecutor
from enum import StrEnum
from threading import Event, Lock
from typing import Annotated

import openai
import sqlalchemy
from pydantic import AfterValidator, BaseModel, Field, validate_call

from infonium.document import WORD_GAP, Block, BlockKind, BlockNumber, Document
from infonium.embedding import ENGINE_AWAY, Embedder, EmbeddingEngine, Vector
from infonium.library import (
    LibraryStore,
    OwnerTransaction,
    document_table,
    item_table,
    kept_document_id,
    kept_title,
    metadata,
)
from infonium.reasons import failure_reason
from infonium.terms import (
    TOKENIZER,
    all_words_expression,
    any_words_expression,
    has_word,
    indexed_words,
    sought_pieces,
    word_total,
)

# How many blocks each way of finding brings to the ranking, and how much the first places of
# a way count above its later ones: a block's score is the sum, over the ways that found it,
# of one over this number plus its place (reciprocal rank fusion).
CANDIDATES = 50
RANK_DAMPING = 60
# How a document's blocks are written into the index: the form of their terms (infonium.terms)
# and which of them are candidates by meaning. A document indexed in another form is indexed
# again. Form 1 left blocks of fewer than 8 words out of the candidates by meaning, and sent
# blocks to the engine without their titles.
INDEX_FORMAT = 2
# What says nothing by itself is found by its words alone, and is given no vector: a heading,
# by its kind, and a block of fewer words than this, as "See also", "muokkaa|" or "comté.". A
# character of Chinese or Japanese counts as a word.
MEANINGFUL_WORDS = 3
# Blocks are sent to the engine this many at a time. A question waits at the engine for the
# batch before it, so the batch is small.
EMBEDDING_BATCH = 8
# The longest the engine may take over a batch of blocks, and over a question, which a search
# waits for.
EMBEDDING_TIMEOUT_SECONDS = 120.0
QUERY_TIMEOUT_SECONDS = 10.0
FLOATS = "f"
# A block is sent to the engine under the title of its item, which tells what a short block,
# such as "Opening hours: 8 to 18", is about: asked when the pond is open, the engine put that
# block at a distance of 0.06 under its title and of 0.17 without it.
TITLE_GAP = "\n"
# How long the worker rests when nothing waits, which is also how soon it notices that an
# engine that was away is back, and how soon a document read while no one searches is indexed.
CATCH_UP_SECONDS = 10.0

WORDS_TABLE = "search_words"
WORDS_COLUMN = "words"
OWNER_COLUMN = "owner"

# The documents the index holds, and how many of their blocks it holds.
indexed_document_table = sqlalchemy.Table(
    "search_document",
    metadata,
    sqlalchemy.Column("document_id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column("item_id", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("blocks", sqlalchemy.Integer, nullable=False),
)
# Each block that holds words, once, under an id that is never given again, which is also its
# row among the words. Its vector is kept with it, with the model that made it; a block of few
# words has none.
indexed_block_table = sqlalchemy.Table(
    "search_block",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column(
        "document_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(indexed_document_table.c.document_id, ondelete="CASCADE"),
        nullable=False,
    ),
    sqlalchemy.Column("item_id", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("block_number", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("text", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("meaningful", sqlalchemy.Boolean, nullable=False),
    sqlalchemy.Column("vector_model", sqlalchemy.String),
    sqlalchemy.Column("embedding", sqlalchemy.LargeBinary),
    sqlalchemy.UniqueConstraint("document_id", "block_number"),
    sqlalchemy.CheckConstraint("(vector_model IS NULL) = (embedding IS NULL)"),
    sqlalchemy.CheckConstraint("meaningful OR vector_model IS NULL"),
    sqlalchemy.Index("search_block_vectors", "owner", "vector_model"),
    sqlite_autoincrement=True,
)
# The words of each block as the index takes them, and its owner; only the index's terms are
# kept, not the words.
CREATE_WORDS = (
    f"CREATE VIRTUAL TABLE {WORDS_TABLE} USING fts5({WORDS_COLUMN}, "
    f"{OWNER_COLUMN} UNINDEXED, content='', contentless_delete=1, contentless_unindexed=1, "
    f"tokenize='{TOKENIZER}')"
)
DROP_LEFT_OWNERS = f"DROP TABLE IF EXISTS {WORDS_TABLE}_content"
words_table = sqlalchemy.table(
    WORDS_TABLE,
    sqlalchemy.column("rowid", sqlalchemy.Integer),
    sqlalchemy.column(WORDS_COLUMN, sqlalchemy.String),
    sqlalchemy.column(OWNER_COLUMN, sqlalchemy.String),
)
words_match = sqlalchemy.literal_column(WORDS_TABLE).op("MATCH")
words_rank = sqlalchemy.func.bm25(sqlalchemy.literal_column(WORDS_TABLE))
RANK_LABEL = "rank"
MATCHED_LABEL = "matched"
MATERIALIZED = "MATERIALIZED"

logger = logging.getLogger(__name__)

SearchLimit = Annotated[int, Field(ge=1)]
# A word or pair of letters that more than this share of the owner's blocks hold, as "of" and
# "the" in English, tells little of which block is sought: a block is not found by some of the
# words for holding it. In a library of six English Wikipedia pages "of" stood in 48% of the
# blocks and "on" in 18%; in one of eight pages in six languages, where English is one page in
# eight, "of" stood in 16%. Words of one page's subject stand in as much as 17%, and are then
# left out too, though a block that holds every word of the query is found by them.
COMMON_SHARE = 0.1
# In a small library every word stands in a large share of the blocks; a word that no more
# than this many blocks hold is never taken for a common one.
COMMON_FLOOR = 10
INDEX_FAILED = "the search of the library has failed; infonium must be started again"
# What a lone surrogate, which no text can hold, becomes in a query; a NUL, which ends the
# full-text match's reading of a word, parts words as a space does.
UNREADABLE = "replace"
NUL = "\x00"
UTF16 = "utf-16"
SURROGATES_KEPT = "surrogatepass"
# The part of SQLite's code for a failure that says the file was busy; the rest of the code
# says how.
PRIMARY_CODE = 0xFF


def readable_query(query: str) -> str:
    # A query that comes as JSON may hold a lone surrogate, which the engine cannot be sent.
    readable = query.encode(UTF16, SURROGATES_KEPT).decode(UTF16, UNREADABLE)
    return readable.replace(NUL, WORD_GAP)


SearchQuery = Annotated[str, AfterValidator(readable_query)]


def is_busy(error: sqlalchemy.exc.OperationalError) -> bool:
    return (
        isinstance(error.orig, sqlite3.Error)
        and error.orig.sqlite_errorcode & PRIMARY_CODE == sqlite3.SQLITE_BUSY
    )


class IndexFailed(Exception):
    # The index cannot search the library until infonium starts again: its work stopped on a
    # fault, or the engine refused a query in a way that waiting does not mend. The cause is
    # kept as the cause; the message names no address.
    pass


class FoundBy(StrEnum):
    # How a block was found: it holds every word of the query; it holds some of them; it is
    # among the nearest to the query in meaning; or it holds every word and is near as well.
    ALL_WORDS = "all_words"
    SOME_WORDS = "some_words"
    MEANING = "meaning"
    ALL_WORDS_AND_MEANING = "all_words_and_meaning"


class FoundBlock(BaseModel):
    document: str
    item: str
    title: str
    block_number: BlockNumber
    text: str
    found_by: FoundBy


class RankedBlock(BaseModel):
    id: int
    found_by: FoundBy


class WaitingBlock(BaseModel):
    id: int
    title: str
    text: str


def create_words(
    target: sqlalchemy.MetaData, connection: sqlalchemy.Connection, **kw: object
) -> None:
    # SQLite 3.51 leaves the table of owners behind when the words are dropped, and will not
    # make the words again beside it.
    if not sqlalchemy.inspect(connection).has_table(WORDS_TABLE):
        connection.exec_driver_sql(DROP_LEFT_OWNERS)
        connection.exec_driver_sql(CREATE_WORDS)


sqlalchemy.event.listen(metadata, "after_create", create_words)


def is_meaningful(block: Block) -> bool:
    return block.kind != BlockKind.HEADING and word_total(block.text) >= MEANINGFUL_WORDS


def found_by(holds_every_word: bool, near_in_meaning: bool) -> FoundBy:
    match holds_every_word, near_in_meaning:
        case True, True:
            return FoundBy.ALL_WORDS_AND_MEANING
        case True, False:
            return FoundBy.ALL_WORDS
        case False, True:
            return FoundBy.MEANING
        case _:
            return FoundBy.SOME_WORDS


def ranked_blocks(
    every_word: list[int], meaning: list[int], some_words: list[int], limit: int
) -> list[RankedBlock]:
    # The three rankings joined by reciprocal rank fusion, with every block that holds every
    # word of the query above every block that does not.
    holding = set(every_word)
    near = set(meaning)
    scores: dict[int, float] = {}
    for ranking in (every_word, meaning, [block for block in some_words if block not in holding]):
        for place, block_id in enumerate(ranking, start=1):
            scores[block_id] = scores.get(block_id, 0.0) + 1 / (RANK_DAMPING + place)
    best = sorted(scores, key=lambda block_id: (block_id not in holding, -scores[block_id]))
    return [
        RankedBlock(id=block_id, found_by=found_by(block_id in holding, block_id in near))
        for block_id in best[:limit]
    ]


def vector_bytes(vector: Vector) -> bytes:
    return array(FLOATS, vector).tobytes()


def without_vector(model: str) -> sqlalchemy.ColumnElement[bool]:
    return indexed_block_table.c.meaningful & indexed_block_table.c.vector_model.is_distinct_from(
        model
    )


def found_by_words(
    transaction: OwnerTransaction,
    expression: str,
    *conditions: sqlalchemy.ColumnElement[bool],
    cap: int = CANDIDATES,
) -> list[int]:
    # The words are matched first, by themselves: joined to the blocks in one query, SQLite
    # would match them again for every block of the owner.
    if not expression:
        return []
    matched = (
        sqlalchemy.select(words_table.c.rowid, words_rank.label(RANK_LABEL))
        .where(words_match(expression), words_table.c.owner == transaction.owner)
        .cte(MATCHED_LABEL)
        .prefix_with(MATERIALIZED)
    )
    return list(
        transaction.connection.execute(
            sqlalchemy.select(matched.c.rowid)
            .join(indexed_block_table, indexed_block_table.c.id == matched.c.rowid)
            .where(indexed_block_table.c.owner == transaction.owner, *conditions)
            .order_by(matched.c[RANK_LABEL])
            .limit(cap)
        ).scalars()
    )


def uncommon_pieces(transaction: OwnerTransaction, pieces: list[str]) -> list[str]:
    # The pieces that no more than the common share of the owner's blocks that meaning may find
    # hold; a piece's blocks are counted up to one past the bound.
    bound = max(
        COMMON_FLOOR,
        int(
            COMMON_SHARE
            * transaction.connection.execute(
                sqlalchemy.select(sqlalchemy.func.count())
                .select_from(indexed_block_table)
                .where(
                    indexed_block_table.c.owner == transaction.owner,
                    indexed_block_table.c.meaningful,
                )
            ).scalar_one()
        ),
    )
    return [
        piece
        for piece in pieces
        if len(found_by_words(transaction, piece, indexed_block_table.c.meaningful, cap=bound + 1))
        <= bound
    ]


def found_by_meaning(transaction: OwnerTransaction, model: str, vector: Vector) -> list[int]:
    return list(
        transaction.connection.execute(
            sqlalchemy.select(indexed_block_table.c.id)
            .where(
                indexed_block_table.c.owner == transaction.owner,
                indexed_block_table.c.vector_model == model,
            )
            .order_by(
                sqlalchemy.func.vec_distance_cosine(
                    indexed_block_table.c.embedding, vector_bytes(vector)
                )
            )
            .limit(CANDIDATES)
        ).scalars()
    )


def found_blocks(transaction: OwnerTransaction, ranked: list[RankedBlock]) -> list[FoundBlock]:
    # Only blocks of the document an item of the owner holds now are given, whatever the index
    # still holds of an earlier one.
    rows = transaction.connection.execute(
        sqlalchemy.select(
            indexed_block_table.c.id,
            indexed_block_table.c.document_id,
            indexed_block_table.c.item_id,
            kept_title,
            indexed_block_table.c.block_number,
            indexed_block_table.c.text,
        )
        .join(item_table, item_table.c.id == indexed_block_table.c.item_id)
        .where(
            indexed_block_table.c.id.in_([block.id for block in ranked]),
            indexed_block_table.c.owner == transaction.owner,
            item_table.c.owner == transaction.owner,
            kept_document_id == indexed_block_table.c.document_id,
        )
    ).all()
    kept = {row[0]: row[1:] for row in rows}
    return [
        FoundBlock(
            document=document,
            item=item,
            title=title,
            block_number=number,
            text=text,
            found_by=block.found_by,
        )
        for block in ranked
        if block.id in kept
        for document, item, title, number, text in [kept[block.id]]
    ]


def is_whole(transaction: OwnerTransaction) -> bool:
    # The owner's part of the index is whole when its documents, its blocks and its words
    # count alike; when one of its tables was thrown away they do not.
    owner = transaction.owner
    counted = (
        sqlalchemy.select(
            sqlalchemy.func.coalesce(sqlalchemy.func.sum(indexed_document_table.c.blocks), 0)
        )
        .where(indexed_document_table.c.owner == owner)
        .scalar_subquery(),
        sqlalchemy.select(sqlalchemy.func.count())
        .select_from(indexed_block_table)
        .where(indexed_block_table.c.owner == owner)
        .scalar_subquery(),
        sqlalchemy.select(sqlalchemy.func.count())
        .select_from(words_table)
        .where(words_table.c.owner == owner)
        .scalar_subquery(),
    )
    return len(set(transaction.connection.execute(sqlalchemy.select(*counted)).one())) == 1


def take_out_all(transaction: OwnerTransaction) -> None:
    owner = transaction.owner
    connection = transaction.connection
    connection.execute(sqlalchemy.delete(words_table).where(words_table.c.owner == owner))
    connection.execute(
        sqlalchemy.delete(indexed_block_table).where(indexed_block_table.c.owner == owner)
    )
    connection.execute(
        sqlalchemy.delete(indexed_document_table).where(indexed_document_table.c.owner == owner)
    )


def take_out(transaction: OwnerTransaction, document: str) -> None:
    block_ids = (
        transaction.connection.execute(
            sqlalchemy.select(indexed_block_table.c.id).where(
                indexed_block_table.c.owner == transaction.owner,
                indexed_block_table.c.document_id == document,
            )
        )
        .scalars()
        .all()
    )
    if block_ids:
        transaction.connection.execute(
            sqlalchemy.delete(words_table).where(words_table.c.rowid.in_(block_ids))
        )
    # The document's blocks go with it.
    transaction.connection.execute(
        sqlalchemy.delete(indexed_document_table).where(
            indexed_document_table.c.owner == transaction.owner,
            indexed_document_table.c.document_id == document,
        )
    )


def take_in(transaction: OwnerTransaction, document_id: str) -> None:
    owner = transaction.owner
    connection = transaction.connection
    body = connection.execute(
        sqlalchemy.select(document_table.c.body).where(
            document_table.c.owner == owner, document_table.c.id == document_id
        )
    ).scalar_one()
    document = Document.model_validate_json(body)
    worded = [
        (number, block) for number, block in enumerate(document.blocks) if has_word(block.text)
    ]
    connection.execute(
        sqlalchemy.insert(indexed_document_table).values(
            document_id=document.id,
            owner=owner,
            item_id=document.item_id,
            format=INDEX_FORMAT,
            blocks=len(worded),
        )
    )
    if not worded:
        return
    block_ids = (
        connection.execute(
            sqlalchemy.insert(indexed_block_table).returning(
                indexed_block_table.c.id, sort_by_parameter_order=True
            ),
            [
                {
                    "owner": owner,
                    "document_id": document.id,
                    "item_id": document.item_id,
                    "block_number": number,
                    "text": block.text,
                    "meaningful": is_meaningful(block),
                }
                for number, block in worded
            ],
        )
        .scalars()
        .all()
    )
    connection.execute(
        sqlalchemy.insert(words_table),
        [
            {"rowid": block_id, WORDS_COLUMN: indexed_words(block.text), OWNER_COLUMN: owner}
            for block_id, (_, block) in zip(block_ids, worded, strict=True)
        ],
    )


class BlockIndex:
    # The index of the blocks of every document the library's items hold now, searched by
    # their words and by their meaning, in tables of its own in the library's file that it can
    # build again from the documents. A search first indexes the words of the owner's documents
    # that it does not hold yet. A worker does the same for every owner, and has the engine make
    # vectors of the blocks that wait for them, a batch of each owner's at a time, while the
    # engine answers; it rests when nothing waits.
    def __init__(self, store: LibraryStore, engine: EmbeddingEngine) -> None:
        self.store = store
        self.embedder = Embedder(engine)
        # A document is taken into the index, or out of it, by one thread at a time.
        self.turn = Lock()
        self.stopped = Event()
        self.woken = Event()
        self.engine_answers = True
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="indexer")
        # No worker runs until the index is resumed.
        self.worker: Future[None] = Future()

    def resume(self) -> None:
        # An owner's part of the index that is not whole is built again from nothing.
        for owner in self.store.owners():
            with self.turn, self.store.transaction(owner) as transaction:
                if not is_whole(transaction):
                    logger.warning(
                        "the index of %s's blocks is not whole; it is built again", owner
                    )
                    take_out_all(transaction)
        self.worker = self.pool.submit(self.work)
        self.worker.add_done_callback(self.record)

    def close(self) -> None:
        self.stopped.set()
        self.woken.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def record(self, finished: Future[None]) -> None:
        error = None if finished.cancelled() else finished.exception()
        if error is not None:
            logger.error(
                "the index of blocks stopped; every search fails until infonium starts again",
                exc_info=error,
            )

    def raise_failure(self) -> None:
        # A worker that failed fails every search after it, so that a fault is not taken for a
        # library that holds nothing.
        if self.worker.done() and not self.worker.cancelled():
            failure = self.worker.exception()
            if failure is not None:
                raise IndexFailed(INDEX_FAILED) from failure

    def work(self) -> None:
        while not self.stopped.is_set():
            if not self.work_round():
                self.woken.wait(CATCH_UP_SECONDS)
                self.woken.clear()

    def work_round(self) -> bool:
        # One round of catching up and of making vectors; whether vectors were made. A library
        # file that another writer holds past the wait leaves the work for the next round.
        try:
            owners = self.store.owners()
            for owner in owners:
                self.catch_up(owner)
            return self.embed_round(owners)
        except sqlalchemy.exc.OperationalError as error:
            if not is_busy(error):
                raise
            logger.warning("the library's file is busy, so indexing waits a round: %s", error.orig)
            return False

    def catch_up(self, owner: str) -> None:
        # The owner's documents that items hold now are indexed, and those that no item holds
        # any more are taken out, each document in a transaction of its own.
        with self.store.transaction(owner) as transaction:
            held = set(
                transaction.connection.execute(
                    sqlalchemy.select(kept_document_id).where(
                        item_table.c.owner == owner, kept_document_id.is_not(None)
                    )
                ).scalars()
            )
            indexed = {
                document: format
                for document, format in transaction.connection.execute(
                    sqlalchemy.select(
                        indexed_document_table.c.document_id, indexed_document_table.c.format
                    ).where(indexed_document_table.c.owner == owner)
                )
            }
        for document, format in indexed.items():
            if document not in held or format != INDEX_FORMAT:
                with self.turn, self.store.transaction(owner) as transaction:
                    take_out(transaction, document)
        missing = [document for document in held if indexed.get(document) != INDEX_FORMAT]
        for document in missing:
            with self.turn, self.store.transaction(owner) as transaction:
                if not self.is_indexed(transaction, document):
                    take_in(transaction, document)
        if missing:
            self.woken.set()

    def is_indexed(self, transaction: OwnerTransaction, document: str) -> bool:
        # Another thread may have indexed the document since it was found missing.
        return (
            transaction.connection.execute(
                sqlalchemy.select(indexed_document_table.c.format).where(
                    indexed_document_table.c.document_id == document,
                    indexed_document_table.c.format == INDEX_FORMAT,
                )
            ).first()
            is not None
        )

    def embed_round(self, owners: list[str]) -> bool:
        # One batch of each owner's blocks that wait for vectors; whether any was made. An engine
        # that is away leaves the blocks waiting for a later round.
        try:
            made = [self.embed_batch(owner) for owner in owners]
        except ENGINE_AWAY as failure:
            if self.engine_answers:
                logger.warning(
                    "the embedding engine is away, so blocks wait for vectors: %s",
                    failure_reason(failure),
                )
            self.engine_answers = False
            return False
        if any(made):
            if not self.engine_answers:
                logger.info("the embedding engine answers again")
            self.engine_answers = True
        return any(made)

    def embed_batch(self, owner: str) -> bool:
        # The blocks indexed last are given vectors first, so that what was read just now is
        # not kept waiting behind a library that is being caught up.
        model = self.embedder.model
        with self.store.transaction(owner) as transaction:
            waiting = [
                WaitingBlock(id=block_id, title=title, text=text)
                for block_id, title, text in transaction.connection.execute(
                    sqlalchemy.select(
                        indexed_block_table.c.id, kept_title, indexed_block_table.c.text
                    )
                    .join(item_table, item_table.c.id == indexed_block_table.c.item_id)
                    .where(
                        indexed_block_table.c.owner == owner,
                        item_table.c.owner == owner,
                        without_vector(model),
                    )
                    .order_by(indexed_block_table.c.id.desc())
                    .limit(EMBEDDING_BATCH)
                )
            ]
        if not waiting:
            return False
        vectors = self.embedder.embedded(
            [f"{block.title}{TITLE_GAP}{block.text}" for block in waiting],
            EMBEDDING_TIMEOUT_SECONDS,
        )
        # A vector is kept only for the block whose text was sent: ids are never given again,
        # and the text must be the same still.
        with self.store.transaction(owner) as transaction:
            transaction.connection.execute(
                sqlalchemy.update(indexed_block_table)
                .where(
                    indexed_block_table.c.id == sqlalchemy.bindparam("block_id"),
                    indexed_block_table.c.owner == owner,
                    indexed_block_table.c.text == sqlalchemy.bindparam("sent"),
                )
                .values(vector_model=model, embedding=sqlalchemy.bindparam("vector")),
                [
                    {"block_id": block.id, "sent": block.text, "vector": vector_bytes(vector)}
                    for block, vector in zip(waiting, vectors, strict=True)
                ],
            )
        return True

    @validate_call
    def search(self, owner: str, query: SearchQuery, limit: SearchLimit) -> list[FoundBlock]:
        # The best blocks of the owner's library for a query, best first: those that hold every
        # word of it, then those near it in meaning, where the engine answers, and those without
        # a vector to be near by that hold some of its words.
        self.raise_failure()
        self.catch_up(owner)
        model = self.embedder.model
        with self.store.transaction(owner) as transaction:
            has_vectors = transaction.connection.execute(
                sqlalchemy.select(indexed_block_table.c.id).where(
                    indexed_block_table.c.owner == owner,
                    indexed_block_table.c.vector_model == model,
                )
            ).first()
        query_vectors = self.query_vectors(query) if has_vectors else []
        with self.store.transaction(owner) as transaction:
            every_word = found_by_words(transaction, all_words_expression(query))
            meaning = [
                block
                for vector in query_vectors
                for block in found_by_meaning(transaction, model, vector)
            ]
            some_words = found_by_words(
                transaction,
                any_words_expression(uncommon_pieces(transaction, sought_pieces(query))),
                without_vector(model) if query_vectors else indexed_block_table.c.meaningful,
            )
            return found_blocks(transaction, ranked_blocks(every_word, meaning, some_words, limit))

    def query_vectors(self, query: str) -> list[Vector]:
        # The vector of the query, or none while the engine is away.
        if not query.strip():
            return []
        try:
            return self.embedder.embedded([query], QUERY_TIMEOUT_SECONDS)
        except ENGINE_AWAY as failure:
            logger.info(
                "searching without meaning, as the embedding engine is away: %s",
                failure_reason(failure),
            )
            return []
        except openai.APIError as failure:
            logger.error("the embedding engine refused a query", exc_info=failure)
            raise IndexFailed(INDEX_FAILED) from failure
