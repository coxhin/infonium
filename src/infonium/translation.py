import logging
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_EXCEPTION, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from enum import StrEnum
from functools import partial
from itertools import accumulate
from math import ceil
from threading import Event, Lock
from typing import Annotated, Literal
from unicodedata import category

import openai
import pydantic
import sqlalchemy
from pydantic import BaseModel, Field
from sqlalchemy.dialects import sqlite

from infonium.document import (
    WORD_GAP,
    Block,
    BlockNumber,
    Code,
    Document,
    FormulaMark,
    Heading,
    ListItem,
    Mark,
    Offset,
    Paragraph,
    Quotation,
    Table,
    Tagged,
    TextRange,
    TimePlace,
)
from infonium.intake import failure_reason
from infonium.language_model import (
    LanguageModel,
    StructuredCompletion,
    UnmeteredCompletion,
    UnstructuredCompletion,
)
from infonium.library import (
    Holding,
    Library,
    LibraryStore,
    NotInLibrary,
    document_table,
    metadata,
    replacing_insert,
)
from infonium.rendition import Passages, Rendered, Term, Wording, keeps_marks, renditions
from infonium.transcript import ends_sentence


class Segment(BaseModel):
    # A part of a block's text, and what it says in the translation's language. The marks are
    # counted in the translated text.
    start: Offset
    end: Offset
    text: str
    marks: list[Mark]


class TranslatedBlock(BaseModel):
    # A block of a document in the translation's language: the parts of its text that were
    # translated, in the order of the text, and the model that wrote them. A block of a
    # recording is translated a sentence at a time, so that each sentence keeps the stretch
    # of time in which it is spoken; any other block is one segment.
    block_number: BlockNumber
    model: str
    segments: list[Segment]


class ProgressKind(StrEnum):
    UNASKED = "unasked"
    TRANSLATING = "translating"
    STOPPED = "stopped"
    TRANSLATED = "translated"
    UNNEEDED = "unneeded"


class Unasked(Tagged):
    # Nobody has asked for the document in this language.
    kind: Literal[ProgressKind.UNASKED] = ProgressKind.UNASKED


class Translating(Tagged):
    # The translation is under way: of the blocks that are to be translated, so many are.
    kind: Literal[ProgressKind.TRANSLATING] = ProgressKind.TRANSLATING
    translated: Annotated[int, Field(ge=0)]
    total: Annotated[int, Field(ge=0)]


class Stopped(Tagged):
    # The translation stopped before its end, for this reason. What was translated is kept,
    # and a translation that is asked for again goes on from there.
    kind: Literal[ProgressKind.STOPPED] = ProgressKind.STOPPED
    reason: str


class Translated(Tagged):
    # Every block that is to be translated is.
    kind: Literal[ProgressKind.TRANSLATED] = ProgressKind.TRANSLATED


class Unneeded(Tagged):
    # The document is written in the translation's language already.
    kind: Literal[ProgressKind.UNNEEDED] = ProgressKind.UNNEEDED


# How far the translation of one document into one language has come.
Progress = Annotated[
    Unasked | Translating | Stopped | Translated | Unneeded, Field(discriminator="kind")
]


# A segment of a recording ends where a place ends a sentence, and once it has run this long
# where none does, so that a line of the translation can be read while it is heard.
SEGMENT_LIMIT_SECONDS = 10.0
# The most text of the source one request asks to have translated, in characters, and how
# much of the text around it the request carries. A block longer than the bound is asked for
# in a request of its own. Measured with gpt-6-luna on 152 blocks, four requests at once:
# requests of 2,000 characters took 172 seconds in all and their median 33, of 4,000 124 and
# 57, of 8,000 104 and 90. The middle one serves the first blocks within a minute.
REQUEST_CHARACTERS = 4000
CONTEXT_CHARACTERS = 600
# How many requests of one translation run at once, how many translations run at once, and
# how many times a request is made before the translation stops. Eight requests at once took
# 288 seconds over 608 blocks where four took 540, and the endpoint refused none.
REQUESTS_AT_ONCE = 8
TRANSLATIONS_AT_ONCE = 2
ATTEMPTS = 3
# The sample of a document by which its language is judged and its terms are found: the
# blocks that are translated, as many as this many characters hold, spread over the
# document; and the most terms that are found.
SAMPLE_CHARACTERS = 12000
SURVEY_TERMS = 60
# Stored with every body, as the library's tables do.
TRANSLATION_FORMAT = 1
LETTER = "L"
BLOCKS_BETWEEN = "\n"
SAMPLE_BETWEEN = "\n\n"
NOT_TRANSLATED = "block {block_number} of item {item_id} is not translated"
UNUSABLE = "the model's translations of blocks {numbers} could not be used in {attempts} replies"
UNASKED_BLOCKS = "only blocks translated one at a time are kept; the rest was never asked for"
SURVEY_INSTRUCTIONS = """\
You are given passages taken from across a document, which is to be translated into \
{language} in pieces.

First name the language most of its words are written in, and say whether that is \
{language}, in the script {language} is written in. A document written mostly in another \
language, or in another script of the same language, such as Traditional Chinese where \
Simplified Chinese is asked for, is not.

Then, unless it is, list the names and terms that recur in the passages and that pieces \
translated apart could render differently: people, places, organisations, works, products, \
and the terms of the field. Give each the one translation into {language} to use for it \
throughout the document; where {language} keeps a name as it is written, give it \
unchanged. List at most {terms}, those that recur most.
"""

