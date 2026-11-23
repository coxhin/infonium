import logging
import sqlite3
import unicodedata
from array import array
from itertools import groupby
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Final

import openai
import sqlalchemy
import sqlite_vec
from pydantic import BaseModel, SecretStr

from infonium.document import UNSPACED_CODE_POINTS, WORD_GAP, BlockNumber, Document
from infonium.library import (
    LIBRARY_FILE,
    document_table,
    item_table,
    kept_document_id,
    kept_title,
    metadata,
)

# How the words of a block are written into the index. A document indexed in another form is
# indexed again.
WORDS_FORMAT = 1
# The writings that put no space between words: those of Chinese and Japanese, and Thai, Lao,
# Burmese and Khmer. A run of letters in them is indexed as the pairs of neighbours it holds,
# so that any two or more letters side by side in it are found.
UNSPACED_WRITING = (
    *UNSPACED_CODE_POINTS,
    range(0x0E00, 0x0F00),
    range(0x1000, 0x10A0),
    range(0x1780, 0x1800),
)
# The kinds of character that make up words, as the index's tokenizer takes them: letters,
# marks and numbers.
WORD_CATEGORIES = frozenset("LMN")
PHRASE_QUOTE = '"'
ANY_ENDING = "*"
EITHER = " OR "
# How many blocks each way of finding brings to the one ranking, and how much the first
# places of a way count above its later ones: a block's score is the sum, over the ways that
# found it, of one over this number plus its place (reciprocal rank fusion).
CANDIDATES = 50
RANK_DAMPING = 60
# Blocks are sent to the engine this many at a time, and of each block at most this much text.
EMBEDDING_BATCH = 32
EMBEDDED_CHARACTERS = 2000
DOCUMENTS_AT_ONCE = 16
# The longest the engine may take over a batch of blocks, and over a query, which a search
# waits for.
EMBEDDING_TIMEOUT_SECONDS = 120.0
QUERY_TIMEOUT_SECONDS = 10.0
FLOATS = "f"
FLOAT_ENCODING: Final = "float"
# How often the index looks for documents it does not hold yet and for blocks without
# vectors, which is also how soon it notices that an engine that was away is back.
CATCH_UP_SECONDS = 10.0
CONNECT = "connect"

WORDS_TABLE = "search_words"
VECTOR_TABLE = "search_vector"

