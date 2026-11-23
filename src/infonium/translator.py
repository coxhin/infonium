import logging
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from functools import partial
from threading import Event, Lock

from pydantic import BaseModel

from infonium.asking import Work, requests_of, translate_blocks
from infonium.document import BlockNumber, Document
from infonium.language_model import LanguageModel, StructuredCompletion
from infonium.library import Item, Library, LibraryStore, NotInLibrary, Read
from infonium.reasons import failure_reason
from infonium.shelf import (
    Course,
    Halted,
    Overruled,
    Running,
    Surveyed,
    TranslationRecord,
    TranslationShelf,
    Unsurveyed,
    with_tokens,
)
from infonium.survey import DocumentSurvey, surveyed
from infonium.translation import (
    Progress,
    Stopped,
    Translated,
    TranslatedBlock,
    Translating,
    Unasked,
    Unneeded,
    is_translated,
    translated_numbers,
)

# The worker that translates the documents of a library in the background. How many
# requests of one translation run at once, and how many translations run at once. Eight
# requests at once took 288 seconds over 608 blocks where four took 540, and the endpoint
# refused none.
REQUESTS_AT_ONCE = 8
TRANSLATIONS_AT_ONCE = 2
# How long a run that waits for its requests waits before it looks whether it has ended.
ENDED_CHECK_SECONDS = 1.0
NOT_TRANSLATED = "block {block_number} of item {item_id} is not translated"
NOT_ASKED = "no translation of item {item_id} is asked for, so none of its blocks is translated"
# Why a translation stopped with blocks missing, for a reader.
READ_AGAIN = "the item was read again into another document, which is translated apart"

logger = logging.getLogger(__name__)


class TranslationNotAsked(Exception):
    # A block is translated again only where a translation was asked for and is needed.
    pass


class TranslationStanding(BaseModel):
    # A translation as far as it has come, the blocks it has kept, in their order, and the
    # numbers of the document's blocks that are to be translated.
    document: str
    language: str
    progress: Progress
    input_tokens: int
    output_tokens: int
    blocks: list[TranslatedBlock]
    blocks_to_translate: list[BlockNumber]


def progress_of(record: TranslationRecord, document: Document, kept: int) -> Progress:
    total = len(translated_numbers(document))
    match record.course:
        case Running():
            return Translating(translated=kept, total=total)
        case Halted(reason=reason):
            return Stopped(translated=kept, total=total, reason=reason)
        case course:
            return course


def is_current(records: list[TranslationRecord], run: int) -> bool:
    # Whether the run is the one the translation goes on in.
    return any(record.run == run and isinstance(record.course, Running) for record in records)


def holds_document(item: Item, document_id: str) -> bool:
    match item.reading:
        case Read(document=held):
            return held == document_id
        case _:
            return False


def restarted(
    records: list[TranslationRecord], first: TranslationRecord
) -> list[TranslationRecord]:
    # The translation as it starts: as the first record given when it was never asked for,
    # in a new run when it has stopped, and not at all when it is under way, done or
    # unneeded.
    match records:
        case []:
            return [first]
        case [TranslationRecord(course=Halted()) as record]:
            return [record.model_copy(update={"run": record.run + 1, "course": Running()})]
        case _:
            return []