logger = logging.getLogger(__name__)

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


class UnusableTranslation(UnstructuredCompletion):
    # The model answered, but none of its replies for some blocks could be used.
    pass


class Running(Tagged):
    # The translation is asked for and goes on, in the run its record counts last; a run
    # that a later one took the place of keeps nothing.
    kind: Literal[ProgressKind.TRANSLATING] = ProgressKind.TRANSLATING


# How a translation stands, as it is kept. A running translation is shown with how far it
# has come; any other is shown as it is kept.
Course = Annotated[Running | Stopped | Translated | Unneeded, Field(discriminator="kind")]


class TranslationRecord(BaseModel):
    # A translation of one document into one language: the title the document goes by and
    # the translations of its terms, how many runs it was started in, how it stands, and what
    # it has taken of its models.
    document_id: str
    language: str
    title: str
    terms: list[Term]
    runs: int
    course: Course
    input_tokens: int
    output_tokens: int


class TranslationStanding(BaseModel):
    # A translation as far as it has come, and the blocks it has kept, in their order.
    document: str
    language: str
    progress: Progress
    input_tokens: int
    output_tokens: int
    blocks: list[TranslatedBlock]


class RunningTranslation(BaseModel):
    owner: str
    document_id: str
    language: str
    run: int


class Survey(BaseModel):
    # What a sample of a document tells before it is translated: its language, whether that
    # is the one it is to be translated into, and else its terms.
    language: str
    written_in_asked_language: bool
    terms: list[Term]


class Work(BaseModel):
    # Blocks of a document to translate, by their numbers, with the title the document goes
    # by and its terms.
    document: Document
    title: str
    terms: list[Term]
    numbers: list[BlockNumber]


def says_words(text: str) -> bool:
    return any(category(character).startswith(LETTER) for character in text)


def block_marks(block: Block) -> list[Mark]:
    match block:
        case Heading() | Paragraph() | ListItem() | Quotation():
            return block.marks
        case _:
            return []


def is_translated(block: Block) -> bool:
    # A listing is kept as it is written, and so is a formula; a block with no letter of any
    # script says nothing in words.
    whole_formula = any(
        isinstance(mark, FormulaMark) and mark.start == 0 and mark.end == len(block.text)
        for mark in block_marks(block)
    )
    return not isinstance(block, Code) and not whole_formula and says_words(block.text)


def translated_numbers(document: Document) -> list[BlockNumber]:
    return [number for number, block in enumerate(document.blocks) if is_translated(block)]


def segment_ranges(block: Block) -> list[TextRange]:
    # A block spoken in a recording is cut after a place that ends a sentence, or once it has
    # run long; any other block is one segment.
    times = [place for place in block.places if isinstance(place, TimePlace)]
    ranges: list[TextRange] = []
    first = 0
    for at, place in enumerate(times):
        lasted = place.moment + place.duration - times[first].moment
        ended = ends_sentence(block.text[place.start : place.end])
        if at + 1 == len(times) or ended or lasted >= SEGMENT_LIMIT_SECONDS:
            ranges.append(TextRange(start=times[first].start, end=place.end))
            first = at + 1
    return ranges or [TextRange(start=0, end=len(block.text))]


def table_cells(table: Table) -> list[list[str]]:
    return [row.split(table.cell_separator) for row in table.text.split(table.row_separator)]