indexed_document_table = sqlalchemy.Table(
    "search_document",
    metadata,
    sqlalchemy.Column("document_id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column("item_id", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
)
# Each block that holds words, under an id that is also its row in the words and the vectors.
indexed_block_table = sqlalchemy.Table(
    "search_block",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("document_id", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column("item_id", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("block_number", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("text", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("embedded", sqlalchemy.Boolean, nullable=False, index=True),
)
# The model the vectors were made by, and how many numbers each has; no row while there are
# no vectors. The vector table exists exactly while this row does.
vector_model_table = sqlalchemy.Table(
    "search_vector_model",
    metadata,
    sqlalchemy.Column("model", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("dimensions", sqlalchemy.Integer, nullable=False),
)
# The words are kept only as the index's terms: a block's text is kept with the block.
CREATE_WORDS = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {WORDS_TABLE} USING fts5(words, content='', "
    "contentless_delete=1, tokenize='unicode61 remove_diacritics 2')"
)
CREATE_VECTORS = (
    f"CREATE VIRTUAL TABLE {VECTOR_TABLE} USING vec0(owner text partition key, "
    "embedding float[{dimensions}] distance_metric=cosine)"
)
DROP_VECTORS = sqlalchemy.text(f"DROP TABLE IF EXISTS {VECTOR_TABLE}")
INSERT_WORDS = sqlalchemy.text(f"INSERT INTO {WORDS_TABLE}(rowid, words) VALUES (:id, :words)")
DELETE_WORDS = sqlalchemy.text(f"DELETE FROM {WORDS_TABLE} WHERE rowid = :id")
CLEAR_WORDS = sqlalchemy.text(f"DELETE FROM {WORDS_TABLE}")
COUNT_WORDS = sqlalchemy.text(f"SELECT count(*) FROM {WORDS_TABLE}")
INSERT_VECTOR = sqlalchemy.text(
    f"INSERT INTO {VECTOR_TABLE}(rowid, owner, embedding) VALUES (:id, :owner, :embedding)"
)
DELETE_VECTOR = sqlalchemy.text(f"DELETE FROM {VECTOR_TABLE} WHERE rowid = :id")
COUNT_VECTORS = sqlalchemy.text(f"SELECT count(*) FROM {VECTOR_TABLE}")
FIND_BY_WORDS = sqlalchemy.text(
    f"SELECT {WORDS_TABLE}.rowid FROM {WORDS_TABLE} "
    f"JOIN {indexed_block_table.name} ON {indexed_block_table.name}.id = {WORDS_TABLE}.rowid "
    f"WHERE {WORDS_TABLE} MATCH :expression AND {indexed_block_table.name}.owner = :owner "
    f"ORDER BY bm25({WORDS_TABLE}) LIMIT :candidates"
)
FIND_BY_MEANING = sqlalchemy.text(
    f"SELECT rowid FROM {VECTOR_TABLE} WHERE embedding MATCH :embedding AND k = :candidates "
    "AND owner = :owner ORDER BY distance"
)

logger = logging.getLogger(__name__)


class FoundBlock(BaseModel):
    document: str
    item: str
    title: str
    block_number: BlockNumber
    text: str


class EmbeddingEngine(BaseModel):
    # Where an engine behind OpenAI's embeddings API is reached, the key it asks for, and the
    # model it embeds with.
    base_url: str
    api_key: SecretStr
    model: str


class IndexedBlock(BaseModel):
    id: int
    owner: str
    text: str


def is_unspaced_letter(character: str) -> bool:
    return unicodedata.category(character)[0] in WORD_CATEGORIES and any(
        ord(character) in code_points for code_points in UNSPACED_WRITING
    )


def has_word(text: str) -> bool:
    return any(unicodedata.category(character)[0] in WORD_CATEGORIES for character in text)


def neighbour_pairs(run: str) -> list[str]:
    return [run] if len(run) == 1 else [run[at : at + 2] for at in range(len(run) - 1)]


def indexed_words(text: str) -> str:
    # The text as the index takes it: each run of letters of an unspaced writing becomes the
    # pairs of neighbours it holds, apart from what stands around it; other text is left for
    # the tokenizer to part at its spaces and punctuation.
    return WORD_GAP.join(
        WORD_GAP.join(neighbour_pairs(run)) if unspaced else run
        for unspaced, characters in groupby(text, key=is_unspaced_letter)
        for run in ["".join(characters)]
    )


def phrase(words: str) -> str:
    # A quoted phrase is taken as words alone, so nothing of a query is read as the index's
    # syntax.
    return f"{PHRASE_QUOTE}{words.replace(PHRASE_QUOTE, PHRASE_QUOTE * 2)}{PHRASE_QUOTE}"


def match_expression(query: str) -> str:
    # A block is found by any word of the query, and ranks higher the more it holds. A part of
    # the query between spaces is sought whole, as a phrase, and also by each word or pair of
    # letters in it, since a question in Chinese or Japanese is one such part. A lone letter
    # of an unspaced writing is sought at the start of a pair as well.
    terms: list[str] = []
    for part in query.split():
        pieces = [piece for piece in indexed_words(part).split() if has_word(piece)]
        if len(pieces) > 1:
            terms.append(phrase(WORD_GAP.join(pieces)))
        terms.extend(phrase(piece) for piece in pieces)
        terms.extend(
            f"{phrase(piece)} {ANY_ENDING}"
            for piece in pieces
            if len(piece) == 1 and is_unspaced_letter(piece)
        )
    return EITHER.join(dict.fromkeys(terms))


def fused(rankings: list[list[int]], limit: int) -> list[int]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for place, block_id in enumerate(ranking, start=1):
            scores[block_id] = scores.get(block_id, 0.0) + 1 / (RANK_DAMPING + place)
    return sorted(scores, key=lambda block_id: -scores[block_id])[:limit]


def vector_bytes(vector: list[float]) -> bytes:
    return array(FLOATS, vector).tobytes()


def create_words(
    target: sqlalchemy.MetaData, connection: sqlalchemy.Connection, **kw: object
) -> None:
    connection.exec_driver_sql(CREATE_WORDS)


sqlalchemy.event.listen(metadata, "after_create", create_words)


def load_vectors(connection: sqlite3.Connection, record: object) -> None:
    connection.enable_load_extension(True)
    sqlite_vec.load(connection)
    connection.enable_load_extension(False)


class BlockIndex:
    # The index of the blocks of every document the library's items hold now, searched by
    # their words and by their meaning. It is kept in the library's file, in tables of its own
    # that it can build again from the documents. Words are indexed before a search and by a
    # worker that catches up every few seconds; the worker also has the engine make vectors
    # of the blocks that wait for them, while the engine answers.
    def __init__(self, directory: Path, engine: EmbeddingEngine) -> None:
        self.database = sqlalchemy.create_engine(f"sqlite:///{directory / LIBRARY_FILE}")
        sqlalchemy.event.listen(self.database, CONNECT, load_vectors)
        self.embedding_model = engine.model
        self.endpoint = openai.OpenAI(
            base_url=engine.base_url,
            api_key=engine.api_key.get_secret_value(),
            timeout=EMBEDDING_TIMEOUT_SECONDS,
            max_retries=0,
        )
        # The index is changed by one thread at a time.
        self.turn = Lock()
        self.stopped = Event()
        self.woken = Event()
        self.engine_answers = True
        self.worker = Thread(target=self.work, name="indexer", daemon=True)

    def resume(self) -> None:
        # An index that is not whole, as when some of its tables were thrown away, is built
        # again; vectors of another model than the one set are made again.
        metadata.create_all(self.database)
        with self.turn, self.database.begin() as connection:
            blocks = connection.execute(
                sqlalchemy.select(sqlalchemy.func.count()).select_from(indexed_block_table)
            ).scalar_one()
            if connection.execute(COUNT_WORDS).scalar_one() != blocks:
                connection.execute(CLEAR_WORDS)
                connection.execute(sqlalchemy.delete(indexed_block_table))
                connection.execute(sqlalchemy.delete(indexed_document_table))
            if self.vectors_are_stale(connection):
                self.forget_vectors(connection)
        self.worker.start()

    def close(self) -> None:
        self.stopped.set()
        self.woken.set()

    def vectors_are_stale(self, connection: sqlalchemy.Connection) -> bool:
        # Vectors are of no use when they are of another model, when their table is gone, or
        # when they are not those of the blocks marked as having them.
        models = list(connection.execute(sqlalchemy.select(vector_model_table.c.model)).scalars())
        embedded = connection.execute(
            sqlalchemy.select(sqlalchemy.func.count())
            .select_from(indexed_block_table)
            .where(indexed_block_table.c.embedded)
        ).scalar_one()
        if models != [self.embedding_model] or not sqlalchemy.inspect(connection).has_table(
            VECTOR_TABLE
        ):
            return bool(models) or embedded > 0
        return bool(connection.execute(COUNT_VECTORS).scalar_one() != embedded)

    def forget_vectors(self, connection: sqlalchemy.Connection) -> None:
        connection.execute(DROP_VECTORS)
        connection.execute(sqlalchemy.delete(vector_model_table))
        connection.execute(sqlalchemy.update(indexed_block_table).values(embedded=False))

    def work(self) -> None:
        while not self.stopped.is_set():
            # The worker goes on after anything goes wrong in a round: the next round tries
            # again.
            try:
                with self.database.connect() as connection:
                    owners = connection.execute(
                        sqlalchemy.select(item_table.c.owner).distinct()
                    ).scalars()
                    owners_now = list(owners)
                for owner in owners_now:
                    self.catch_up(owner)
                self.embed_waiting()
            except Exception:
                logger.exception("indexing the library's blocks failed; it is tried again")
            self.woken.wait(CATCH_UP_SECONDS)
            self.woken.clear()

    def catch_up(self, owner: str) -> None:
        # The owner's documents that items hold now are indexed, and those that no item holds
        # any more are taken out.
        held_documents = sqlalchemy.select(kept_document_id).where(
            item_table.c.owner == owner, kept_document_id.is_not(None)
        )
        indexed_documents = sqlalchemy.select(
            indexed_document_table.c.document_id, indexed_document_table.c.format
        ).where(indexed_document_table.c.owner == owner)
        with self.turn:
            with self.database.connect() as connection:
                held = set(connection.execute(held_documents).scalars())
                indexed = {
                    document: format for document, format in connection.execute(indexed_documents)
                }
            gone = [
                document
                for document, format in indexed.items()
                if document not in held or format != WORDS_FORMAT
            ]
            missing = [document for document in held if indexed.get(document) != WORDS_FORMAT]
            if gone:
                with self.database.begin() as connection:
                    self.take_out(connection, gone)
            for first in range(0, len(missing), DOCUMENTS_AT_ONCE):
                with self.database.begin() as connection:
                    self.take_in(connection, owner, missing[first : first + DOCUMENTS_AT_ONCE])
        if missing:
            self.woken.set()

    def take_out(self, connection: sqlalchemy.Connection, documents: list[str]) -> None:
        block_ids = connection.execute(
            sqlalchemy.select(indexed_block_table.c.id, indexed_block_table.c.embedded).where(
                indexed_block_table.c.document_id.in_(documents)
            )
        ).all()
        if block_ids:
            connection.execute(DELETE_WORDS, [{"id": block_id} for block_id, _ in block_ids])
        embedded = [{"id": block_id} for block_id, has_vector in block_ids if has_vector]
        if embedded:
            connection.execute(DELETE_VECTOR, embedded)
        connection.execute(
            sqlalchemy.delete(indexed_block_table).where(
                indexed_block_table.c.document_id.in_(documents)
            )
        )
        connection.execute(
            sqlalchemy.delete(indexed_document_table).where(
                indexed_document_table.c.document_id.in_(documents)
            )
        )

    def take_in(self, connection: sqlalchemy.Connection, owner: str, documents: list[str]) -> None:
        bodies = (
            connection.execute(
                sqlalchemy.select(document_table.c.body).where(
                    document_table.c.owner == owner, document_table.c.id.in_(documents)
                )
            )
            .scalars()
            .all()
        )
        for document in (Document.model_validate_json(body) for body in bodies):
            worded = [
                (number, block.text)
                for number, block in enumerate(document.blocks)
                if has_word(block.text)
            ]
            if worded:
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
                                "text": text,
                                "embedded": False,
                            }
                            for number, text in worded
                        ],
                    )
                    .scalars()
                    .all()
                )
                connection.execute(
                    INSERT_WORDS,
                    [
                        {"id": block_id, "words": indexed_words(text)}
                        for block_id, (_, text) in zip(block_ids, worded, strict=True)
                    ],
                )
            connection.execute(
                sqlalchemy.insert(indexed_document_table).values(
                    document_id=document.id,
                    owner=owner,
                    item_id=document.item_id,
                    format=WORDS_FORMAT,
                )
            )

    def embed_waiting(self) -> None:
        # Blocks without vectors are sent to the engine a batch at a time, until none waits or
        # the engine does not answer; then they wait for the next round.
        while not self.stopped.is_set():
            with self.database.connect() as connection:
                waiting = [
                    IndexedBlock(id=block_id, owner=owner, text=text)
                    for block_id, owner, text in connection.execute(
                        sqlalchemy.select(
                            indexed_block_table.c.id,
                            indexed_block_table.c.owner,
                            indexed_block_table.c.text,
                        )
                        .where(sqlalchemy.not_(indexed_block_table.c.embedded))
                        .order_by(indexed_block_table.c.id)
                        .limit(EMBEDDING_BATCH)
                    )
                ]
            if not waiting:
                return
            try:
                vectors = self.embedded(
                    [block.text for block in waiting], EMBEDDING_TIMEOUT_SECONDS
                )
            except openai.APIError as failure:
                if self.engine_answers:
                    logger.warning(
                        "the embedding engine did not answer, so blocks wait for vectors: %s",
                        failure,
                    )
                self.engine_answers = False
                return
            if not self.engine_answers:
                logger.info("the embedding engine answers again")
            self.engine_answers = True
            self.keep_vectors(waiting, vectors)

    def embedded(self, texts: list[str], timeout: float) -> list[list[float]]:
        reply = self.endpoint.with_options(timeout=timeout).embeddings.create(
            model=self.embedding_model,
            input=[text[:EMBEDDED_CHARACTERS] for text in texts],
            encoding_format=FLOAT_ENCODING,
        )
        return [vector.embedding for vector in sorted(reply.data, key=lambda row: row.index)]

    def keep_vectors(self, blocks: list[IndexedBlock], vectors: list[list[float]]) -> None:
        # A block taken out of the index while its vector was made gets none.
        dimensions = len(vectors[0])
        with self.turn, self.database.begin() as connection:
            kept = connection.execute(
                sqlalchemy.select(vector_model_table.c.model, vector_model_table.c.dimensions)
            ).all()
            if kept != [(self.embedding_model, dimensions)]:
                self.forget_vectors(connection)
                connection.execute(
                    sqlalchemy.insert(vector_model_table).values(
                        model=self.embedding_model, dimensions=dimensions
                    )
                )
                connection.exec_driver_sql(CREATE_VECTORS.format(dimensions=dimensions))
            present = set(
                connection.execute(
                    sqlalchemy.select(indexed_block_table.c.id).where(
                        indexed_block_table.c.id.in_([block.id for block in blocks]),
                        sqlalchemy.not_(indexed_block_table.c.embedded),
                    )
                ).scalars()
            )
            connection.execute(
                INSERT_VECTOR,
                [
                    {"id": block.id, "owner": block.owner, "embedding": vector_bytes(vector)}
                    for block, vector in zip(blocks, vectors, strict=True)
                    if block.id in present
                ],
            )
            connection.execute(
                sqlalchemy.update(indexed_block_table)
                .where(indexed_block_table.c.id.in_(present))
                .values(embedded=True)
            )

    def search(self, owner: str, query: str, limit: int) -> list[FoundBlock]:
        # The best blocks of the owner's library for a query, by its words and, where the
        # engine answers, by its meaning, best first.
        self.catch_up(owner)
        expression = match_expression(query)
        with self.database.connect() as connection:
            by_words = (
                list(
                    connection.execute(
                        FIND_BY_WORDS,
                        {"expression": expression, "owner": owner, "candidates": CANDIDATES},
                    ).scalars()
                )
                if expression
                else []
            )
            has_vectors = connection.execute(
                sqlalchemy.select(vector_model_table.c.model).where(
                    vector_model_table.c.model == self.embedding_model
                )
            ).first()
        by_meaning = self.found_by_meaning(owner, query) if has_vectors else []
        ranked = fused([by_words, by_meaning], limit)
        return self.found_blocks(owner, ranked)

    def found_by_meaning(self, owner: str, query: str) -> list[int]:
        # A search that the engine does not answer goes on by words alone.
        try:
            vector = self.embedded([query], QUERY_TIMEOUT_SECONDS)[0]
        except openai.APIError as failure:
            logger.info(
                "searching by words alone, as the embedding engine did not answer: %s", failure
            )
            return []
        with self.database.connect() as connection:
            return list(
                connection.execute(
                    FIND_BY_MEANING,
                    {"embedding": vector_bytes(vector), "candidates": CANDIDATES, "owner": owner},
                ).scalars()
            )

    def found_blocks(self, owner: str, ranked: list[int]) -> list[FoundBlock]:
        # Only blocks of the document an item of the owner holds now are given, whatever the
        # index still holds of an earlier one.
        with self.database.connect() as connection:
            rows = connection.execute(
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
                    indexed_block_table.c.id.in_(ranked),
                    indexed_block_table.c.owner == owner,
                    item_table.c.owner == owner,
                    kept_document_id == indexed_block_table.c.document_id,
                )
            ).all()
        found = {
            block_id: FoundBlock(
                document=document, item=item, title=title, block_number=number, text=text
            )
            for block_id, document, item, title, number, text in rows
        }
        return [found[block_id] for block_id in ranked if block_id in found]