def gather_failure(failures: list[BaseException], request: Future[None]) -> None:
    if not request.cancelled() and (failure := request.exception()) is not None:
        logger.warning("blocks are left untranslated: %s", failure_reason(failure))
        failures.append(failure)


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
        # The end of each run at work, by owner and document: set when a later run takes its
        # place, when its item is read again, or when the service stops. A run that has ended
        # makes no more requests and gives up its place among the translations at work.
        self.ends: dict[tuple[str, str], Event] = {}
        # Set when the service stops: a run then stays running, to go on when the service
        # starts again.
        self.closing = Event()

    @contextmanager
    def opened(self, owner: str) -> Iterator[tuple[Library, TranslationShelf]]:
        with self.store.transaction(owner) as transaction:
            yield Library(transaction), TranslationShelf(transaction)

    def standing(self, owner: str, item_id: str) -> TranslationStanding:
        with self.opened(owner) as (library, shelf):
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
            blocks_to_translate=translated_numbers(document),
        )

    def new_end(self, owner: str, document_id: str) -> Event:
        # The end of a run that starts, which ends the run before it. Called with the turn
        # held.
        key = (owner, document_id)
        if key in self.ends:
            self.ends[key].set()
        self.ends[key] = Event()
        return self.ends[key]

    def ensure(self, owner: str, item_id: str) -> TranslationStanding:
        # Starts the translation when it was never asked for or has stopped.
        with self.turn, self.opened(owner) as (library, shelf):
            holding = library.holding(item_id)
            first = TranslationRecord(
                document_id=holding.document.id,
                item_id=item_id,
                language=self.language,
                title=holding.title,
                survey=Unsurveyed(),
                run=1,
                course=Running(),
                input_tokens=0,
                output_tokens=0,
            )
            started = [
                (record, self.new_end(owner, record.document_id))
                for record in restarted(shelf.records(holding.document.id, self.language), first)
            ]
            for record, _ in started:
                shelf.keep_record(record)
        for record, ended in started:
            self.translate_in_background(
                owner, record.item_id, record.document_id, record.run, ended
            )
        return self.standing(owner, item_id)

    def anew(self, owner: str, item_id: str) -> TranslationStanding:
        # Discards what was translated and starts from the first block, in a run that takes
        # the place of any under way. A document that the survey found to need no
        # translation, or that a reader asked for anew after that, is translated whatever
        # its language.
        with self.turn, self.opened(owner) as (library, shelf):
            holding = library.holding(item_id)
            earlier = shelf.records(holding.document.id, self.language)
            overruled = any(
                isinstance(record.course, Unneeded) or isinstance(record.survey, Overruled)
                for record in earlier
            )
            record = TranslationRecord(
                document_id=holding.document.id,
                item_id=item_id,
                language=self.language,
                title=holding.title,
                survey=Overruled() if overruled else Unsurveyed(),
                run=1 + max((record.run for record in earlier), default=0),
                course=Running(),
                input_tokens=0,
                output_tokens=0,
            )
            shelf.discard_blocks(record.document_id, self.language)
            shelf.keep_record(record)
            ended = self.new_end(owner, record.document_id)
        self.translate_in_background(owner, item_id, record.document_id, record.run, ended)
        return self.standing(owner, item_id)

    def retranslated(self, owner: str, item_id: str, block_number: BlockNumber) -> TranslatedBlock:
        # Translates one block again with the model that writes digests, and keeps it in
        # place of its earlier translation. Only a translation that was asked for and is
        # needed has blocks to translate again, and a block that is never translated has none.
        with self.opened(owner) as (library, shelf):
            holding = library.holding(item_id)
            records = shelf.records(holding.document.id, self.language)
        if not records or any(isinstance(record.course, Unneeded) for record in records):
            raise TranslationNotAsked(NOT_ASKED.format(item_id=item_id))
        blocks = holding.document.blocks
        if block_number >= len(blocks) or not is_translated(blocks[block_number]):
            raise NotInLibrary(NOT_TRANSLATED.format(block_number=block_number, item_id=item_id))
        kept: list[TranslatedBlock] = []
        work = Work(
            document=holding.document,
            title=holding.title,
            terms=[
                term
                for record in records
                if isinstance(record.survey, Surveyed)
                for term in record.survey.terms
            ],
            numbers=[block_number],
        )
        keep = partial(self.keep_again, owner, holding.document.id, kept)
        translate_blocks(self.retranslating(), self.language, work, keep, lambda: True)
        [block] = kept
        return block

    def keep_again(
        self,
        owner: str,
        document_id: str,
        kept: list[TranslatedBlock],
        done: list[TranslatedBlock],
        input_tokens: int,
        output_tokens: int,
    ) -> bool:
        # A block translated again, and the tokens it took, kept whatever the translation's
        # course.
        with self.turn, self.opened(owner) as (_, shelf):
            for record in shelf.records(document_id, self.language):
                shelf.keep_record(with_tokens(record, input_tokens, output_tokens))
            for block in done:
                shelf.keep_block(document_id, self.language, block)
        kept.extend(done)
        return True

    def keep_in_run(
        self,
        owner: str,
        item_id: str,
        document_id: str,
        run: int,
        done: list[TranslatedBlock],
        input_tokens: int,
        output_tokens: int,
    ) -> bool:
        # Blocks that a run translated, and the tokens they took, kept while the run is the
        # current one and its item holds its document; a block kept already stays as it is.
        # A run whose item was read again keeps nothing more, and stops.
        with self.turn, self.opened(owner) as (library, shelf):
            records = shelf.records(document_id, self.language)
            current = is_current(records, run)
            holds = holds_document(library.item(item_id), document_id)
            for record in records if current and not holds else []:
                shelf.keep_record(record.model_copy(update={"course": Halted(reason=READ_AGAIN)}))
            for record in records if current and holds else []:
                shelf.keep_record(with_tokens(record, input_tokens, output_tokens))
                for block in done:
                    shelf.add_block(document_id, self.language, block)
        return current and holds and not self.closing.is_set()

    def keep_survey(
        self,
        owner: str,
        document_id: str,
        run: int,
        survey: StructuredCompletion[DocumentSurvey],
        course: Course,
    ) -> None:
        # What a survey found, and the tokens it took. A document that needs no translation
        # keeps no terms.
        needed = not survey.output.written_in_asked_language
        with self.turn, self.opened(owner) as (_, shelf):
            records = shelf.records(document_id, self.language)
            for record in records if is_current(records, run) else []:
                found = Surveyed(terms=survey.output.terms) if needed else record.survey
                changed = record.model_copy(update={"survey": found, "course": course})
                shelf.keep_record(with_tokens(changed, survey.input_tokens, survey.output_tokens))

    def course_changed(self, owner: str, document_id: str, run: int, course: Course) -> None:
        with self.turn, self.opened(owner) as (_, shelf):
            records = shelf.records(document_id, self.language)
            for record in records if is_current(records, run) else []:
                shelf.keep_record(record.model_copy(update={"course": course}))

    def goes_on(self, owner: str, item_id: str, document_id: str, run: int, ended: Event) -> bool:
        # Whether a run is still to ask for blocks: it has not ended, and its item still holds
        # its document. A run whose item was read again stops.
        if ended.is_set():
            return False
        with self.store.transaction(owner) as transaction:
            holds = holds_document(Library(transaction).item(item_id), document_id)
        if not holds:
            ended.set()
            self.course_changed(owner, document_id, run, Halted(reason=READ_AGAIN))
        return holds

    def translate_in_background(
        self, owner: str, item_id: str, document_id: str, run: int, ended: Event
    ) -> None:
        finished = self.pool.submit(self.translate, owner, item_id, document_id, run, ended)
        finished.add_done_callback(partial(self.run_over, owner, document_id, run, ended))

    def translate(self, owner: str, item_id: str, document_id: str, run: int, ended: Event) -> None:
        # One run of a translation. A document is surveyed first, for its language and its
        # terms, unless a run before surveyed it or a reader overruled the survey; the blocks
        # not kept are then asked for, several requests at once. A request whose blocks fail
        # every attempt leaves them untranslated, and the run goes on with the rest; a
        # translation that misses blocks at its end has stopped.
        if not self.goes_on(owner, item_id, document_id, run, ended):
            return
        with self.opened(owner) as (library, shelf):
            records = shelf.records(document_id, self.language)
            holding = library.holding(item_id)
            kept = shelf.kept_numbers(document_id, self.language)
        if not is_current(records, run):
            return
        document = holding.document
        pending = [number for number in translated_numbers(document) if number not in kept]
        match records[0].survey:
            case Unsurveyed() if pending:
                survey = surveyed(self.translating(), self.language, document)
                if survey.output.written_in_asked_language:
                    self.keep_survey(owner, document_id, run, survey, Unneeded())
                    return
                self.keep_survey(owner, document_id, run, survey, Running())
                terms = survey.output.terms
            case Surveyed(terms=found):
                terms = found
            case _:
                terms = []
        model = self.translating()
        failures: list[BaseException] = []
        requesting = ThreadPoolExecutor(
            max_workers=REQUESTS_AT_ONCE, thread_name_prefix="translation-request"
        )
        requests = [
            requesting.submit(
                translate_blocks,
                model,
                self.language,
                Work(document=document, title=holding.title, terms=terms, numbers=numbers),
                partial(self.keep_in_run, owner, item_id, document_id, run),
                partial(self.goes_on, owner, item_id, document_id, run, ended),
            )
            for numbers in requests_of(document, pending)
        ]
        for request in requests:
            request.add_done_callback(partial(gather_failure, failures))
        while not ended.is_set() and not all(request.done() for request in requests):
            wait(requests, timeout=ENDED_CHECK_SECONDS)
        # A run that has ended does not wait for the requests under way: they keep nothing
        # when a later run took its place, and make no more requests.
        requesting.shutdown(wait=not ended.is_set(), cancel_futures=True)
        if not self.goes_on(owner, item_id, document_id, run, ended):
            return
        with self.opened(owner) as (_, shelf):
            kept = shelf.kept_numbers(document_id, self.language)
        missing = [number for number in translated_numbers(document) if number not in kept]
        match missing, failures:
            case [], _:
                self.course_changed(owner, document_id, run, Translated())
            case _, [*_, last]:
                stopped = Halted(reason=failure_reason(last))
                self.course_changed(owner, document_id, run, stopped)
            case _:
                logger.error("document %s ended its run %d with blocks missing", document_id, run)

    def run_over(
        self, owner: str, document_id: str, run: int, ended: Event, finished: Future[None]
    ) -> None:
        # A run that is over lets go of its end, unless a later run has taken its place, and a
        # run that failed stops its translation.
        with self.turn:
            if self.ends.get((owner, document_id)) is ended:
                del self.ends[owner, document_id]
        error = None if finished.cancelled() else finished.exception()
        if error is not None:
            logger.error("translating document %s failed", document_id, exc_info=error)
            self.course_changed(owner, document_id, run, Halted(reason=failure_reason(error)))

    def resume(self) -> None:
        # Translations that were under way when the service stopped go on from what they
        # kept; one whose item was read again meanwhile stops before it asks for anything.
        for owner in self.store.owners():
            with self.opened(owner) as (_, shelf):
                running = shelf.running(self.language)
            for record in running:
                with self.turn:
                    ended = self.new_end(owner, record.document_id)
                self.translate_in_background(
                    owner, record.item_id, record.document_id, record.run, ended
                )

    def close(self) -> None:
        self.closing.set()
        with self.turn:
            for ended in self.ends.values():
                ended.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