def segment_wording(block: Block, segment: TextRange) -> Wording:
    # The part of a block's text in a segment, with the parts of its marks that lie in it.
    marks = [
        mark.model_copy(
            update={
                "start": max(mark.start, segment.start) - segment.start,
                "end": min(mark.end, segment.end) - segment.start,
            }
        )
        for mark in block_marks(block)
        if mark.start < segment.end and segment.start < mark.end
    ]
    return Wording(text=block.text[segment.start : segment.end], marks=marks)


def block_wordings(block: Block) -> list[Wording]:
    # What of a block is asked for: a table cell by cell, those that say something in words,
    # and any other block segment by segment.
    if isinstance(block, Table):
        cells = [cell for row in table_cells(block) for cell in row]
        return [Wording(text=cell, marks=[]) for cell in cells if says_words(cell)]
    return [segment_wording(block, segment) for segment in segment_ranges(block)]


def translated_block(
    block_number: BlockNumber, block: Block, model: str, rendered: list[Rendered]
) -> TranslatedBlock:
    # The translated cells of a table take the places of their sources, each on one line, so
    # that the translated text keeps the table's grid; a cell with no words stays as it is.
    if isinstance(block, Table):
        answers = iter(rendered)
        rows = [
            block.cell_separator.join(
                WORD_GAP.join(next(answers).text.split()) if says_words(cell) else cell
                for cell in row
            )
            for row in table_cells(block)
        ]
        text = block.row_separator.join(rows)
        segments = [Segment(start=0, end=len(block.text), text=text, marks=[])]
    else:
        segments = [
            Segment(start=segment.start, end=segment.end, text=done.text, marks=done.marks)
            for segment, done in zip(segment_ranges(block), rendered, strict=True)
        ]
    return TranslatedBlock(block_number=block_number, model=model, segments=segments)


def passages_of(work: Work, wordings: list[list[Wording]]) -> Passages:
    # The blocks' wordings, with as much of the text before the first block and after the
    # last as a request carries.
    blocks = work.document.blocks
    before = BLOCKS_BETWEEN.join(block.text for block in blocks[: work.numbers[0]])
    after = BLOCKS_BETWEEN.join(block.text for block in blocks[work.numbers[-1] + 1 :])
    return Passages(
        title=work.title,
        terms=work.terms,
        before=before[len(before) - CONTEXT_CHARACTERS :] if before else "",
        wordings=[wording for block in wordings for wording in block],
        after=after[:CONTEXT_CHARACTERS],
    )


def requests_of(document: Document, numbers: list[BlockNumber]) -> list[list[BlockNumber]]:
    # Blocks in their order, gathered into requests up to the bound on their size.
    requests: list[list[BlockNumber]] = []
    size = REQUEST_CHARACTERS
    for number in numbers:
        block_size = sum(len(wording.text) for wording in block_wordings(document.blocks[number]))
        if size + block_size > REQUEST_CHARACTERS:
            requests.append([])
            size = 0
        requests[-1].append(number)
        size += block_size
    return requests


def document_sample(document: Document) -> str:
    # Every so many of the blocks that are translated, so that what they hold together comes
    # near the size of a sample.
    texts = [document.blocks[number].text for number in translated_numbers(document)]
    stride = ceil(sum(map(len, texts)) / SAMPLE_CHARACTERS)
    return SAMPLE_BETWEEN.join(texts[::stride])[:SAMPLE_CHARACTERS]


def progress_of(record: TranslationRecord, document: Document, kept: int) -> Progress:
    match record.course:
        case Running():
            return Translating(translated=kept, total=len(translated_numbers(document)))
        case course:
            return course


def with_tokens(
    record: TranslationRecord, input_tokens: int, output_tokens: int
) -> TranslationRecord:
    return record.model_copy(
        update={
            "input_tokens": record.input_tokens + input_tokens,
            "output_tokens": record.output_tokens + output_tokens,
        }
    )


def is_current(records: list[TranslationRecord], run: int) -> bool:
    # Whether the run is the one the translation goes on in.
    return any(record.runs == run and isinstance(record.course, Running) for record in records)


def restarted(
    records: list[TranslationRecord], first: TranslationRecord
) -> list[TranslationRecord]:
    # The translation as it starts, when it was never asked for, starting as the first record
    # given, or has stopped; nothing when it is under way, done or unneeded.
    match records:
        case []:
            return [first]
        case [TranslationRecord(course=Stopped()) as record]:
            return [record.model_copy(update={"runs": record.runs + 1, "course": Running()})]
        case _:
            return []


