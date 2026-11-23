import logging
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from functools import partial
from itertools import accumulate
from math import ceil
from threading import Event, Lock

import openai
from pydantic import BaseModel

from infonium.document import BlockNumber, Document
from infonium.intake import failure_reason
from infonium.language_model import (
    LanguageModel,
    StructuredCompletion,
    UnmeteredCompletion,
    UnstructuredCompletion,
)
from infonium.library import Item, Library, LibraryStore, NotInLibrary, Read
from infonium.rendition import Passages, Term, Wording, keeps_marks, renditions
from infonium.shelf import (
    Course,
    Overruled,
    Running,
    Surveyed,
    TranslationRecord,
    TranslationShelf,
    Unsurveyed,
    running_translations,
    with_tokens,
)
from infonium.translation import (
    Progress,
    Stopped,
    Translated,
    TranslatedBlock,
    Translating,
    Unasked,
    Unneeded,
    block_wordings,
    is_translated,
    translated_block,
    translated_numbers,
)

# The most text of the source one request asks to have translated, in characters, and how
# much of the text around it the request carries. A block longer than the bound is asked for
# in a request of its own. Measured with gpt-6-luna on 152 blocks, four requests at once:
# requests of 2,000 characters took 172 seconds in all and their median 33, of 4,000 124 and
# 57, of 8,000 104 and 90. The middle one serves the first blocks within a minute.
REQUEST_CHARACTERS = 4000
CONTEXT_CHARACTERS = 600
# How many requests of one translation run at once, how many translations run at once, and
# how many times a request is made before its blocks are given up. Eight requests at once
# took 288 seconds over 608 blocks where four took 540, and the endpoint refused none.
REQUESTS_AT_ONCE = 8
TRANSLATIONS_AT_ONCE = 2
ATTEMPTS = 3
# How long a run that waits for its requests waits before it looks whether it has ended.
ENDED_CHECK_SECONDS = 1.0
# The sample of a document by which its language is judged and its terms are found: the
# blocks that are translated, as many as this many characters hold, spread over the
# document; and the most terms that are found.
SAMPLE_CHARACTERS = 12000
SURVEY_TERMS = 60
BLOCKS_BETWEEN = "\n"
SAMPLE_BETWEEN = "\n\n"
# The failures of a request to the model after which it is made again.
MODEL_FAILURES = (openai.OpenAIError, UnstructuredCompletion, UnmeteredCompletion)
NOT_TRANSLATED = "block {block_number} of item {item_id} is not translated"
NOT_ASKED = "no translation of item {item_id} is asked for, so none of its blocks is translated"
UNUSABLE = "the model's replies for blocks {numbers} could not be used in {attempts} attempts"
MISSING = "{count} blocks could not be translated; the last failed: {reason}"
READ_AGAIN = "the item was read again into another document, which is translated apart"
SURVEY_INSTRUCTIONS = """\
You are given passages taken from across a document, which is to be translated into \
{language} in pieces.

First name the language most of its words are written in, and say whether that is \
{language}, in the script {language} is written in. A document written mostly in another \
language, or in another script of the same language, such as Traditional Chinese where \
Simplified Chinese is asked for, is not.

If it is, list no terms: leave the list empty. Otherwise list the names and terms that \
recur in the passages and that pieces translated apart could render differently: people, \
places, organisations, works, products, and the terms of the field. Give each the one \
translation into {language} to use for it throughout the document; where {language} keeps a \
name as it is written, give it unchanged. List at most {terms}, those that recur most.
"""

logger = logging.getLogger(__name__)


class UnusableTranslation(Exception):
    # The model answered every request for some blocks, but none of its replies could be used.
    pass


class TranslationNotAsked(Exception):
    # A block is translated again only where a translation was asked for and is needed.
    pass


class DocumentSurvey(BaseModel):
    # What a sample of a document tells before it is translated: its language, whether that
    # is the one it is to be translated into, and else its terms.
    language: str
    written_in_asked_language: bool
    terms: list[Term]


class TranslationStanding(BaseModel):
    # A translation as far as it has come, and the blocks it has kept, in their order.
    document: str
    language: str
    progress: Progress
    input_tokens: int
    output_tokens: int
    blocks: list[TranslatedBlock]


