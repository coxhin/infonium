import logging
from functools import partial
from math import ceil

from pydantic import BaseModel

from infonium.asking import with_attempts
from infonium.document import Document
from infonium.glossary import TERMS_KEPT_IN_ENGLISH, Term
from infonium.language_model import LanguageModel, StructuredCompletion
from infonium.translation import translated_numbers

# The survey of a document before it is translated: which language it is in, and the names
# and terms that recur in it, each with the one translation to use. The sample it is asked
# about: the blocks that are translated, as many as this many characters hold, spread over
# the document; and the most terms that are found. The terms it lists are rendered one way
# throughout, and a term it leaves out is judged anew by each request, so it is shown a paper
# whole: a sample of 12,000 of the 38,897 characters of "Attention Is All You Need" held 3 of
# its 11 mentions of queries and none of positional encoding or embedding, and the whole
# paper, 10,400 input tokens, had gpt-6-luna list 60 to 85 terms.
SAMPLE_CHARACTERS = 40000
SURVEY_TERMS = 100
SAMPLE_BETWEEN = "\n\n"
SURVEY_INSTRUCTIONS = """\
You are given passages taken from across a document, which is to be translated into \
{language} in pieces.

First name the language most of its words are written in, and say whether that is \
{language}, in the script {language} is written in. A document written mostly in another \
language, or in another script of the same language, such as Traditional Chinese where \
Simplified Chinese is asked for, is not.

If it is, list no terms: leave the list empty. Otherwise list the names and terms that \
recur in the passages and that pieces translated apart could render differently, in this \
order. First every term of art that recurs and stays in English by the rule below, as \
itself, in the singular and without a capital that only begins a sentence. Then every \
technical word that recurs and is translated by the rule, as its settled word in \
{language}. Then the names of people, places, organisations, works and products, as \
{language} writes them, unchanged where {language} keeps them as they are written. List a \
term once, in the language and letters the passages write it in, with the forms of it that \
they write, such as its singular and its plural, separated by " / ", and give it the one \
rendering to use for it throughout the document, alone, with nothing after it in brackets. \
List at most {terms}.

{terms_kept_in_english}
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
            instructions=SURVEY_INSTRUCTIONS.format(
                language=language,
                terms=SURVEY_TERMS,
                terms_kept_in_english=TERMS_KEPT_IN_ENGLISH.format(language=language),
            ),
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