# What becomes of what a request brought: the blocks it translated, and the tokens it took.
# It answers whether the requests are to go on.
Keeper = Callable[[list[TranslatedBlock], int, int], bool]


def translate_blocks(
    model: LanguageModel, language: str, work: Work, keep: Keeper, closing: Event
) -> None:
    # Asks for the blocks, then again for those that did not come back usable, until each
    # has or as many requests are made as a translation makes. A request that fails is made
    # again the same way. A block that lost most of the marks of one of its passages is
    # asked for again too, but kept as it came from the last request.
    pending = work.numbers
    for attempt in range(1, ATTEMPTS + 1):
        if closing.is_set():
            return
        wordings = [block_wordings(work.document.blocks[number]) for number in pending]
        asked = work.model_copy(update={"numbers": pending})
        logger.info(
            "asking %s for blocks %s of document %s, attempt %d",
            model.name,
            pending,
            work.document.id,
            attempt,
        )
        passages = passages_of(asked, wordings)
        try:
            replied = renditions(model, language, passages)
        except (
            openai.OpenAIError,
            UnstructuredCompletion,
            UnmeteredCompletion,
            pydantic.ValidationError,
        ) as failure:
            if attempt == ATTEMPTS:
                raise
            logger.warning("a translation request is made again: %s", failure_reason(failure))
            continue
        ends = [0, *accumulate(len(block) for block in wordings)]
        done: list[TranslatedBlock] = []
        missing: list[BlockNumber] = []
        for number, first, last in zip(pending, ends, ends[1:], strict=False):
            rendered = [replied.rendered[at] for at in range(first, last) if at in replied.rendered]
            complete = len(rendered) == last - first
            faithful = attempt == ATTEMPTS or all(
                keeps_marks(wording, done)
                for wording, done in zip(passages.wordings[first:last], rendered, strict=False)
            )
            if complete and faithful:
                block = work.document.blocks[number]
                done.append(translated_block(number, block, model.name, rendered))
            else:
                missing.append(number)
        if not keep(done, replied.input_tokens, replied.output_tokens) or not missing:
            return
        pending = missing
    raise UnusableTranslation(UNUSABLE.format(numbers=pending, attempts=ATTEMPTS))


class TranslationShelf:
    # The translations of one owner's library, read and written in the library's transaction.
    def __init__(self, connection: sqlalchemy.Connection, owner: str) -> None:
        self.connection = connection
        self.owner = owner

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

    def discard_blocks(self, document_id: str, language: str) -> None:
        self.connection.execute(
            sqlalchemy.delete(translated_block_table).where(
                *self.of_document(translated_block_table, document_id, language)
            )
        )

    def document(self, document_id: str) -> Document:
        body = self.connection.execute(
            sqlalchemy.select(document_table.c.body).where(
                document_table.c.owner == self.owner, document_table.c.id == document_id
            )
        ).scalar_one()
        return Document.model_validate_json(body)


def running_translations(connection: sqlalchemy.Connection) -> list[RunningTranslation]:
    # Every owner's translations that are under way.
    rows = connection.execute(
        sqlalchemy.select(translation_table.c.owner, translation_table.c.body)
    )
    kept = [(owner, TranslationRecord.model_validate_json(body)) for owner, body in rows]
    return [
        RunningTranslation(
            owner=owner, document_id=record.document_id, language=record.language, run=record.runs
        )
        for owner, record in kept
        if isinstance(record.course, Running)
    ]


