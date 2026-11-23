import logging
from functools import partial
from math import ceil

from pydantic import BaseModel

from infonium.asking import with_attempts
from infonium.document import Document
from infonium.glossary import Term
from infonium.language_model import LanguageModel, StructuredCompletion
from infonium.translation import translated_numbers

# The survey of a document before it is translated: which language it is in, and the names
# and terms that recur in it, each with the one translation to use. The sample it is asked
# about: the blocks that are translated, as many as this many characters hold, spread over
# the document; and the most terms that are found.
SAMPLE_CHARACTERS = 12000
SURVEY_TERMS = 60
SAMPLE_BETWEEN = "\n\n"
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


class DocumentSurvey(BaseModel):
    # What a sample of a document tells before it is translated: its language, whether that
    # is the one it is to be translated into, and else its terms.
    language: str
    written_in_asked_language: bool
    terms: list[Term]


def document_sample(document: Document) -> str:
    # Every so many of the blocks that are translated, so that what they hold together comes
    # near the size of a sample.
    texts = [document.blocks[number].text for number in translated_numbers(document)]
    stride = ceil(sum(map(len, texts)) / SAMPLE_CHARACTERS)
    return SAMPLE_BETWEEN.join(texts[::stride])[:SAMPLE_CHARACTERS]


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
