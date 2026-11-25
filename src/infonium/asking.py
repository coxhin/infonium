import logging
from collections.abc import Callable
from itertools import accumulate

from pydantic import BaseModel

from infonium.document import BlockNumber, Document
from infonium.glossary import Term, first_uses, occurring_terms
from infonium.language_model import (
    MODEL_FAILURES,
    LanguageModel,
)
from infonium.reasons import failure_reason
from infonium.rendition import (
    FirstUse,
    PassageBlock,
    Passages,
    Wording,
    keeps_marks,
    renditions,
    wordings_of,
)
from infonium.translation import (
    TranslatedBlock,
    block_wordings,
    is_cut_into_segments,
    translated_block,
    translated_numbers,
)

# Asking a model for blocks of a document in another language: the requests, their
# attempts, and the failure of blocks whose replies could not be used.
#
# The most text of the source one request asks to have translated, in characters, and how
# much of the text around it the request carries. A block longer than the bound is asked for
# in a request of its own. Measured with gpt-6-luna on 152 blocks, four requests at once:
# requests of 2,000 characters took 172 seconds in all and their median 33, of 4,000 124 and
# 57, of 8,000 104 and 90. The middle one serves the first blocks within a minute.
REQUEST_CHARACTERS = 4000
CONTEXT_CHARACTERS = 600
# How many times a request is made before its blocks are given up.
ATTEMPTS = 3
BLOCKS_BETWEEN = "\n"
UNUSABLE = "the model's replies could not be used in {attempts} attempts"

logger = logging.getLogger(__name__)


class UnusableTranslation(Exception):
    # The model answered every request for some blocks, but none of its replies could be used.
    pass


class Work(BaseModel):
    # Blocks of a document to translate, by their numbers, with the title the document goes
    # by, its terms, and the block in which the document first uses each term kept as it is
    # written, by the term's source.
    document: Document
    title: str
    terms: list[Term]
    first_uses: dict[str, BlockNumber]
    numbers: list[BlockNumber]


def document_first_uses(document: Document, terms: list[Term]) -> dict[str, BlockNumber]:
    # The first of the blocks that are translated to use each term kept as it is written.
    texts = {number: document.blocks[number].text for number in translated_numbers(document)}
    return first_uses(terms, texts)


# What becomes of what a request brought: the blocks it translated, and the tokens it took.
# It answers whether the requests are to go on.
Keeper = Callable[[list[TranslatedBlock], int, int], bool]
# Whether the blocks are still to be asked for, asked before each request.
GoesOn = Callable[[], bool]


def request_first_uses(work: Work, wordings: list[list[Wording]]) -> list[FirstUse]:
    # The passages, by their numbers in the request, in which the document first uses a term
    # kept as it is written: the first passage of the term's first block that uses it, or
    # the block's first passage where none does by itself.
    terms = {term.source: term for term in work.terms}
    firsts = [1 + before for before in [0, *accumulate(len(block) for block in wordings)]]
    uses: list[FirstUse] = []
    for source, block_number in work.first_uses.items():
        if block_number not in work.numbers:
            continue
        at = work.numbers.index(block_number)
        using = [
            firsts[at] + offset
            for offset, wording in enumerate(wordings[at])
            if occurring_terms([terms[source]], [wording.text])
        ]
        uses.append(FirstUse(source=source, passage=using[0] if using else firsts[at]))
    return uses


def passages_of(work: Work, wordings: list[list[Wording]]) -> Passages:
    # The blocks' wordings, block by block, with as much of the text before the first block
    # and after the last as a request carries.
    blocks = work.document.blocks
    before = BLOCKS_BETWEEN.join(block.text for block in blocks[: work.numbers[0]])
    after = BLOCKS_BETWEEN.join(block.text for block in blocks[work.numbers[-1] + 1 :])
    return Passages(
        title=work.title,
        terms=work.terms,
        first_uses=request_first_uses(work, wordings),
        before=before[len(before) - CONTEXT_CHARACTERS :] if before else "",
        blocks=[
            PassageBlock(wordings=block, in_segments=is_cut_into_segments(blocks[number]))
            for number, block in zip(work.numbers, wordings, strict=True)
        ],
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


def with_attempts[Result](ask: Callable[[], Result]) -> Result:
    # A request to the model, made again after a failure until the last attempt, whose
    # failure is raised.
    for _ in range(ATTEMPTS - 1):
        try:
            return ask()
        except MODEL_FAILURES as failure:
            logger.warning("a request to the model is made again: %s", failure_reason(failure))
    return ask()


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
        flat = wordings_of(passages)
        ends = [0, *accumulate(len(block) for block in wordings)]
        done: list[TranslatedBlock] = []
        missing: list[BlockNumber] = []
        for number, first, last in zip(pending, ends, ends[1:], strict=False):
            rendered = [replied.rendered[at] for at in range(first, last) if at in replied.rendered]
            complete = len(rendered) == last - first
            faithful = attempt == ATTEMPTS or all(
                keeps_marks(wording, done)
                for wording, done in zip(flat[first:last], rendered, strict=False)
            )
            if complete and faithful:
                block = work.document.blocks[number]
                done.append(translated_block(number, block, model.name, rendered))
            else:
                missing.append(number)
        if not keep(done, replied.input_tokens, replied.output_tokens) or not missing:
            return
        pending = missing
    raise UnusableTranslation(UNUSABLE.format(attempts=ATTEMPTS))