class Translator:
    # Translates documents of a library into one language, block by block and off the
    # thread that asks, and keeps each block as it comes. The models are made when they are
    # first asked for.
    def __init__(
        self,
        store: LibraryStore,
        language: str,
        translating: Callable[[], LanguageModel],
        retranslating: Callable[[], LanguageModel],
    ) -> None:
        self.store = store
        self.language = language
        self.translating = translating
        self.retranslating = retranslating
        self.pool = ThreadPoolExecutor(
            max_workers=TRANSLATIONS_AT_ONCE, thread_name_prefix="translator"
        )
        # A translation is started, kept to and stopped by one thread at a time. So one that
        # is asked for twice at once is started once, and a run that another took the place
        # of keeps nothing.
        self.turn = Lock()
        # Set when the service stops: a run then asks for nothing more, and stays running, to
        # go on when the service starts again.
        self.closing = Event()

    @contextmanager
    def shelves(self, owner: str) -> Iterator[tuple[Library, TranslationShelf]]:
        with self.store.library(owner) as library:
            yield library, TranslationShelf(library.connection, owner)

    def standing(self, owner: str, item_id: str) -> TranslationStanding:
        with self.shelves(owner) as (library, shelf):
            document = library.holding(item_id).document
            records = shelf.records(document.id, self.language)
            blocks = shelf.blocks(document.id, self.language)
        progress: Progress = Unasked()
        input_tokens = output_tokens = 0
        for record in records:
            progress = progress_of(record, document, len(blocks))
            input_tokens, output_tokens = record.input_tokens, record.output_tokens
        return TranslationStanding(
            document=document.id,
            language=self.language,
            progress=progress,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            blocks=blocks,
        )

    def ensure(self, owner: str, item_id: str) -> TranslationStanding:
        # Starts the translation when it was never asked for or has stopped.
        with self.turn, self.shelves(owner) as (library, shelf):
            holding = library.holding(item_id)
            first = TranslationRecord(
                document_id=holding.document.id,
                language=self.language,
                title=holding.title,
                terms=[],
                runs=1,
                course=Running(),
                input_tokens=0,
                output_tokens=0,
            )
            started = restarted(shelf.records(holding.document.id, self.language), first)
            for record in started:
                shelf.keep_record(record)
        for record in started:
            self.translate_in_background(owner, record.document_id, record.runs)
        return self.standing(owner, item_id)

    def anew(self, owner: str, item_id: str) -> TranslationStanding:
        # Discards what was translated and starts from the first block, in a run that takes
        # the place of any under way.
        with self.turn, self.shelves(owner) as (library, shelf):
            holding = library.holding(item_id)
            earlier = shelf.records(holding.document.id, self.language)
            record = TranslationRecord(
                document_id=holding.document.id,
                language=self.language,
                title=holding.title,
                terms=[],
                runs=1 + max((record.runs for record in earlier), default=0),
                course=Running(),
                input_tokens=0,
                output_tokens=0,
            )
            shelf.discard_blocks(record.document_id, self.language)
            shelf.keep_record(record)
        self.translate_in_background(owner, record.document_id, record.runs)
        return self.standing(owner, item_id)

    def retranslated(self, owner: str, item_id: str, block_number: BlockNumber) -> TranslatedBlock:
        # Translates one block again with the model that writes digests, and keeps it in
        # place of its earlier translation. A block that is never translated has none.
        with self.shelves(owner) as (library, shelf):
            holding = library.holding(item_id)
            records = shelf.records(holding.document.id, self.language)
        blocks = holding.document.blocks
        if block_number >= len(blocks) or not is_translated(blocks[block_number]):
            raise NotInLibrary(NOT_TRANSLATED.format(block_number=block_number, item_id=item_id))
        kept: list[TranslatedBlock] = []
        keep = partial(self.keep_again, owner, holding, kept)
        work = Work(
            document=holding.document,
            title=holding.title,
            terms=[term for record in records for term in record.terms],
            numbers=[block_number],
        )
        translate_blocks(self.retranslating(), self.language, work, keep, Event())
        [block] = kept
        return block

    def keep_again(
        self,
        owner: str,
        holding: Holding,
        kept: list[TranslatedBlock],
        done: list[TranslatedBlock],
        input_tokens: int,
        output_tokens: int,
    ) -> bool:
        # A block translated again, and the tokens it took, kept whatever the translation's
        # course. A document whose translation was never asked for gets one that stands
        # stopped, holding the block.
        document_id = holding.document.id
        with self.turn, self.shelves(owner) as (_, shelf):
            unasked = TranslationRecord(
                document_id=document_id,
                language=self.language,
                title=holding.title,
                terms=[],
                runs=0,
                course=Stopped(reason=UNASKED_BLOCKS),
                input_tokens=0,
                output_tokens=0,
            )
            [record] = shelf.records(document_id, self.language) or [unasked]
            shelf.keep_record(with_tokens(record, input_tokens, output_tokens))
            for block in done:
                shelf.keep_block(document_id, self.language, block)
        kept.extend(done)
        return True

    def keep_in_run(
        self,
        owner: str,
        document_id: str,
        run: int,
        done: list[TranslatedBlock],
        input_tokens: int,
        output_tokens: int,
    ) -> bool:
        # Blocks that a run translated, and the tokens they took, kept while the run is the
        # current one; a block kept already stays as it is.
        with self.turn, self.shelves(owner) as (_, shelf):
            records = shelf.records(document_id, self.language)
            current = is_current(records, run)
            for record in records if current else []:
                shelf.keep_record(with_tokens(record, input_tokens, output_tokens))
                for block in done:
                    shelf.add_block(document_id, self.language, block)
        return current and not self.closing.is_set()

    def keep_survey(
        self, owner: str, document_id: str, run: int, survey: StructuredCompletion[Survey]
    ) -> None:
        with self.turn, self.shelves(owner) as (_, shelf):
            records = shelf.records(document_id, self.language)
            for record in records if is_current(records, run) else []:
                surveyed = record.model_copy(update={"terms": survey.output.terms})
                shelf.keep_record(with_tokens(surveyed, survey.input_tokens, survey.output_tokens))

    def course_changed(self, owner: str, document_id: str, run: int, course: Course) -> None:
        with self.turn, self.shelves(owner) as (_, shelf):
            records = shelf.records(document_id, self.language)
            for record in records if is_current(records, run) else []:
                shelf.keep_record(record.model_copy(update={"course": course}))

    def translate_in_background(self, owner: str, document_id: str, run: int) -> None:
        finished = self.pool.submit(self.translate, owner, document_id, run)
        finished.add_done_callback(partial(self.record_failure, owner, document_id, run))

    def translate(self, owner: str, document_id: str, run: int) -> None:
        # One run of a translation. A document is surveyed first, for its language and its
        # terms, unless a run before found its terms; the blocks not kept are then asked for,
        # several requests at once. A translation that holds only blocks translated one at a
        # time has no terms yet, and one whose survey found none is surveyed again.
        with self.shelves(owner) as (_, shelf):
            records = shelf.records(document_id, self.language)
            document = shelf.document(document_id)
            kept = shelf.kept_numbers(document_id, self.language)
        if not is_current(records, run):
            return
        pending = [number for number in translated_numbers(document) if number not in kept]
        terms = records[0].terms
        if pending and not terms:
            survey = self.survey(document)
            self.keep_survey(owner, document_id, run, survey)
            if survey.output.written_in_asked_language:
                self.course_changed(owner, document_id, run, Unneeded())
                return
            terms = survey.output.terms
        model = self.translating()
        with ThreadPoolExecutor(
            max_workers=REQUESTS_AT_ONCE, thread_name_prefix="translation-request"
        ) as requesting:
            requests = [
                requesting.submit(
                    translate_blocks,
                    model,
                    self.language,
                    Work(document=document, title=records[0].title, terms=terms, numbers=numbers),
                    partial(self.keep_in_run, owner, document_id, run),
                    self.closing,
                )
                for numbers in requests_of(document, pending)
            ]
            # The first request that fails stops the translation: the requests that wait are
            # not made, and those under way are let finish and kept.
            wait(requests, return_when=FIRST_EXCEPTION)
            for request in requests:
                request.cancel()
        for request in requests:
            if not request.cancelled() and (failure := request.exception()) is not None:
                raise failure
        with self.shelves(owner) as (_, shelf):
            kept = shelf.kept_numbers(document_id, self.language)
        if not self.closing.is_set() and set(translated_numbers(document)) <= kept:
            self.course_changed(owner, document_id, run, Translated())

    def survey(self, document: Document) -> StructuredCompletion[Survey]:
        # The model is asked about a sample of the document: a script tells no language
        # apart from another written in it, and the language is named as a person names it.
        completion = self.translating().structured(
            instructions=SURVEY_INSTRUCTIONS.format(language=self.language, terms=SURVEY_TERMS),
            prompt=document_sample(document),
            output=Survey,
        )
        logger.info(
            "document %s is judged to be written in %s, with %d terms",
            document.id,
            completion.output.language,
            len(completion.output.terms),
        )
        return completion

    def record_failure(
        self, owner: str, document_id: str, run: int, finished: Future[None]
    ) -> None:
        error = None if finished.cancelled() else finished.exception()
        if error is not None:
            logger.error("translating document %s failed", document_id, exc_info=error)
            self.course_changed(owner, document_id, run, Stopped(reason=failure_reason(error)))

    def resume(self) -> None:
        # Translations that were under way when the service stopped go on from what they kept.
        with self.store.engine.begin() as connection:
            running = running_translations(connection)
        for translation in running:
            if translation.language == self.language:
                self.translate_in_background(
                    translation.owner, translation.document_id, translation.run
                )

    def close(self) -> None:
        self.closing.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