class Work(BaseModel):
    # Blocks of a document to translate, by their numbers, with the title the document goes
    # by and its terms.
    document: Document
    title: str
    terms: list[Term]
    numbers: list[BlockNumber]


# What becomes of what a request brought: the blocks it translated, and the tokens it took.
# It answers whether the requests are to go on.
Keeper = Callable[[list[TranslatedBlock], int, int], bool]
# Whether the blocks are still to be asked for, asked before each request.
GoesOn = Callable[[], bool]


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


def with_attempts[Result](ask: Callable[[], Result]) -> Result:
    # A request to the model, made again after a failure until the last attempt, whose
    # failure is raised.
    for _ in range(ATTEMPTS - 1):
        try:
            return ask()
        except MODEL_FAILURES as failure:
            logger.warning("a request to the model is made again: %s", failure_reason(failure))
    return ask()


def surveyed(
    model: LanguageModel, language: str, document: Document
) -> StructuredCompletion[DocumentSurvey]:
    # The model is asked about a sample of the document: a script tells no language apart
    # from another written in it, and the language is named as a person names it. A request
    # that fails is made again, as many times as a request for blocks is.
    completion = with_attempts(
        partial(
            model.structured,
            instructions=SURVEY_INSTRUCTIONS.format(language=language, terms=SURVEY_TERMS),
            prompt=document_sample(document),
            output=DocumentSurvey,
        )
    )
    logger.info(
        "document %s is judged to be written in %s, with %d terms",
        document.id,
        completion.output.language,
        len(completion.output.terms),
    )
    return completion


def translate_blocks(
    model: LanguageModel, language: str, work: Work, keep: Keeper, goes_on: GoesOn
) -> None:
    # Asks for the blocks, then again for those that did not come back usable, until each
    # has or as many requests are made as a translation makes, and as long as the blocks are
    # still to be asked for. A request that fails is made again the same way, and the last
    # failure is raised. A block that lost most of the marks of one of its passages is
    # asked for again too, but kept as it came from the last request.
    pending = work.numbers
    for attempt in range(1, ATTEMPTS + 1):
        if not goes_on():
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
        except MODEL_FAILURES as failure:
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


def progress_of(record: TranslationRecord, document: Document, kept: int) -> Progress:
    match record.course:
        case Running():
            return Translating(translated=kept, total=len(translated_numbers(document)))
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
        case [TranslationRecord(course=Stopped()) as record]:
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
                shelf.keep_record(record.model_copy(update={"course": Stopped(reason=READ_AGAIN)}))
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
            self.course_changed(owner, document_id, run, Stopped(reason=READ_AGAIN))
        return holds

    def translate_in_background(
        self, owner: str, item_id: str, document_id: str, run: int, ended: Event
    ) -> None:
        finished = self.pool.submit(self.translate, owner, item_id, document_id, run, ended)
        finished.add_done_callback(partial(self.record_failure, owner, document_id, run))

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
                reason = MISSING.format(count=len(missing), reason=failure_reason(last))
                self.course_changed(owner, document_id, run, Stopped(reason=reason))
            case _:
                logger.error("document %s ended its run %d with blocks missing", document_id, run)

    def record_failure(
        self, owner: str, document_id: str, run: int, finished: Future[None]
    ) -> None:
        error = None if finished.cancelled() else finished.exception()
        if error is not None:
            logger.error("translating document %s failed", document_id, exc_info=error)
            self.course_changed(owner, document_id, run, Stopped(reason=failure_reason(error)))

    def resume(self) -> None:
        # Translations that were under way when the service stopped go on from what they
        # kept; one whose item was read again meanwhile stops before it asks for anything.
        with self.store.engine.begin() as connection:
            running = running_translations(connection)
        for translation in running:
            if translation.language == self.language:
                with self.turn:
                    ended = self.new_end(translation.owner, translation.document_id)
                self.translate_in_background(
                    translation.owner,
                    translation.item_id,
                    translation.document_id,
                    translation.run,
                    ended,
                )

    def close(self) -> None:
        self.closing.set()
        with self.turn:
            for ended in self.ends.values():
                ended.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
