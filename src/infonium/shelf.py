from enum import StrEnum
from typing import Annotated, Literal

import sqlalchemy
from pydantic import BaseModel, Field
from sqlalchemy.dialects import sqlite

from infonium.document import BlockNumber, Tagged
from infonium.glossary import Term
from infonium.library import OwnerTransaction, document_table, metadata, replacing_insert
from infonium.translation import ProgressKind, Stopped, Translated, TranslatedBlock, Unneeded

# The translations a library keeps: for each document and language, a record of how the
# translation stands, and the blocks it has translated. Stored with every body, as the
# library's tables do.
TRANSLATION_FORMAT = 1

translation_table = sqlalchemy.Table(
    "translation",
    metadata,
    sqlalchemy.Column(
        "document_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(document_table.c.id),
        primary_key=True,
    ),
    sqlalchemy.Column("language", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)
translated_block_table = sqlalchemy.Table(
    "translated_block",
    metadata,
    sqlalchemy.Column(
        "document_id",
        sqlalchemy.String,
        sqlalchemy.ForeignKey(document_table.c.id),
        primary_key=True,
    ),
    sqlalchemy.Column("language", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("block_number", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("owner", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("format", sqlalchemy.Integer, nullable=False),
    sqlalchemy.Column("body", sqlalchemy.String, nullable=False),
)


class Running(Tagged):
    # The translation is asked for and goes on, in the run its record names; a run that a
    # later one took the place of keeps nothing.
    kind: Literal[ProgressKind.TRANSLATING] = ProgressKind.TRANSLATING


# How a translation stands, as it is kept. A running translation is shown with how far it
# has come; any other is shown as it is kept.
Course = Annotated[Running | Stopped | Translated | Unneeded, Field(discriminator="kind")]


class SurveyKind(StrEnum):
    UNSURVEYED = "unsurveyed"
    SURVEYED = "surveyed"
    OVERRULED = "overruled"


class Unsurveyed(Tagged):
    # Nothing is known yet of the document's language or terms.
    kind: Literal[SurveyKind.UNSURVEYED] = SurveyKind.UNSURVEYED


class Surveyed(Tagged):
    # The survey found the document to need translating, with these terms.
    kind: Literal[SurveyKind.SURVEYED] = SurveyKind.SURVEYED
    terms: list[Term]


class Overruled(Tagged):
    # The reader asked for the document anew after the survey found it needed no
    # translation: it is translated without a survey, and without terms.
    kind: Literal[SurveyKind.OVERRULED] = SurveyKind.OVERRULED


SurveyState = Annotated[Unsurveyed | Surveyed | Overruled, Field(discriminator="kind")]


class TranslationRecord(BaseModel):
    # A translation of one item's document into one language: the title the document goes
    # by, what its survey found, the number of the run it goes on in or went on in last,
    # counted from 1, how it stands, and what it has taken of its models.
    document_id: str
    item_id: str
    language: str
    title: str
    survey: SurveyState
    run: Annotated[int, Field(ge=1)]
    course: Course
    input_tokens: int
    output_tokens: int


def with_tokens(
    record: TranslationRecord, input_tokens: int, output_tokens: int
) -> TranslationRecord:
    return record.model_copy(
        update={
            "input_tokens": record.input_tokens + input_tokens,
            "output_tokens": record.output_tokens + output_tokens,
        }
    )


class TranslationShelf:
    # The translations of one owner's library, read and written in one transaction.
    def __init__(self, transaction: OwnerTransaction) -> None:
        self.connection = transaction.connection
        self.owner = transaction.owner

    def of_document(
        self, table: sqlalchemy.Table, document_id: str, language: str
    ) -> list[sqlalchemy.ColumnElement[bool]]:
        return [
            table.c.owner == self.owner,
            table.c.document_id == document_id,
            table.c.language == language,
        ]

    def records(self, document_id: str, language: str) -> list[TranslationRecord]:
        # The translation of the document into the language when there is one, and nothing
        # when there is not.
        bodies = self.connection.execute(
            sqlalchemy.select(translation_table.c.body).where(
                *self.of_document(translation_table, document_id, language)
            )
        ).scalars()
        return [TranslationRecord.model_validate_json(body) for body in bodies]

    def keep_record(self, record: TranslationRecord) -> None:
        self.connection.execute(
            replacing_insert(
                translation_table,
                {
                    translation_table.c.document_id: record.document_id,
                    translation_table.c.language: record.language,
                    translation_table.c.owner: self.owner,
                    translation_table.c.format: TRANSLATION_FORMAT,
                    translation_table.c.body: record.model_dump_json(),
                },
            )
        )

    def blocks(self, document_id: str, language: str) -> list[TranslatedBlock]:
        bodies = self.connection.execute(
            sqlalchemy.select(translated_block_table.c.body)
            .where(*self.of_document(translated_block_table, document_id, language))
            .order_by(translated_block_table.c.block_number)
        ).scalars()
        return [TranslatedBlock.model_validate_json(body) for body in bodies]

    def kept_numbers(self, document_id: str, language: str) -> set[BlockNumber]:
        numbers = self.connection.execute(
            sqlalchemy.select(translated_block_table.c.block_number).where(
                *self.of_document(translated_block_table, document_id, language)
            )
        ).scalars()
        return set(numbers)

    def block_insert(
        self, document_id: str, language: str, block: TranslatedBlock
    ) -> sqlite.Insert:
        return sqlite.insert(translated_block_table).values(
            {
                translated_block_table.c.document_id: document_id,
                translated_block_table.c.language: language,
                translated_block_table.c.block_number: block.block_number,
                translated_block_table.c.owner: self.owner,
                translated_block_table.c.format: TRANSLATION_FORMAT,
                translated_block_table.c.body: block.model_dump_json(),
            }
        )

    def keep_block(self, document_id: str, language: str, block: TranslatedBlock) -> None:
        # In place of the block's earlier translation.
        insert = self.block_insert(document_id, language, block)
        self.connection.execute(
            insert.on_conflict_do_update(
                index_elements=list(translated_block_table.primary_key.columns),
                set_={
                    translated_block_table.c.format: insert.excluded.format,
                    translated_block_table.c.body: insert.excluded.body,
                },
            )
        )

    def add_block(self, document_id: str, language: str, block: TranslatedBlock) -> None:
        # Unless the block has a translation already, which stays.
        self.connection.execute(
            self.block_insert(document_id, language, block).on_conflict_do_nothing()
        )

    def running(self, language: str) -> list[TranslationRecord]:
        # The owner's translations into the language that are under way.
        bodies = self.connection.execute(
            sqlalchemy.select(translation_table.c.body).where(
                translation_table.c.owner == self.owner, translation_table.c.language == language
            )
        ).scalars()
        records = [TranslationRecord.model_validate_json(body) for body in bodies]
        return [record for record in records if isinstance(record.course, Running)]

    def discard_blocks(self, document_id: str, language: str) -> None:
        self.connection.execute(
            sqlalchemy.delete(translated_block_table).where(
                *self.of_document(translated_block_table, document_id, language)
            )
        )
